import sys
from contextlib import ExitStack
import json
import time
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
import library_hubspot_bridge
from institution import create_app


class BridgeProvider:
    def __init__(self):
        self.record = None
        self.creates = 0
        self.updates = 0
        self.archives = 0

    @staticmethod
    def response(status, data=None):
        import requests

        r = requests.Response()
        r.status_code = status
        r._content = json.dumps(data).encode("utf-8") if data is not None else b""
        r._content_consumed = True
        return r

    def request(self, method, url, **kwargs):
        method = method.upper()
        oauth_url = "https://api.hubapi.com/oauth/2026-03/token"
        base = "https://api.hubapi.com/crm/objects/2026-09/a123_Videos"

        if method == "POST" and url == oauth_url:
            return self.response(
                200,
                {
                    "access_token": "bridge-access",
                    "refresh_token": "fixture-refresh-token",
                    "expires_in": 1800,
                    "token_type": "Bearer",
                },
            )

        headers = kwargs.get("headers") or {}
        assert headers.get("Authorization") == "Bearer bridge-access"

        if method == "POST" and url == base + "/search":
            body = kwargs.get("json") or {}
            filters = []
            for group in body.get("filterGroups", []):
                filters.extend(group.get("filters", []))

            external_id = None
            for f in filters:
                if f.get("propertyName") == "hs_external_id":
                    assert f.get("operator") == "EQ"
                    external_id = f.get("value")
                    break

            assert external_id is not None
            assert body.get("limit") == 2

            if (
                self.record is not None
                and self.record["properties"].get("hs_external_id") == external_id
            ):
                return self.response(200, {"total": 1, "results": [self.record]})
            return self.response(200, {"total": 0, "results": []})

        if method == "POST" and url == base:
            body = kwargs.get("json") or {}
            props = body.get("properties") or {}
            keys = (
                "hs_title",
                "hs_external_id",
                "hs_oembed_url",
                "hs_details_page_link",
            )

            self.record = {
                "id": "712",
                "archived": False,
                "properties": {key: props.get(key) for key in keys},
            }
            self.creates += 1
            if self.creates == 1:
                return self.response(502, {"message": "fixture lost acknowledgment"})
            return self.response(201, self.record)

        if method == "PATCH" and url == base + "/712":
            body = kwargs.get("json") or {}
            props = body.get("properties") or {}
            keys = (
                "hs_title",
                "hs_external_id",
                "hs_oembed_url",
                "hs_details_page_link",
            )

            assert self.record is not None
            assert props.get("hs_external_id") == self.record["properties"].get(
                "hs_external_id"
            )
            assert set(props.keys()) == set(keys)

            self.record["properties"] = {key: props.get(key) for key in keys}
            self.updates += 1
            return self.response(200, self.record)

        if method == "DELETE" and url == base + "/712":
            self.archives += 1
            self.record = None
            return self.response(204)

        raise AssertionError(f"Unexpected network request: {method} {url}")


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
            bridge = BridgeProvider()
            bridge.time_offset = 0
            cleanup.enter_context(
                patch.dict(
                    "os.environ",
                    {
                        "CUEPORCH_PUBLIC_URL": base,
                        "CUEPORCH_HUBSPOT_CLIENT_ID": "bridge-client",
                        "CUEPORCH_HUBSPOT_CLIENT_SECRET": "bridge-secret",
                        "CUEPORCH_HUBSPOT_MEDIA_TYPE": "a123_Videos",
                    },
                )
            )
            cleanup.enter_context(
                patch("workspace_oauth.provider_request", bridge.request)
            )
            original_bridge_start = library_hubspot_bridge.start
            cleanup.enter_context(
                patch.object(
                    library_hubspot_bridge,
                    "start",
                    side_effect=lambda *args, **kwargs: original_bridge_start(
                        *args,
                        **{**kwargs, "clock": lambda: time.time() + bridge.time_offset},
                    ),
                )
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
                self.exercise(
                    browser, base, consumer_url, consumer, operator, clip, bridge
                )

    def bridge_wait(self, page, base, h, uid, state):
        deadline = time.time() + 10
        last = None
        while time.time() < deadline:
            response = page.request.get(
                f"{base}/api/library/work/{uid}/hubspot-media-bridge",
                headers=h,
            )
            assert response.ok, response.text
            last = response.json()
            if last.get("state") == state:
                return last
            page.wait_for_timeout(100)
        assert False, last

    def verify_bridge_publication(self, page, base, h, operator, uid, durl, bridge):
        from urllib.parse import parse_qs, urlencode, urlsplit
        from playwright.sync_api import expect

        callback = base + "/integration/oauth/hubspot/callback"
        page.add_init_script(
            f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator)})"
        )

        def handle_hubspot(route):
            qs = parse_qs(urlsplit(route.request.url).query)
            assert "state" in qs
            assert qs["redirect_uri"][0] == callback
            optional_scope = qs.get("optional_scope", [""])[0].split()
            assert "media_bridge.read" in optional_scope
            assert "media_bridge.write" in optional_scope
            location = (
                callback
                + "?"
                + urlencode({"state": qs["state"][0], "code": "bridge-code"})
            )
            route.fulfill(status=302, headers={"Location": location})

        page.route("https://app.hubspot.com/oauth/authorize?*", handle_hubspot)
        page.goto(base + "/operator/integrations")
        page.get_by_text("Connect HubSpot", exact=True).click()
        expect(page.get_by_text("Disconnect HubSpot", exact=True)).to_be_visible()

        page.goto(base + "/operator/library")
        page.get_by_role("button", name="Load library", exact=True).click()
        page.locator('summary[aria-label^="Actions for "]').first.click()
        page.get_by_role("button", name="Share", exact=True).click()
        group = page.get_by_role("group", name="HubSpot Media Bridge")
        expect(group).to_be_visible()
        add = group.get_by_role("button", name="Add video to HubSpot", exact=True)
        expect(add).to_be_enabled()

        r = page.request.put(
            base + "/api/policy/publication",
            headers=h,
            data={"required": True},
        )
        assert r.ok, r.text
        add.click()
        expect(group.get_by_role("status")).to_contain_text(
            "Waiting for publication approval"
        )
        assert bridge.creates == 0

        r = page.request.put(
            base + "/api/policy/publication",
            headers=h,
            data={"required": False},
        )
        assert r.ok, r.text
        receipt = self.bridge_wait(page, base, h, uid, "unconfirmed")
        assert bridge.creates == 1

        group.get_by_role("button", name="Refresh HubSpot status", exact=True).click()
        expect(group.get_by_role("status")).to_have_text(receipt["problem"])

        add = group.get_by_role("button", name="Add video to HubSpot", exact=True)
        add.click()
        self.bridge_wait(page, base, h, uid, "published")
        assert bridge.creates == 1
        assert bridge.updates == 1
        self.assert_bridge_properties(bridge, durl, base, uid)

    def verify_bridge_withdrawal(self, page, base, h, uid, bridge):
        bridge.time_offset += 35
        self.bridge_wait(page, base, h, uid, "archived")
        assert bridge.archives == 1
        assert bridge.record is None

    def assert_bridge_properties(self, bridge, durl, base, uid):
        assert bridge.record is not None
        props = bridge.record["properties"]
        assert set(props.keys()) == {
            "hs_external_id",
            "hs_title",
            "hs_oembed_url",
            "hs_details_page_link",
        }
        assert props["hs_oembed_url"] == durl
        assert props["hs_details_page_link"] == f"{base}/watch/work/{uid}"
        assert props["hs_title"]
        assert props["hs_external_id"]

    def prepare_bridge_folder(self, page, base, h, clip, manual_uid):
        from uuid import uuid4

        upload_id = str(uuid4())
        r = page.request.post(
            f"{base}/api/library/uploads",
            headers=h,
            multipart={
                "upload_id": upload_id,
                "file": {
                    "name": "folder-clip.mp4",
                    "mimeType": "video/mp4",
                    "buffer": clip.read_bytes(),
                },
            },
        )
        assert r.status == 201, r.text()
        uid = r.json()["id"]
        assert uid != manual_uid, f"upload returned same uid as manual_uid: {uid}"

        r = page.request.put(
            f"{base}/api/library/work/{uid}/privacy",
            headers=h,
            data={
                "visibility": "public",
                "embed": "anywhere",
                "domains": [],
                "download": False,
            },
        )
        assert r.ok, r.text()

        r = page.request.post(
            f"{base}/api/library/folders",
            headers=h,
            data={"name": "Media Bridge test", "color": "#22aaff", "parent": None},
        )
        assert r.status == 201, r.text()
        folder_id = r.json()["id"]

        r = page.request.put(
            f"{base}/api/library/placement",
            headers=h,
            data={"works": [uid, manual_uid], "folder": folder_id},
        )
        assert r.ok, r.text()

        return uid, folder_id

    def verify_bridge_folder(self, page, base, h, clip, manual_uid, bridge):
        from playwright.sync_api import expect

        uid, folder_id = self.prepare_bridge_folder(page, base, h, clip, manual_uid)

        page.goto(f"{base}/operator/library")
        page.get_by_role("button", name="Load library", exact=True).click()
        page.get_by_role("navigation", name="Folders").get_by_role(
            "button", name="Media Bridge test", exact=True
        ).click()
        page.get_by_role("button", name="HubSpot Media Bridge", exact=True).click()

        dialog = page.get_by_role("dialog", name="HubSpot Media Bridge")
        dialog.get_by_role(
            "button", name="Sync this folder to HubSpot", exact=True
        ).click()
        expect(
            page.get_by_text(
                "HubSpot sync is active for Media Bridge test.", exact=True
            )
        ).to_be_visible()

        record = self.bridge_wait(page, base, h, uid, "published")
        assert record["managed"] is True
        assert bridge.creates == 2
        assert (
            bridge.record["properties"]["hs_details_page_link"]
            == f"{base}/watch/work/{uid}"
        )

        resp = page.request.put(
            f"{base}/api/library/placement",
            headers=h,
            data={"works": [uid], "folder": None},
        )
        assert resp.ok

        bridge.time_offset += 16
        self.bridge_wait(page, base, h, uid, "archived")
        assert bridge.archives == 2
        assert bridge.record is None

        resp = page.request.put(
            f"{base}/api/library/placement",
            headers=h,
            data={"works": [uid], "folder": folder_id},
        )
        assert resp.ok

        bridge.time_offset += 16
        self.bridge_wait(page, base, h, uid, "published")
        assert bridge.creates == 3

        dialog.get_by_role(
            "button", name="Stop HubSpot folder sync", exact=True
        ).click()
        self.bridge_wait(page, base, h, uid, "archived")
        assert bridge.archives == 3
        assert bridge.record is None

        manual_record = self.bridge_wait(page, base, h, manual_uid, "archived")
        assert manual_record is not None
        assert manual_record["managed"] is False

    def render_hubspot_module(self, name, payload, **fields):
        import re
        from jinja2 import Environment
        from markupsafe import escape

        module_dir = (
            Path(__file__).resolve().parents[1] / "integrations" / "hubspot" / name
        )
        with open(module_dir / "fields.json", encoding="utf-8") as f:
            assert json.load(f)[0]["supported_source_types"] == ["media_bridge"]
        env = Environment(autoescape=True)
        env.filters["escape_attr"] = escape
        env.filters["regex_replace"] = lambda value, pattern, replacement: re.sub(
            pattern, replacement, str(value)
        )
        module = {
            "video": {"media_bridge_object": {"oembed_response": payload}},
            **fields,
        }
        template = env.from_string(
            (module_dir / "module.html").read_text(encoding="utf-8")
        )
        return template.render(module=module)

    def verify_hubspot_email(self, page, payload):
        from playwright.sync_api import expect

        destination = {"href": "https://campaign.example.invalid/watch"}

        # Case 1: rendered fallback with a real destination and empty poster
        html = self.render_hubspot_module(
            "cueporch-email.module",
            payload,
            destination=destination,
            poster={},
        )
        page.set_content(html)
        link = page.get_by_role("link")
        expect(link).to_have_attribute("href", destination["href"])
        expect(link).to_contain_text(payload["title"])
        expect(page.locator("iframe")).to_have_count(0)
        expect(page.locator("video")).to_have_count(0)
        expect(page.locator("script")).to_have_count(0)

        # Case 2: poster src on the synthetic host; string checks only, no network.
        poster_src = "https://campaign.example.invalid/poster.png"
        html_with_poster = self.render_hubspot_module(
            "cueporch-email.module",
            payload,
            destination=destination,
            poster={"src": poster_src},
        )
        assert poster_src in html_with_poster, "poster image src missing from render"
        assert "<img" in html_with_poster, "poster image element missing from render"
        assert destination["href"] in html_with_poster, (
            "destination link missing from render"
        )
        assert "iframe" not in html_with_poster.lower()
        assert "<video" not in html_with_poster.lower()

        # Case 3: blank destination renders a blank/absent link
        html_blank = self.render_hubspot_module(
            "cueporch-email.module",
            payload,
            destination={"href": ""},
            poster={},
        )
        page.set_content(html_blank)
        expect(page.get_by_role("link")).to_have_count(0)

    def exercise(self, browser, base, consumer_url, consumer, operator, clip, bridge):
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
        self.verify_bridge_publication(page, base, h, operator, uid, durl, bridge)
        d = api.get(
            bridge.record["properties"]["hs_oembed_url"] + "&maxwidth=400&maxheight=180"
        )
        assert d.ok and "no-store" in (d.headers.get("cache-control") or "")
        dj = d.json()
        assert dj["width"] <= 400 and dj["height"] <= 180
        assert f"{base}/embed/work/{uid}" in dj["html"]
        m = api.get(
            f"{base}/api/public/oembed",
            params={"url": f"https://example.invalid/watch/work/{uid}"},
        )
        assert m.status == 404
        self.verify_hubspot_email(page, dj)
        consumer["code"] = self.render_hubspot_module("cueporch-video.module", dj)
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
        self.verify_bridge_withdrawal(page, base, h, uid, bridge)
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
        self.verify_bridge_folder(page, base, h, clip, uid, bridge)
