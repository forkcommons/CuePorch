"""Source-browser coverage of the documented X native video posting workflow."""

from contextlib import ExitStack, closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from flask import Flask, Response, redirect, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
import social_publication
import x_upload as x


class XPublication(unittest.TestCase):
    def test_restarted_media_upload_and_public_profile_recheck(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "Porch headline.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=640x360:rate=24", "-t", "5", "-c:v", "libx264", "-preset", "ultrafast",
                            "-crf", "18", "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            video = clip.read_bytes()
            chunk = 128 * 1024
            self.assertGreater(len(video), 2 * chunk)
            remote = Flask("x-fixture")
            provider = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            origin = f"http://127.0.0.1:{provider.server_port}"
            service = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/x/callback"
            database = root / "workspace.sqlite"
            operator = secrets.token_urlsafe(32)
            state = dict(lookups=0, refreshed=False, interrupted=False, protected=False, revoked=False)
            uploads = {}
            request_segments, posts = [], []
            finalized, ready = set(), set()
            cleanup.enter_context(patch.multiple(x, OAUTH=replace(
                x.OAUTH, authorization_url=origin + "/oauth/authorize", token_url=origin + "/oauth/token",
                revoke_url=origin + "/oauth/revoke",
            ), API=origin + "/2", CHUNK=chunk))
            cleanup.enter_context(patch.object(social_publication, "RETRY_SECONDS", (3, 3)))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_PUBLIC_URL": "", "CUEPORCH_X_CLIENT_ID": "cueporch", "CUEPORCH_X_CLIENT_SECRET": "",
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
                assert request.args["client_id"] == "cueporch" and request.args["redirect_uri"] == callback
                assert request.args["scope"] == x.OAUTH.scope and request.args["code_challenge_method"] == "S256"
                return redirect(callback + "?" + urlencode(dict(state=request.args["state"], code="x-code")))

            @remote.post("/oauth/token")
            def token():
                assert request.form["client_id"] == "cueporch" and "client_secret" not in request.form
                if request.form["grant_type"] == "authorization_code":
                    assert request.form["code"] == "x-code" and request.form["code_verifier"]
                    return dict(access_token="fixture-old", refresh_token="fixture-rotate",
                                token_type="Bearer", scope=x.OAUTH.scope, expires_in=7200)
                assert request.form["refresh_token"] == "fixture-rotate" and not state["refreshed"]
                state["refreshed"] = True
                return dict(access_token="fixture-live", refresh_token="fixture-revocable",
                            token_type="Bearer", scope=x.OAUTH.scope, expires_in=7200)

            @remote.post("/oauth/revoke")
            def revoke():
                assert request.form["token"] == "fixture-revocable"
                state["revoked"] = True
                return "", 200

            @remote.get("/2/users/me")
            def account():
                assert request.args["user.fields"] == "protected"
                if request.headers["Authorization"] == "Bearer fixture-old":
                    state["lookups"] += 1
                    if state["lookups"] > 1:
                        return "", 401
                else:
                    assert request.headers["Authorization"] == "Bearer fixture-live"
                return dict(data=dict(id="19", username="porch", protected=state["protected"]))

            @remote.before_request
            def check_api():
                if request.path.startswith("/2/") and request.path != "/2/users/me":
                    assert request.headers["Authorization"] == "Bearer fixture-live"

            @remote.post("/2/media/upload/initialize")
            def initialize():
                assert request.json == dict(media_type="video/mp4", media_category="tweet_video", total_bytes=len(video))
                uid = str(len(uploads) + 1)
                uploads[uid] = {}
                return dict(data=dict(id=uid, expires_after_secs=3600))

            @remote.post("/2/media/upload/<uid>/append")
            def append(uid):
                segment = int(request.form["segment_index"])
                data = request.files["media"].read()
                assert data == video[segment * chunk:(segment + 1) * chunk]
                request_segments.append((uid, segment))
                uploads[uid][segment] = data
                if uid == "1" and segment == 1 and not state["interrupted"]:
                    state["interrupted"] = True
                    return "", 503
                return dict(data=dict(expires_at=2000000000))

            @remote.post("/2/media/upload/<uid>/finalize")
            def finalize(uid):
                assert b"".join(uploads[uid].values()) == video
                finalized.add(uid)
                return dict(data=dict(id=uid, processing_info=dict(state="pending", check_after_secs=1)))

            @remote.get("/2/media/upload")
            def status():
                uid = request.args["media_id"]
                assert request.args["command"] == "STATUS" and uid in finalized
                return dict(data=dict(id=uid, processing_info=dict(
                    state="succeeded" if uid in ready else "in_progress", check_after_secs=1)))

            @remote.post("/2/tweets")
            def post():
                assert request.json == dict(text="Porch bulletin", media={"media_ids": ["1"]})
                assert "1" in ready and not state["protected"]
                posts.append("1")
                return dict(data=dict(id="8899", text="Porch bulletin")), 201

            @remote.get("/playback")
            def playback():
                return Response(b"".join(uploads["1"].values()), mimetype="video/mp4")

            cleanup.enter_context(service_worker(provider))
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                api = page.context.request
                auth = {"Authorization": "Bearer " + operator}
                page.goto(base + "/operator/integrations")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open integrations").click()
                page.get_by_role("button", name="Connect X", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("X is connected.", timeout=15000)
                item = api.post(base + "/api/library/uploads", headers=auth, multipart={
                    "upload_id": str(uuid4()), "file": {"name": clip.name, "mimeType": "video/mp4", "buffer": video},
                }, timeout=30000)
                self.assertEqual(201, item.status, item.text())
                uid = item.json()["id"]
                endpoint = base + f"/api/library/work/{uid}/social/x"
                page.goto(base + f"/operator/videos/{uid}")
                page.get_by_role("link", name="Publish to X").click()
                expect(page.locator("#socialCompany")).to_have_text("@porch")
                form = page.locator("#socialForm")
                consent = form.get_by_label("Publish a public video on X")
                expect(consent).not_to_be_checked()
                form.get_by_label("Post text").fill("Porch bulletin")
                consent.check()
                form.get_by_role("button", name="Post to X").click()
                deadline = monotonic() + 15
                checkpoint = None
                while monotonic() < deadline:
                    with closing(sqlite3.connect(database)) as db:
                        checkpoint = db.execute("SELECT session,offset,attempts FROM social_publication").fetchone()
                    if checkpoint and checkpoint[2]:
                        break
                    sleep(.05)
                self.assertEqual((chunk, 1), checkpoint[1:])
                self.assertTrue(state["refreshed"])
                self.assertEqual([("1", 0), ("1", 1)], request_segments)
                stop()
                start_app()
                page.reload()
                deadline = monotonic() + 15
                while "1" not in finalized and monotonic() < deadline:
                    sleep(.05)
                self.assertIn("1", finalized)
                self.assertEqual([], posts)
                ready.add("1")
                expect(page.locator("#socialJobs").get_by_role("link", name="Open on X")).to_have_attribute(
                    "href", "https://x.com/i/web/status/8899", timeout=15000)
                self.assertEqual(1, request_segments.count(("1", 0)))
                self.assertEqual(2, request_segments.count(("1", 1)))
                self.assertEqual(hashlib.sha256(video).digest(), hashlib.sha256(b"".join(uploads["1"].values())).digest())
                player = browser.new_page()
                player.goto(origin + "/playback")
                self.assertEqual(640, player.locator("video").evaluate("async v => {v.muted = true; await v.play(); return v.videoWidth}"))

                form.get_by_label("Post text").fill("Porch bulletin")
                consent.check()
                form.get_by_role("button", name="Post to X").click()
                deadline = monotonic() + 15
                while "2" not in finalized and monotonic() < deadline:
                    sleep(.05)
                self.assertIn("2", finalized)
                state["protected"] = True
                ready.add("2")
                expect(page.locator("#socialJobs")).to_contain_text("A protected X account cannot receive a public post", timeout=15000)
                self.assertEqual(["1"], posts)
                self.assertEqual("failed", api.get(endpoint, headers=auth).json()["jobs"][0]["state"])
                page.goto(base + "/operator/integrations")
                page.get_by_role("button", name="Disconnect X", exact=True).click()
                expect(page.get_by_role("button", name="Connect X", exact=True)).to_be_visible()
                self.assertTrue(state["revoked"])
        print("X PKCE, rotating token, public profile, exact segment retry after restart, processing, decoded playback,"
              " profile protection before posting and provider revocation passed.")


if __name__ == "__main__":
    unittest.main()
