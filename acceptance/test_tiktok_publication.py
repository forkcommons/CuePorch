"""Exercise TikTok advertiser selection and the Business API video upload contract."""

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from flask import Flask, redirect, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tiktok_business as tiktok
from institution import create_app
import social_publication


class TikTokPublication(unittest.TestCase):
    def test_selected_advertiser_upload(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "Porch arts premiere.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=640x360:rate=24", "-t", "5", "-c:v", "libx264", "-preset", "ultrafast",
                            "-crf", "18", "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            video = clip.read_bytes()
            digest = hashlib.sha256(video).hexdigest()
            size = len(video)
            md5 = hashlib.md5(video, usedforsecurity=False).hexdigest()
            remote = Flask("tiktok-fixture")
            provider = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            origin = f"http://127.0.0.1:{provider.server_port}"
            service = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/tiktok/callback"
            database = root / "workspace.sqlite"
            uploads = []
            exchanges = []
            operator = secrets.token_urlsafe(32)

            cleanup.enter_context(patch.multiple(tiktok, OAUTH=replace(
                tiktok.OAUTH, authorization_url=origin + "/oauth/authorize", token_url=origin + "/oauth/token",
            ), API_BASE=origin + "/api"))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_PUBLIC_URL": "", "CUEPORCH_TIKTOK_CLIENT_ID": "cueporch",
                "CUEPORCH_TIKTOK_CLIENT_SECRET": "fixture-secret",
            }))

            def start_app():
                with patch("institution.library_worker.start", return_value=lambda: None):
                    app = create_app(root, database, operator, "station")
                stops = [stop for name, stop in app.extensions.items() if name.startswith("stop_")]

                def stop_app():
                    while stops:
                        stops.pop()()

                cleanup.callback(stop_app)
                service.app = app
                return stop_app

            stop = start_app()

            @remote.get("/oauth/authorize")
            def authorize():
                self.assertEqual("cueporch", request.args.get("app_id"))
                self.assertEqual(callback, request.args.get("redirect_uri"))
                exchanges.append(dict(app_id=request.args.get("app_id"),
                                      redirect_uri=request.args.get("redirect_uri")))
                state = request.args.get("state", "")
                return redirect(callback + "?" + urlencode(dict(auth_code="fixture-code", state=state)))

            @remote.post("/oauth/token")
            def exchange():
                self.assertEqual({"app_id": "cueporch", "secret": "fixture-secret",
                                  "auth_code": "fixture-code",
                                  "return_advertiser_ids": True,
                                  "is_long_term": True}, request.get_json())
                return dict(code=0, data=dict(access_token="fixture-ads-token",
                                             advertiser_ids=["1001", "1002"]))

            @remote.post("/api/file/video/ad/upload/")
            def upload():
                self.assertEqual("fixture-ads-token", request.headers.get("Access-Token"))
                self.assertEqual("1002", request.form.get("advertiser_id"))
                self.assertEqual("UPLOAD_BY_FILE", request.form.get("upload_type"))
                file = request.files["video_file"]
                self.assertEqual(clip.stem, request.form.get("file_name"))
                data = file.read()
                self.assertEqual(md5, request.form.get("video_signature"))
                self.assertEqual("clip.mp4", file.filename)
                self.assertEqual(digest, hashlib.sha256(data).hexdigest())
                self.assertEqual(size, len(data))
                uploads.append(dict(advertiser=request.form.get("advertiser_id"),
                                    filename=file.filename, body=data))
                if len(uploads) > 1:
                    return dict(code=40002, message="upstream outcome ambiguous"), 503
                return dict(code=0, data=[dict(video_id="fixture-video-1")])

            cleanup.enter_context(service_worker(provider))
            cleanup.enter_context(service_worker(service))

            from uuid import uuid4
            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                api = page.context.request
                auth = {"Authorization": "Bearer " + operator}
                page.goto(base + "/operator/integrations")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open integrations").click()
                page.get_by_role("button", name="Connect TikTok for Business", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("TikTok for Business is connected.", timeout=15000)
                upload = api.post(base + "/api/library/uploads", headers=auth, multipart={
                    "upload_id": str(uuid4()),
                    "file": {"name": clip.name, "mimeType": "video/mp4", "buffer": video},
                }, timeout=30000)
                self.assertEqual(201, upload.status, upload.text())
                uid = upload.json()["id"]
                endpoint = base + f"/api/library/work/{uid}/social/tiktok"
                page.goto(base + f"/operator/videos/{uid}")
                page.get_by_role("link", name="Publish to TikTok for Business").click()
                form = page.locator("#socialForm")
                expect(form).to_be_visible()
                selector = form.get_by_label("Advertiser ID", exact=True)
                expect(selector.locator("option")).to_have_text(["Choose an advertiser", "1001", "1002"])
                self.assertNotIn("fixture-", api.get(endpoint, headers=auth).text())
                invalid = api.post(endpoint, headers=auth | {"X-CuePorch-Social": "1"}, data={
                    "id": str(uuid4()), "title": clip.stem, "visibility": "private", "company": "9999",
                    "cut": api.get(endpoint, headers=auth).json()["cut"],
                })
                self.assertEqual(400, invalid.status)
                form.get_by_label("Title").fill(clip.stem)
                selector.select_option("1002")
                form.get_by_label("Send to this Asset Library").check()
                form.get_by_role("button", name="Send to TikTok Ads Manager").click()
                jobs = page.locator("#socialJobs")
                expect(jobs.get_by_role("link", name="Open TikTok Ads Manager", exact=True)).to_be_visible(timeout=15000)
                expect(jobs).to_contain_text("Video fixture-video-1")
                self.assertEqual(1, len(uploads))
                self.assertEqual(digest, hashlib.sha256(uploads[0]["body"]).hexdigest())

                r = api.put(base + "/api/policy/publication", headers=auth, data={"required": True})
                assert r.ok
                form.get_by_role("button", name="Send to TikTok Ads Manager").click()
                expect(jobs).to_contain_text("waiting for publication approval")
                assert len(uploads) == 1
                job_id = api.get(endpoint, headers=auth).json()["jobs"][0]["id"]
                r = api.post(base + f"/api/actions/{job_id}/decision", headers=auth, data={"decision": "approve"})
                assert r.ok
                expect(jobs).to_contain_text("unconfirmed")
                assert len(uploads) == 2
                stop()
                start_app()
                page.reload()
                expect(jobs).to_contain_text("unconfirmed")
                receipt = api.get(endpoint, headers=auth).json()["jobs"][0]
                assert receipt["id"] == job_id
                assert receipt["state"] == "unconfirmed"
                assert len(uploads) == 2
                subprocess.run(['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-frames:v', '1', '-f', 'null', '-'], input=uploads[0]["body"], check=True, capture_output=True)

            # Browser scenario
