"""Exercise privately importing and automatically archiving Webex recordings."""

import base64
from contextlib import closing, ExitStack
from datetime import datetime, timezone
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
from urllib.parse import urlencode, urlsplit

from flask import Flask, Response, redirect, request
from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import make_server
from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
import webex_recordings as webex


class WebexImport(unittest.TestCase):
    def test_recording_download_and_automatic_archive(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264",
                            "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            media = clip.read_bytes()
            remote = Flask("mock-webex")
            provider = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            provider_url = f"http://127.0.0.1:{provider.server_port}"
            observed = dict(downloads=[], automatic=[], refreshed=False, foreign_cursor=False)
            request_media = webex.provider_request

            def fixture_media(method, url, **fields):
                self.assertEqual("recordings.webex.com", urlsplit(url).hostname)
                return request_media(method, provider_url + urlsplit(url).path, **fields)

            cleanup.enter_context(patch.multiple(
                webex, API=provider_url + "/v1", POLL_SECONDS=.25, provider_request=fixture_media,
            ))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_WEBEX_CLIENT_ID": "fixture-webex", "CUEPORCH_WEBEX_CLIENT_SECRET": "fixture-secret",
                "CUEPORCH_PUBLIC_URL": "",
            }))
            operator = secrets.token_urlsafe(32)
            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, root / "workspace.sqlite", operator, "station")
            for name, stop in app.extensions.items():
                if name.startswith("stop_"):
                    cleanup.callback(stop)
            service = make_server("127.0.0.1", 0, app, request_handler=QuietHandler, threaded=True)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/webex/callback"

            @remote.get("/v1/authorize")
            def consent():
                assert request.args["client_id"] == "fixture-webex" and request.args["redirect_uri"] == callback
                assert request.args["code_challenge_method"] == "S256" and request.args["scope"] == webex.SCOPE
                observed["challenge"] = request.args["code_challenge"]
                return redirect(callback + "?" + urlencode(dict(state=request.args["state"], code="webex-code")))

            @remote.post("/v1/access_token")
            def token():
                assert request.form["client_id"] == "fixture-webex" and request.form["client_secret"] == "fixture-secret"
                if request.form["grant_type"] == "authorization_code":
                    assert request.form["redirect_uri"] == callback and request.form["code"] == "webex-code"
                    hashed = hashlib.sha256(request.form["code_verifier"].encode()).digest()
                    assert base64.urlsafe_b64encode(hashed).decode().rstrip("=") == observed["challenge"]
                    access = "old-access"
                else:
                    assert request.form["refresh_token"] == "fixture-refresh" and not observed["refreshed"]
                    observed["refreshed"] = True
                    access = "fresh-access"
                return dict(access_token=access, refresh_token="fixture-refresh", scope=webex.SCOPE,
                            expires_in=1209600, token_type="Bearer")

            def recording(uid, created, shared=False):
                return dict(id=uid, topic="Community planning", createTime=created, format="MP4", status="available",
                            shareToMe=shared, sizeBytes=len(media), meetingSeriesId="weekly-planning")

            first = recording("first", "2026-11-17T16:00:00Z")
            second = recording("second", "2026-11-18T16:00:00Z")

            @remote.get("/v1/recordings")
            def recordings():
                if request.headers["Authorization"] == "Bearer old-access":
                    return "", 401
                assert request.headers["Authorization"] == "Bearer fresh-access"
                if request.args.get("after") == "last":
                    return dict(items=[first])
                assert request.args["format"] == "MP4" and "from" in request.args and "to" in request.args
                origin = "https://wrong.invalid" if observed["foreign_cursor"] else provider_url
                link = f'<{origin}/v1/recordings?after=last>; rel="next"'
                return dict(items=[second, *observed["automatic"]]), 200, {"Link": link}

            @remote.get("/v1/recordings/<uid>")
            def details(uid):
                assert request.headers["Authorization"] == "Bearer fresh-access"
                row = next(item for item in [first, second, *observed["automatic"]] if item["id"] == uid)
                return row | dict(temporaryDirectDownloadLinks={
                    "recordingDownloadLink": "https://recordings.webex.com/signed/" + uid,
                })

            @remote.get("/signed/<uid>")
            def signed(uid):
                assert "Authorization" not in request.headers
                return redirect("https://recordings.webex.com/content/" + uid)

            @remote.get("/content/<uid>")
            def content(uid):
                assert "Authorization" not in request.headers
                observed["downloads"].append(uid)
                return Response(media, mimetype="video/mp4")

            cleanup.enter_context(service_worker(provider))
            cleanup.enter_context(service_worker(service))
            api = cleanup.enter_context(requests.Session())
            api.headers["Authorization"] = "Bearer " + operator
            watch = base + "/api/integrations/webex/watch"
            files = base + "/api/integrations/webex/files"
            folder = api.post(base + "/api/library/folders", timeout=5,
                              json=dict(name="Archived meetings", color="#75198a", parent=None))
            folder.raise_for_status()
            folder_id = folder.json()["id"]

            def archived(count):
                deadline = monotonic() + 15
                while monotonic() < deadline:
                    jobs = api.get(watch, timeout=5).json()["jobs"]
                    if len(jobs) == count and all(job["state"] == "imported" for job in jobs):
                        return jobs
                    sleep(.1)
                self.fail(f"Expected {count} completed Webex jobs, got {jobs!r}.")

            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                page.goto(base + "/operator/integrations")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open integrations").click()
                page.get_by_role("button", name="Connect Webex", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("Webex is connected.", timeout=15000)
                page.get_by_role("button", name="Browse recordings").click()
                page.get_by_role("button", name="More videos").click()
                page.get_by_role("button", name="Import Community planning · 2026-11-17 1600 UTC").click()
                receipt = page.locator("#driveReceipt")
                expect(receipt).to_contain_text("(private)", timeout=30000)
                self.assertTrue(observed["refreshed"])
                self.assertEqual(["first"], observed["downloads"])
                receipt.get_by_role("link").click()
                video = page.locator("#workspacePreview")
                expect(video).to_be_visible()
                self.assertEqual(320, video.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}"))

                observed["foreign_cursor"] = True
                self.assertEqual(502, api.get(files, timeout=5).status_code)
                observed["foreign_cursor"] = False
                page.goto(base + "/operator/integrations")
                page.get_by_role("button", name="Browse recordings").click()
                page.get_by_role("button", name="Enable automatic archive").click()
                expect(page.locator("#dropboxWatchStatus")).to_contain_text("Archiving new Webex recordings.")
                now = datetime.now(timezone.utc).isoformat()
                observed["automatic"] = [recording("automatic", now), recording("shared", now, shared=True)]
                previous = archived(1)[0]
                self.assertEqual(["first", "automatic"], observed["downloads"])
                page.get_by_label("Library destination").select_option(folder_id)
                page.get_by_role("button", name="Change archive destination").click()
                expect(page.locator("#dropboxWatchStatus")).to_contain_text("Archived meetings")
                self.assertEqual(previous, archived(1)[0])
                self.assertEqual(["first", "automatic"], observed["downloads"])
                observed["automatic"].append(recording("follow-up", datetime.now(timezone.utc).isoformat()))
                follow_up, retained = archived(2)
                self.assertEqual(previous, retained)
                with closing(sqlite3.connect(root / "workspace.sqlite")) as database:
                    placement = database.execute("SELECT folder FROM library_placement WHERE work=?",
                                                 (follow_up["work"],)).fetchone()
                self.assertEqual((folder_id,), placement)
                page.get_by_role("button", name="Disconnect Webex", exact=True).click()
                expect(page.locator("#integrationStatus")).to_contain_text("remove CuePorch access in your Webex account")
                self.assertEqual(409, api.get(files, timeout=5).status_code)
                self.assertEqual({"enabled": False, "jobs": []}, api.get(watch, timeout=5).json())
        print("Webex PKCE, token refresh, checked continuation, 320px private playback, signed CDN without bearer,"
              " owned-recording automatic archive, destination change with queue retention and disconnect passed.")


if __name__ == "__main__":
    unittest.main()
