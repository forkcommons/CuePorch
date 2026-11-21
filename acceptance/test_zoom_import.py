"""Exercise Zoom cloud recording selection and unattended archive."""

import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlencode, urlsplit

from flask import Flask, Response, redirect, request
from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
import zoom_recordings as zoom


class ZoomImport(unittest.TestCase):
    def test_preferred_view_and_auto_archive(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264",
                            "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            media = clip.read_bytes()
            remote = Flask("mock-zoom")
            provider = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            provider_url = f"http://127.0.0.1:{provider.server_port}"
            observed = dict(downloads=[], revoked=False, automatic=None)
            selected = "/weekly+second//="
            initial_media_url = zoom.media_url

            def local_media(url, signed=False):
                allowed = initial_media_url(url, signed)
                return provider_url + urlsplit(allowed).path

            cleanup.enter_context(patch.multiple(
                zoom, ORIGIN=provider_url, API=provider_url + "/v2/", POLL_SECONDS=.25, media_url=local_media,
            ))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_ZOOM_CLIENT_ID": "zoom-public", "CUEPORCH_ZOOM_CLIENT_SECRET": "",
                "CUEPORCH_PUBLIC_URL": "", "CUEPORCH_DROPBOX_CLIENT_ID": "", "CUEPORCH_GOOGLE_MEET_CLIENT_ID": "",
            }))
            operator = secrets.token_urlsafe(32)
            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, root / "workspace.sqlite", operator, "station")
            for name, stop in app.extensions.items():
                if name.startswith("stop_"):
                    cleanup.callback(stop)
            service = make_server("127.0.0.1", 0, app, request_handler=QuietHandler, threaded=True)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/zoom/callback"

            @remote.get("/oauth/authorize")
            def consent():
                assert request.args["client_id"] == "zoom-public" and request.args["redirect_uri"] == callback
                assert request.args["code_challenge_method"] == "S256"
                observed["challenge"] = request.args["code_challenge"]
                return redirect(callback + "?" + urlencode(dict(state=request.args["state"], code="zoom-code")))

            @remote.post("/oauth/token")
            def token():
                assert request.form["client_id"] == "zoom-public" and "client_secret" not in request.form
                assert "Authorization" not in request.headers
                hashed = hashlib.sha256(request.form["code_verifier"].encode()).digest()
                assert base64.urlsafe_b64encode(hashed).decode().rstrip("=") == observed["challenge"]
                return dict(access_token="fixture-zoom", refresh_token="fixture-refresh", scope=zoom.SCOPES,
                            expires_in=3600, token_type="bearer")

            @remote.post("/oauth/revoke")
            def revoke():
                assert dict(request.form) == {"client_id": "zoom-public", "token": "fixture-zoom"}
                observed["revoked"] = True
                return "", 200

            def meeting(occurrence, cut, start):
                def file(view, suffix):
                    uid = f"{cut}-{suffix}"
                    return dict(id=uid, file_type="MP4", status="completed", recording_type=view,
                                file_size=len(media), recording_end=start,
                                download_url="https://tenant.zoom.us/rec/download/" + uid)
                return dict(uuid=occurrence, id=426609133, topic="Community meeting", start_time=start,
                            recording_files=[file("gallery_view", "gallery"),
                                             file("shared_screen_with_speaker_view", "speaker")])

            first = meeting("first", "first", "2026-11-17T16:00:00Z")
            second = meeting(selected, "second", "2026-11-20T16:00:00Z")

            @remote.get("/v2/users/me/recordings")
            def recordings():
                assert request.headers["Authorization"] == "Bearer fixture-zoom"
                assert "from" in request.args and "to" in request.args
                if request.args.get("next_page_token") == "oldest":
                    return dict(meetings=[first])
                current = [second]
                if observed["automatic"]:
                    current.append(observed["automatic"])
                return dict(meetings=current, next_page_token="oldest")

            @remote.get("/v2/meetings/<path:occurrence>/recordings")
            def selected_recording(occurrence):
                assert request.headers["Authorization"] == "Bearer fixture-zoom"
                assert unquote(occurrence) in (selected, "automatic")
                return second if unquote(occurrence) == selected else observed["automatic"]

            @remote.get("/rec/download/<uid>")
            def download(uid):
                assert request.headers["Authorization"] == "Bearer fixture-zoom"
                assert uid.endswith("-speaker")
                return redirect("https://cdn.zoomcdn.net/content/" + uid)

            @remote.get("/content/<uid>")
            def cdn(uid):
                assert "Authorization" not in request.headers
                observed["downloads"].append(uid)
                return Response(media, mimetype="video/mp4")

            cleanup.enter_context(service_worker(provider))
            cleanup.enter_context(service_worker(service))
            api = cleanup.enter_context(requests.Session())
            api.headers["Authorization"] = "Bearer " + operator
            watch = base + "/api/integrations/zoom/watch"

            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                page.goto(base + "/operator/integrations")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open integrations").click()
                page.get_by_role("button", name="Connect Zoom", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("Zoom is connected.")
                page.get_by_role("button", name="Browse recordings").click()
                page.get_by_role("button", name="More videos").click()
                expect(page.get_by_role("button", name="Import Community meeting · 2026-11-17 1600 UTC")).to_be_visible()
                page.get_by_role("button", name="Import Community meeting · 2026-11-20 1600 UTC").click()
                receipt = page.locator("#driveReceipt")
                expect(receipt).to_contain_text("(private)", timeout=30000)
                self.assertEqual(["second-speaker"], observed["downloads"])
                receipt.get_by_role("link").click()
                video = page.locator("#workspacePreview")
                expect(video).to_be_visible()
                self.assertEqual(320, video.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}"))

                page.goto(base + "/operator/integrations")
                page.get_by_role("button", name="Browse recordings").click()
                page.get_by_role("button", name="Enable automatic archive").click()
                expect(page.locator("#dropboxWatchStatus")).to_contain_text("Archiving new Zoom recordings.")
                observed["automatic"] = meeting("automatic", "automatic", datetime.now(timezone.utc).isoformat())
                deadline = monotonic() + 15
                while monotonic() < deadline:
                    jobs = api.get(watch, timeout=5).json()["jobs"]
                    if any(job["state"] == "imported" for job in jobs):
                        break
                    sleep(.1)
                else:
                    self.fail("The new Zoom occurrence did not finish automatic import.")
                self.assertEqual(1, len(jobs))
                self.assertEqual(["second-speaker", "automatic-speaker"], observed["downloads"])
                page.get_by_role("button", name="Disconnect Zoom", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("Zoom disconnected.")
                self.assertTrue(observed["revoked"])
                self.assertEqual(409, api.get(base + "/api/integrations/zoom/files", timeout=5).status_code)
                self.assertEqual({"enabled": False, "jobs": []}, api.get(watch, timeout=5).json())
        print("Zoom public PKCE, pagination, recurring UUID, documented preferred view, credential-free CDN,"
              " private 320px playback, automatic archive and revoke passed.")


if __name__ == "__main__":
    unittest.main()
