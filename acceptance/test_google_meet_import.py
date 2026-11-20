"""Use the Meet and Drive contracts to import a selected conference recording."""

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
from urllib.parse import urlencode

from flask import Flask, Response, redirect, request
from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import google_meet_recordings as meet
from institution import create_app
import library_integrations as cloud


class MeetImport(unittest.TestCase):
    def test_repeat_conferences_are_distinct(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264",
                            "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            media = clip.read_bytes()
            provider = Flask("mock-meet")
            remote = make_server("127.0.0.1", 0, provider, threaded=True, request_handler=QuietHandler)
            provider_url = f"http://127.0.0.1:{remote.server_port}"
            cleanup.enter_context(patch.multiple(
                cloud, AUTH=f"http://localhost:{remote.server_port}/authorize", TOKEN_URL=provider_url + "/token",
                REVOKE=provider_url + "/revoke", FILES=provider_url + "/drive/v3/files",
            ))
            cleanup.enter_context(patch.multiple(meet, API=provider_url + "/v2/", POLL_SECONDS=.25))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_GOOGLE_MEET_CLIENT_ID": "fixture-meet", "CUEPORCH_GOOGLE_MEET_CLIENT_SECRET": "fixture-secret",
                "CUEPORCH_DROPBOX_CLIENT_ID": "", "CUEPORCH_DROPBOX_CLIENT_SECRET": "", "CUEPORCH_PUBLIC_URL": "",
            }))
            operator = secrets.token_urlsafe(32)
            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, root / "workspace.sqlite", operator, "station")
            for name, stop in app.extensions.items():
                if name.startswith("stop_"):
                    cleanup.callback(stop)
            service = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/google-meet/callback"
            dates = {"first": "2026-11-17", "second": "2026-11-20"}
            observed = dict(downloads=[], revoked=False, auto_finished=None, auto_ready=False, auto_listed=False)

            @provider.get("/authorize")
            def consent():
                assert request.args["client_id"] == "fixture-meet" and request.args["scope"] == meet.SCOPES
                assert request.args["redirect_uri"] == callback and request.args["code_challenge_method"] == "S256"
                return redirect(callback + "?" + urlencode(dict(state=request.args["state"], code="meet-code")))

            @provider.post("/token")
            def token():
                assert request.form["client_secret"] == "fixture-secret" and request.form["code"] == "meet-code"
                return dict(access_token="meet-access", refresh_token="meet-refresh", token_type="bearer",
                            expires_in=3600, scope=meet.SCOPES)

            @provider.post("/revoke")
            def revoke():
                assert request.form["token"] == "meet-refresh"
                observed["revoked"] = True
                return "", 200

            @provider.before_request
            def authorize_resource():
                if request.path.startswith(("/v2/", "/drive/")):
                    assert request.headers["Authorization"] == "Bearer meet-access"

            @provider.get("/v2/conferenceRecords")
            def conferences():
                rows = [dict(name=f"conferenceRecords/{occurrence}", space="spaces/weekly",
                             startTime=day + "T12:00:00Z", endTime=day + "T12:30:00Z")
                        for occurrence, day in dates.items()]
                if request.args.get("filter"):
                    assert request.args["filter"].startswith('end_time>="')
                    if observed["auto_finished"]:
                        rows.append(dict(name="conferenceRecords/automatic", space="spaces/weekly",
                                         startTime=observed["auto_finished"], endTime=observed["auto_finished"]))
                    return dict(conferenceRecords=rows)
                if request.args.get("pageToken") == "meet-more":
                    return dict(conferenceRecords=rows[:1])
                return dict(conferenceRecords=rows[1:], nextPageToken="meet-more")

            @provider.get("/v2/conferenceRecords/<occurrence>/recordings")
            def list_recordings(occurrence):
                return dict(recordings=[recording(occurrence),
                    {"name": f"conferenceRecords/{occurrence}/recordings/preparing", "state": "ENDED"}])

            @provider.get("/v2/conferenceRecords/<occurrence>/recordings/ready")
            def recording(occurrence):
                if occurrence == "automatic":
                    observed["auto_listed"] = True
                    return dict(name="conferenceRecords/automatic/recordings/ready",
                                state="FILE_GENERATED" if observed["auto_ready"] else "ENDED",
                                startTime=observed["auto_finished"], driveDestination={"file": "meet_automatic"})
                return dict(name=f"conferenceRecords/{occurrence}/recordings/ready", state="FILE_GENERATED",
                            startTime=dates[occurrence] + "T12:02:00Z",
                            driveDestination={"file": "meet_" + occurrence})

            @provider.get("/drive/v3/files/<item>")
            def media_file(item):
                assert item.startswith("meet_")
                if request.args.get("alt") == "media":
                    observed["downloads"].append(item)
                    return Response(media, mimetype="video/mp4")
                return dict(id=item, name=f"Weekly program {item[5:]}.mp4", mimeType="video/mp4", version="1",
                            size=str(len(media)), sha256Checksum=hashlib.sha256(media).hexdigest(),
                            capabilities=dict(canDownload=True))

            cleanup.enter_context(service_worker(remote))
            cleanup.enter_context(service_worker(service))
            api = cleanup.enter_context(requests.Session())
            api.headers.update({"Authorization": "Bearer " + operator})
            watch_api = base + "/api/integrations/google-meet/watch"

            def wait_for(check):
                deadline = monotonic() + 18
                while monotonic() < deadline:
                    result = api.get(watch_api, timeout=5)
                    result.raise_for_status()
                    receipt = result.json()
                    if check(receipt):
                        return receipt
                    sleep(.1)
                self.fail("The Meet archive did not reach the expected state.")

            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                page.goto(base + "/operator/integrations")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open integrations").click()
                page.get_by_label("Category").select_option("Communications")
                page.get_by_role("button", name="Connect Google Meet").click()
                expect(page.locator("#integrationStatus")).to_have_text("Google Meet is connected.")
                page.get_by_role("button", name="Browse recordings").click()
                page.get_by_role("button", name="More videos").click()
                expect(page.get_by_role("button", name="Open Meeting · 2026-11-17 12:00 UTC")).to_be_visible()
                page.get_by_role("button", name="Open Meeting · 2026-11-20 12:00 UTC").click()
                candidates = page.locator("#driveVideos").get_by_role("button")
                expect(candidates).to_have_count(1)
                page.get_by_role("button", name="Import Recording · 2026-11-20 12:02 UTC").click()
                receipt = page.locator("#driveReceipt")
                expect(receipt).to_contain_text("(private)", timeout=30000)
                self.assertEqual(["meet_second"], observed["downloads"])
                receipt.get_by_role("link").click()
                video = page.locator("#workspacePreview")
                expect(video).to_be_visible()
                self.assertEqual(320, video.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}"))
                page.goto(base + "/operator/integrations")
                page.get_by_role("button", name="Browse recordings").click()
                page.get_by_role("button", name="Enable automatic archive").click()
                expect(page.locator("#dropboxWatchStatus")).to_contain_text("Archiving new Google Meet recordings.")
                baseline = wait_for(lambda receipt: receipt.get("checked"))
                self.assertEqual([], baseline["jobs"])
                observed["auto_finished"] = datetime.now(timezone.utc).isoformat()
                wait_for(lambda receipt: observed["auto_listed"])
                self.assertEqual([], api.get(watch_api, timeout=5).json()["jobs"])
                self.assertEqual(["meet_second"], observed["downloads"])
                observed["auto_ready"] = True
                automated = wait_for(lambda receipt: any(job["state"] == "imported" for job in receipt["jobs"]))
                self.assertEqual(["meet_second", "meet_automatic"], observed["downloads"])
                self.assertEqual(1, len(automated["jobs"]))
                page.get_by_role("button", name="Refresh watch status").click()
                page.get_by_role("link", name="Meeting recording", exact=False).click()
                expect(video).to_be_visible()
                self.assertEqual(320, video.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}"))
                page.goto(base + "/operator/integrations")
                page.get_by_role("button", name="Disconnect Google Meet").click()
                expect(page.locator("#integrationStatus")).to_have_text("Google Meet disconnected.")
                self.assertTrue(observed["revoked"])
                self.assertEqual({"enabled": False, "jobs": []}, api.get(watch_api, timeout=5).json())
                result = requests.get(base + "/api/integrations/google-meet/files", timeout=5,
                                      headers={"Authorization": "Bearer " + operator})
                self.assertEqual(409, result.status_code)
        print("Meet OAuth scope, repeated conference pagination, selected download, generated-only automatic"
              " archive, private 320px playback and revoke passed.")


if __name__ == "__main__":
    unittest.main()
