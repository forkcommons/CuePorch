import sys
from contextlib import ExitStack
import json
from pathlib import Path
import secrets
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from flask import Flask
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


class WebsiteEmbeds(unittest.TestCase):
    def test_oembed_policy_and_playback(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps(dict(institution="station", schema=1, works=[]))
            )
            clip = root / "Embed clip.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=24",
                    "-t",
                    "3",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(clip),
                ],
                check=True,
                capture_output=True,
            )
            remote = Flask("cueporch-service")
            service = make_server(
                "127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True
            )
            base = f"http://127.0.0.1:{service.server_port}"
            consumer = {"code": ""}
            consumer_app = Flask("embed-consumer")

            @consumer_app.route("/")
            def consumer_page():
                return "<!doctype html><title>Embed consumer</title>" + consumer["code"]

            consumer_server = make_server(
                "127.0.0.1",
                0,
                consumer_app,
                request_handler=QuietHandler,
                threaded=True,
            )
            consumer_url = f"http://127.0.0.1:{consumer_server.server_port}"
            database = root / "workspace.sqlite"
            operator = secrets.token_urlsafe(32)
            cleanup.enter_context(
                patch.dict("os.environ", {"CUEPORCH_PUBLIC_URL": base})
            )

            def start_app():
                with patch(
                    "institution.library_worker.start", return_value=lambda: None
                ):
                    app = create_app(root, database, operator, "station")
                stops = [
                    stop
                    for name, stop in app.extensions.items()
                    if name.startswith("stop_")
                ]

                def stop_app():
                    while stops:
                        stops.pop()()

                cleanup.callback(stop_app)
                service.app = app
                return stop_app

            start_app()
            cleanup.enter_context(service_worker(service))
            cleanup.enter_context(service_worker(consumer_server))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                self.exercise(browser, base, consumer_url, consumer, operator, clip)

    def exercise(self, browser, base, consumer_url, consumer, operator, clip):
        import json
        from uuid import uuid4
        from playwright.sync_api import expect

        page = browser.new_page()
        api = page.context.request
        h = {"Authorization": f"Bearer {operator}"}
        r = api.post(
            f"{base}/api/library/uploads",
            headers=h,
            multipart={
                "upload_id": str(uuid4()),
                "file": {
                    "name": clip.name,
                    "mimeType": "video/mp4",
                    "buffer": clip.read_bytes(),
                },
            },
        )
        assert r.status == 201, r.text
        uid = r.json()["id"]
        watch = f"{base}/watch/work/{uid}"
        pr = api.put(
            f"{base}/api/library/work/{uid}/privacy",
            headers={**h, "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "visibility": "public",
                    "embed": "anywhere",
                    "domains": [],
                    "download": False,
                }
            ),
        )
        assert pr.ok, pr.text
        page.goto(watch)
        durl = page.locator(
            'link[rel="alternate"][type="application/json+oembed"]'
        ).get_attribute("href")
        d = api.get(durl + "&maxwidth=400&maxheight=180")
        assert d.ok and "no-store" in (d.headers.get("cache-control") or "")
        dj = d.json()
        assert dj["width"] <= 400 and dj["height"] <= 180
        assert f"{base}/embed/work/{uid}" in dj["html"]
        m = api.get(
            f"{base}/api/public/oembed",
            params={"url": f"https://example.invalid/watch/work/{uid}"},
        )
        assert m.status == 404
        consumer["code"] = dj["html"]
        page.goto(consumer_url)
        v = page.frame_locator("iframe").locator("video")
        expect(v).to_be_visible()
        v.evaluate("e => { e.muted = true; e.play(); }")
        expect(v).to_have_js_property("videoWidth", 320)
        t0 = v.evaluate("e => e.currentTime")
        page.wait_for_timeout(500)
        assert v.evaluate("e => e.currentTime") > t0

        old_src = v.evaluate("e => e.currentSrc || e.src")
        old_media = None if old_src.startswith("blob:") else old_src
        allowed = consumer_url.split("//", 1)[-1].split("/", 1)[0]
        pr = api.put(
            f"{base}/api/library/work/{uid}/privacy",
            headers={**h, "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "visibility": "public",
                    "embed": "domains",
                    "domains": [allowed],
                    "download": False,
                }
            ),
        )
        assert pr.ok, pr.text
        assert api.get(durl).status == 404
        page.goto(watch)
        assert (
            page.locator(
                'link[rel="alternate"][type="application/json+oembed"]'
            ).count()
            == 0
        )
        page.goto(consumer_url)
        v = page.frame_locator("iframe").locator("video")
        expect(v).to_be_visible()
        v.evaluate("e => { e.muted = true; e.play(); }")
        expect(v).to_have_js_property("videoWidth", 320)
        bad = api.get(
            f"{base}/embed/work/{uid}",
            headers={"Sec-Fetch-Dest": "iframe", "Referer": "https://example.invalid/"},
        )
        assert bad.status == 403
        page.goto(watch)
        page.evaluate("v => sessionStorage.setItem('cueporchOperator', v)", operator)
        page.goto(f"{base}/operator/videos/{uid}")
        expect(page.locator("#embedWebsites")).to_be_visible()
        page.locator("#embedWebsites summary").click()
        expect(page.locator("#embedWebsites strong")).to_have_text(
            ["WordPress", "WooCommerce", "Wix", "GoDaddy"]
        )
        code = page.locator("#workspaceEmbed").input_value()
        assert (
            f"{base}/embed/work/{uid}" in code
            and "/media" not in code
            and clip.name not in code
        )
        pr = api.put(
            f"{base}/api/library/work/{uid}/privacy",
            headers={**h, "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "visibility": "private",
                    "embed": "disabled",
                    "domains": [],
                    "download": False,
                }
            ),
        )
        assert pr.ok, pr.text
        assert api.get(durl).status == 404
        assert api.get(watch).status == 404
        assert (
            api.get(
                f"{base}/embed/work/{uid}",
                headers={"Referer": consumer_url, "Sec-Fetch-Dest": "iframe"},
            ).status
            == 404
        )
        if old_media:
            assert api.get(old_media).status == 404
