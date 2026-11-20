"""Exercise browser publication and restart recovery using the documented YouTube API contract."""

from contextlib import ExitStack, closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from flask import Flask, Response, redirect, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
import social_publication
import youtube_upload as youtube


class YouTubePublication(unittest.TestCase):
    def test_resume_across_restart_and_optional_approval(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "Porch program.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=640x360:rate=24", "-t", "5", "-c:v", "libx264", "-preset", "ultrafast",
                            "-crf", "13", "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            video = clip.read_bytes()
            chunk = 256 * 1024
            self.assertGreater(len(video), 2 * chunk)
            remote = Flask("youtube-fixture")
            provider = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            origin = f"http://127.0.0.1:{provider.server_port}"
            service = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/youtube/callback"
            api_path = "/upload/youtube/v3/videos"
            uploads = {}
            probes, ranges = [], []
            observed = dict(refresh=False, revoked=False)
            cleanup.enter_context(patch.multiple(youtube, OAUTH=replace(
                youtube.OAUTH, authorization_url=origin + "/oauth/authorize", token_url=origin + "/oauth/token",
                revoke_url=origin + "/oauth/revoke",
            ), UPLOAD=origin + api_path, CHUNK=chunk))
            cleanup.enter_context(patch.object(social_publication, "RETRY_SECONDS", (3, 3)))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_PUBLIC_URL": "", "CUEPORCH_YOUTUBE_CLIENT_ID": "cueporch",
                "CUEPORCH_YOUTUBE_CLIENT_SECRET": "fixture-secret",
            }))
            operator = secrets.token_urlsafe(32)
            database = root / "workspace.sqlite"

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
                assert request.args["client_id"] == "cueporch" and request.args["redirect_uri"] == callback
                assert request.args["scope"] == youtube.OAUTH.scope and request.args["access_type"] == "offline"
                assert request.args["code_challenge_method"] == "S256"
                return redirect(callback + "?" + urlencode(dict(state=request.args["state"], code="fixture-code")))

            @remote.post("/oauth/token")
            def token():
                assert request.form["client_id"] == "cueporch" and request.form["client_secret"] == "fixture-secret"
                first = request.form["grant_type"] == "authorization_code"
                if first:
                    assert request.form["code"] == "fixture-code" and request.form["redirect_uri"] == callback
                    assert request.form["code_verifier"]
                else:
                    assert request.form["refresh_token"] == "fixture-refresh" and not observed["refresh"]
                    observed["refresh"] = True
                return dict(access_token="old-access" if first else "fresh-access", refresh_token="fixture-refresh",
                            scope=youtube.OAUTH.scope, token_type="Bearer", expires_in=3600)

            @remote.post("/oauth/revoke")
            def revoke():
                assert request.form["token"] == "fixture-refresh"
                observed["revoked"] = True
                return "", 200

            @remote.post(api_path)
            def start_upload():
                if request.headers["Authorization"] == "Bearer old-access":
                    return "expired", 401
                assert request.headers["Authorization"] == "Bearer fresh-access"
                assert request.args["uploadType"] == "resumable" and request.args["notifySubscribers"] == "false"
                assert request.headers["X-Upload-Content-Type"] == "video/mp4"
                assert int(request.headers["X-Upload-Content-Length"]) == len(video)
                uid = str(len(uploads) + 1)
                uploads[uid] = dict(metadata=request.get_json(), received=bytearray())
                return "", 200, {"Location": origin + api_path + "?upload_id=" + uid}

            @remote.put(api_path)
            def put_bytes():
                assert request.headers["Authorization"] == "Bearer fresh-access"
                uid = request.args["upload_id"]
                item = uploads[uid]
                received = item["received"]
                position = request.headers["Content-Range"]
                data = request.get_data()
                if position == f"bytes */{len(video)}":
                    assert not data
                    probes.append(len(received))
                else:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", position)
                    start, end, total = map(int, match.groups())
                    assert start == len(received) and end - start + 1 == len(data) and total == len(video)
                    ranges.append((uid, start, end))
                    received.extend(data)
                    if uid == "1" and start == 0:
                        return "disconnected after receiving bytes", 503
                if len(received) == len(video):
                    privacy = item["metadata"]["status"]["privacyStatus"]
                    return dict(id=f"PorchVid00{uid}", status={"privacyStatus": privacy}), 201
                return "", 308, {"Range": f"bytes=0-{len(received) - 1}"}

            @remote.get("/playback/<uid>")
            def remote_playback(uid):
                return Response(bytes(uploads[uid]["received"]), mimetype="video/mp4")

            cleanup.enter_context(service_worker(provider))
            cleanup.enter_context(service_worker(service))
            digest = hashlib.sha256(video).hexdigest()
            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                api = page.context.request
                auth = {"Authorization": "Bearer " + operator}
                page.goto(base + "/operator/integrations")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open integrations").click()
                page.get_by_role("button", name="Connect YouTube", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("YouTube is connected.", timeout=15000)
                upload = api.post(base + "/api/library/uploads", headers=auth, multipart={
                    "upload_id": "9504157e-212a-4ea8-aeb5-27661e9936bf",
                    "file": {"name": clip.name, "mimeType": "video/mp4", "buffer": video},
                }, timeout=30000)
                self.assertEqual(201, upload.status, upload.text())
                uid = upload.json()["id"]
                endpoint = base + f"/api/library/work/{uid}/social/youtube"
                self.assertEqual(403, api.get(endpoint).status)
                page.goto(base + f"/operator/videos/{uid}")
                page.get_by_role("link", name="Publish to YouTube").click()
                form = page.locator("#socialForm")
                expect(form).to_be_visible()
                expect(form.get_by_label("Visibility on YouTube")).to_have_value("private")
                expect(form.get_by_label("Title", exact=True)).to_have_value(clip.stem)
                form.get_by_role("button", name="Upload to YouTube").click()

                deadline = monotonic() + 15
                checkpoint = None
                while monotonic() < deadline:
                    with closing(sqlite3.connect(database)) as db:
                        checkpoint = db.execute("SELECT session,probe,attempts FROM social_publication").fetchone()
                    if checkpoint and checkpoint[2]:
                        break
                    sleep(.05)
                self.assertEqual((1, 1), checkpoint[1:])
                self.assertNotIn(b"upload_id=1", checkpoint[0])
                stop()
                self.assertEqual(chunk, len(uploads["1"]["received"]))
                self.assertEqual([], probes)
                start_app()
                page.reload()
                history = page.get_by_role("list", name="", exact=True).filter(has_text="delivered")
                expect(page.locator("#socialJobs")).to_contain_text("delivered", timeout=20000)
                expect(history.get_by_role("link", name="Open on YouTube")).to_have_attribute(
                    "href", "https://www.youtube.com/watch?v=PorchVid001")
                first = api.get(endpoint, headers=auth).json()["jobs"][0]
                self.assertEqual(("delivered", "private", len(video)), (first["state"], first["privacy"], first["sent"]))
                self.assertEqual([chunk], probes)
                self.assertEqual(digest, hashlib.sha256(uploads["1"]["received"]).hexdigest())
                self.assertTrue(observed["refresh"])
                player = browser.new_page()
                player.goto(origin + "/playback/1")
                self.assertEqual(640, player.locator("video").evaluate("async v => {v.muted = true; await v.play(); return v.videoWidth}"))

                policy = api.put(base + "/api/policy/publication", headers=auth, data={"required": True})
                self.assertTrue(policy.ok, policy.text())
                form.get_by_label("Visibility on YouTube").select_option("unlisted")
                form.get_by_label("This video is made for kids").check()
                form.get_by_role("button", name="Upload to YouTube").click()
                expect(page.locator("#socialJobs")).to_contain_text("waiting for publication approval")
                waiting = api.get(endpoint, headers=auth).json()["jobs"][0]
                self.assertEqual(1, len(uploads))
                self.assertTrue(api.post(base + f"/api/actions/{waiting['id']}/decision", headers=auth,
                                        data={"decision": "approve"}).ok)
                expect(page.locator("#socialJobs").get_by_role("link", name="Open on YouTube")).to_have_count(2, timeout=20000)
                self.assertEqual({"privacyStatus": "unlisted", "selfDeclaredMadeForKids": True}, uploads["2"]["metadata"]["status"])
                self.assertEqual(digest, hashlib.sha256(uploads["2"]["received"]).hexdigest())

                form.get_by_label("Visibility on YouTube").select_option("public")
                form.get_by_label("This video is made for kids").uncheck()
                form.get_by_role("button", name="Upload to YouTube").click()
                expect(page.locator("#socialJobs")).to_contain_text("waiting for publication approval")
                self.assertTrue(api.put(base + "/api/policy/publication", headers=auth, data={"required": False}).ok)
                expect(page.locator("#socialJobs").get_by_role("link", name="Open on YouTube")).to_have_count(3, timeout=20000)
                self.assertEqual("public", uploads["3"]["metadata"]["status"]["privacyStatus"])
                self.assertEqual(digest, hashlib.sha256(uploads["3"]["received"]).hexdigest())

                self.assertTrue(api.put(base + "/api/policy/publication", headers=auth, data={"required": True}).ok)
                form.get_by_role("button", name="Upload to YouTube").click()
                expect(page.locator("#socialJobs")).to_contain_text("waiting for publication approval")
                page.goto(base + "/operator/integrations")
                page.get_by_role("button", name="Disconnect YouTube", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("YouTube disconnected.")
                self.assertTrue(observed["revoked"])
                self.assertEqual("canceled", api.get(endpoint, headers=auth).json()["jobs"][0]["state"])
                self.assertEqual(3, len(uploads))
        print("YouTube OAuth refresh, default private upload, interrupted-session restart and byte-range recovery,"
              " verified bytes and playback, opt-in approval and disconnect passed.")


if __name__ == "__main__":
    unittest.main()
