"""Verify cloud import and playback against an HTTP Google Drive contract fixture."""

import base64
from contextlib import ExitStack, closing
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from flask import Flask, Response, redirect, request
from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import WSGIRequestHandler, make_server

from browser import chromium
from test_webinar_email import service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
import library_integrations as drive
from workspace_oauth import SCHEMA, migrate_drive_connection


class QuietHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        pass


class DriveImport(unittest.TestCase):
    def test_preserve_existing_encrypted_connection(self):
        with closing(sqlite3.connect(":memory:")) as database:
            database.executescript(SCHEMA + """
                CREATE TABLE workspace_drive_connection(id INTEGER PRIMARY KEY, revision TEXT, token BLOB);
                INSERT INTO workspace_drive_connection VALUES(1,'prior',X'123456');
            """)
            migrate_drive_connection(database)
            migrate_drive_connection(database)
            self.assertEqual(
                [("google-drive", "prior", bytes.fromhex("123456"))],
                database.execute("SELECT * FROM workspace_integration_connection").fetchall(),
            )

    def test_private_import_from_directory(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264",
                            "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            media = clip.read_bytes()
            row = dict(id="video_1", name="Drive screening.mp4", mimeType="video/mp4", version="7",
                       size=str(len(media)), md5Checksum=hashlib.md5(media, usedforsecurity=False).hexdigest(),
                       capabilities=dict(canDownload=True))
            observed = {"refreshes": 0, "downloads": 0, "revocations": 0, "tamper": False, "rate_limit": False}
            provider = Flask("mock-drive")
            provider_server = make_server("127.0.0.1", 0, provider, threaded=True, request_handler=QuietHandler)
            provider_url = f"http://127.0.0.1:{provider_server.server_port}"
            cleanup.enter_context(patch.multiple(
                drive, AUTH=f"http://localhost:{provider_server.server_port}/auth", TOKEN_URL=provider_url + "/token",
                REVOKE=provider_url + "/revoke", FILES=provider_url + "/files",
            ))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_GOOGLE_DRIVE_CLIENT_ID": "fixture-client",
                "CUEPORCH_GOOGLE_DRIVE_CLIENT_SECRET": "fixture-secret",
                "CUEPORCH_PUBLIC_URL": "",
            }))
            operator = secrets.token_urlsafe(32)
            database = root / "workspace.sqlite"
            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, database, operator, "station")
            for name, stop in app.extensions.items():
                if name.startswith("stop_"):
                    cleanup.callback(stop)
            service = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + drive.CALLBACK

            @provider.get("/auth")
            def authorize():
                args = request.args
                assert args["response_type"] == "code" and args["scope"] == drive.SCOPE
                assert args["access_type"] == "offline" and args["code_challenge_method"] == "S256"
                assert args["redirect_uri"] == callback
                observed["challenge"] = args["code_challenge"]
                observed["state"] = args["state"]
                return redirect(callback + "?" + urlencode(dict(state=args["state"], code="fixture-code")))

            @provider.post("/token")
            def token():
                form = request.form
                assert form["client_secret"] == "fixture-secret" and form["client_id"] == "fixture-client"
                if form["grant_type"] == "authorization_code":
                    verifier = hashlib.sha256(form["code_verifier"].encode()).digest()
                    assert base64.urlsafe_b64encode(verifier).decode().rstrip("=") == observed["challenge"]
                    assert form["redirect_uri"] == callback and form["code"] == "fixture-code"
                    access, refresh = "expired-access", "initial-refresh"
                else:
                    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "initial-refresh"
                    observed["refreshes"] += 1
                    access, refresh = "current-access", "rotated-refresh"
                return dict(access_token=access, refresh_token=refresh, expires_in=3600,
                            token_type="Bearer", scope=drive.SCOPE)

            @provider.post("/revoke")
            def revoke():
                assert request.form["token"] == "rotated-refresh"
                observed["revocations"] += 1
                return "", 200

            @provider.get("/files")
            @provider.get("/files/<item>")
            def files(item=None):
                credential = request.headers["Authorization"]
                if credential == "Bearer expired-access":
                    return "", 401
                assert credential == "Bearer current-access"
                assert request.args["supportsAllDrives"] == "true"
                if observed["rate_limit"]:
                    return "", 429, {"Retry-After": "10"}
                if not item:
                    assert "trashed = false" in request.args["q"]
                    if request.args.get("pageToken") == "second":
                        return dict(files=[row])
                    return dict(files=[row | dict(capabilities=dict(canDownload=False))],
                                nextPageToken="second")
                if item != row["id"]:
                    return "", 404
                if request.args.get("alt") == "media":
                    observed["downloads"] += 1
                    return Response(media[:-1] + b"\xff" if observed["tamper"] else media, mimetype="video/mp4")
                return row

            cleanup.enter_context(service_worker(provider_server))
            cleanup.enter_context(service_worker(service))
            session = cleanup.enter_context(requests.Session())
            session.headers.update({"Authorization": "Bearer " + operator, drive.MARKER: "1"})
            endpoints = base + "/api/integrations/google-drive"
            anonymous = requests.get(base + "/api/integrations", timeout=5)
            self.assertEqual(403, anonymous.status_code)
            self.assertIn("no-store", anonymous.headers["Cache-Control"])
            rejected = session.post(endpoints + "/connect", json={}, timeout=5,
                                    headers={"Origin": "https://unrelated.example"})
            self.assertEqual(403, rejected.status_code)
            pending = session.post(endpoints + "/connect", json={}, timeout=5)
            pending.raise_for_status()
            state = parse_qs(urlsplit(pending.json()["authorization_url"]).query)["state"][0]
            wrong_browser = requests.get(callback, params=dict(state=state, code="fixture-code"), timeout=5)
            self.assertEqual(403, wrong_browser.status_code)
            cancelled = session.get(callback, params=dict(state=state, error="access_denied"),
                                    allow_redirects=False, timeout=5)
            self.assertEqual("/operator/integrations?connection=google-drive&result=cancelled", cancelled.headers["Location"])
            folder = session.post(base + "/api/library/folders",
                                  json=dict(name="Festival", color="#75198a", parent=None), timeout=5)
            folder.raise_for_status()
            folder_id = folder.json()["id"]
            folder_rule = session.put(base + f"/api/library/folders/{folder_id}/privacy", timeout=5,
                json=dict(enabled=True, visibility="public", embed="anywhere", domains=[], download=True))
            folder_rule.raise_for_status()
            self.assertEqual("public", folder_rule.json()["settings"]["visibility"])

            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                page.goto(base + "/operator/integrations")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open integrations").click()
                expect(page.get_by_role("heading", name="Google Drive")).to_be_visible()
                page.get_by_role("button", name="Connected", exact=True).click()
                expect(page.get_by_text("No integrations match.")).to_be_visible()
                page.get_by_role("button", name="Discover").click()
                page.get_by_label("Find an integration").fill("unknown")
                expect(page.get_by_text("No integrations match.")).to_be_visible()
                page.get_by_label("Find an integration").fill("Google")
                page.get_by_label("Category").select_option("Cloud storage")
                page.get_by_role("button", name="Connect Google Drive").click()
                expect(page.locator("#integrationStatus")).to_have_text("Google Drive is connected.")
                replay = page.request.get(callback, params=dict(state=observed["state"], code="fixture-code"))
                self.assertEqual(403, replay.status)
                page.get_by_role("button", name="Connected", exact=True).click()
                page.get_by_role("button", name="Browse videos").click()
                expect(page.get_by_text("No downloadable videos on this page.")).to_be_visible(timeout=15000)
                page.get_by_role("button", name="More videos").click()
                page.get_by_label("Library destination").select_option(label="Festival")
                picker = page.get_by_role("button", name="Import Drive screening.mp4")
                expect(picker).to_be_visible()
                self.assertEqual(1, observed["refreshes"])
                observed["rate_limit"] = True
                busy = session.get(endpoints + "/files", timeout=5)
                self.assertEqual((429, "10"), (busy.status_code, busy.headers["Retry-After"]))
                observed["rate_limit"] = False
                observed["tamper"] = True
                picker.click()
                expect(page.locator("#integrationStatus")).to_contain_text("did not match")
                self.assertEqual([], json.loads((root / "manifest.json").read_text())["works"])
                observed["tamper"] = False
                picker.click()
                announce = page.locator("#driveReceipt")
                expect(announce).to_have_text("Library video: Drive screening.mp4 (private)", timeout=30000)
                detail = announce.get_by_role("link")
                work = detail.get_attribute("href").split("/")[-1]
                duplicate = session.post(endpoints + "/import",
                    json=dict(file=row["id"], folder=folder_id), timeout=30)
                self.assertEqual((200, work, "private"),
                                 (duplicate.status_code, duplicate.json()["id"], duplicate.json()["visibility"]))
                self.assertEqual(3, observed["downloads"])
                with closing(sqlite3.connect(database)) as con:
                    self.assertEqual((folder_id,), con.execute(
                        "SELECT folder FROM library_placement WHERE work=?", (work,),
                    ).fetchone())
                    encrypted, = con.execute(
                        "SELECT token FROM workspace_integration_connection WHERE provider='google-drive'",
                    ).fetchone()
                    self.assertNotIn(b"rotated-refresh", encrypted)
                self.assertEqual(404, requests.get(base + f"/watch/local/{work}", timeout=5).status_code)
                detail.click()
                preview = page.locator("#workspacePreview")
                expect(preview).to_be_visible()
                self.assertEqual(320, preview.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}"))
                page.goto(base + "/operator/integrations")
                page.get_by_role("button", name="Disconnect Google Drive").click()
                expect(page.locator("#integrationStatus")).to_have_text("Google Drive disconnected.")
                self.assertEqual(1, observed["revocations"])
                self.assertEqual(409, session.get(endpoints + "/files", timeout=5).status_code)
                page.goto(base + f"/operator/videos/{work}")
                expect(preview).to_be_visible()
                self.assertEqual(320, preview.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}"))
            print("Google Drive OAuth, one-use state, PKCE, refresh, pagination, throttling, checksum rejection,"
                  " private Library placement, same-revision retry, 320px playback and disconnect passed.")


if __name__ == "__main__":
    unittest.main()
