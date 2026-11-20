"""Rehearse a Dropbox folder listing and verified media ingest in a real browser."""

import base64
from contextlib import ExitStack
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

from flask import Flask, Response, redirect, request
from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
import library_integrations as cloud


class DropboxImport(unittest.TestCase):
    def test_content_hash_across_blocks(self):
        first = b"1" * 4_194_304
        second = b"two"
        expected = hashlib.sha256(hashlib.sha256(first).digest() + hashlib.sha256(second).digest())
        digest = cloud.DropboxHash()
        digest.update(first[:-1])
        digest.update(first[-1:] + second)
        self.assertEqual(expected.hexdigest(), digest.hexdigest())

    def test_import_video_from_nested_folder(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264",
                            "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            media = clip.read_bytes()
            content_hash = hashlib.sha256(hashlib.sha256(media).digest()).hexdigest()

            source_folder = {".tag": "folder", "name": "Festivals", "id": "id:festival"}
            row = {".tag": "file", "name": "Opening.mp4", "id": "id:opening",
                   "rev": "aaaaa0000000000000099", "size": len(media), "content_hash": content_hash}
            observed = {"revocations": 0, "downloads": 0}
            provider = Flask("mock-dropbox")
            provider_server = make_server("127.0.0.1", 0, provider, threaded=True, request_handler=QuietHandler)
            provider_url = f"http://127.0.0.1:{provider_server.server_port}"
            cleanup.enter_context(patch.multiple(
                cloud, DROPBOX_AUTH=f"http://localhost:{provider_server.server_port}/oauth2/authorize",
                DROPBOX_TOKEN=provider_url + "/oauth2/token", DROPBOX_RPC=provider_url + "/2",
                DROPBOX_CONTENT=provider_url + "/2",
            ))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_DROPBOX_CLIENT_ID": "fixture-client",
                "CUEPORCH_DROPBOX_CLIENT_SECRET": "fixture-secret",
                "CUEPORCH_GOOGLE_DRIVE_CLIENT_ID": "",
                "CUEPORCH_GOOGLE_DRIVE_CLIENT_SECRET": "",
                "CUEPORCH_PUBLIC_URL": "",
            }))
            operator = secrets.token_urlsafe(32)
            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, root / "workspace.sqlite", operator, "station")
            for name, stop in app.extensions.items():
                if name.startswith("stop_"):
                    cleanup.callback(stop)
            service = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/dropbox/callback"

            @provider.get("/oauth2/authorize")
            def authorize():
                args = request.args
                assert args["client_id"] == "fixture-client" and args["response_type"] == "code"
                assert args["scope"] == cloud.DROPBOX_SCOPE and args["redirect_uri"] == callback
                assert args["token_access_type"] == "offline" and args["code_challenge_method"] == "S256"
                observed["challenge"] = args["code_challenge"]
                return redirect(callback + "?" + urlencode(dict(state=args["state"], code="fixture-code")))

            @provider.post("/oauth2/token")
            def token():
                form = request.form
                assert form["client_secret"] == "fixture-secret" and form["client_id"] == "fixture-client"
                assert form["grant_type"] == "authorization_code" and form["code"] == "fixture-code"
                verifier = hashlib.sha256(form["code_verifier"].encode()).digest()
                assert base64.urlsafe_b64encode(verifier).decode().rstrip("=") == observed["challenge"]
                return dict(access_token="fixture-access", refresh_token="fixture-refresh", expires_in=3600,
                            token_type="bearer", scope=cloud.DROPBOX_SCOPE)

            @provider.before_request
            def require_access():
                if request.path.startswith("/2/"):
                    assert request.headers["Authorization"] == "Bearer fixture-access"

            @provider.post("/2/files/list_folder")
            def list_folder():
                assert request.json["recursive"] is False
                if request.json["path"] == source_folder["id"]:
                    return dict(entries=[], cursor="dropbox-next-page", has_more=True)
                assert request.json["path"] == ""
                return dict(entries=[source_folder], cursor="root-page", has_more=False)

            @provider.post("/2/files/list_folder/continue")
            def list_more():
                assert request.json == {"cursor": "dropbox-next-page"}
                return dict(entries=[row], cursor="dropbox-last-page", has_more=False)

            @provider.post("/2/files/get_metadata")
            def metadata():
                assert request.json["path"] == row["id"]
                return row

            @provider.post("/2/files/download")
            def download():
                assert json.loads(request.headers["Dropbox-API-Arg"]) == {"path": "rev:" + row["rev"]}
                observed["downloads"] += 1
                return Response(media, mimetype="application/octet-stream")

            @provider.post("/2/auth/token/revoke")
            def revoke():
                observed["revocations"] += 1
                return "", 200

            cleanup.enter_context(service_worker(provider_server))
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                page.goto(base + "/operator/integrations")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open integrations").click()
                expect(page.get_by_role("heading", name="Dropbox", exact=True)).to_be_visible()
                page.get_by_role("button", name="Connect Dropbox", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("Dropbox is connected.")
                page.get_by_role("button", name="Browse videos").click()
                page.get_by_role("button", name="Open folder Festivals").click()
                expect(page.locator("#cloudFolderPath")).to_have_text("Dropbox / Festivals")
                page.get_by_role("button", name="Parent folder").click()
                expect(page.locator("#cloudFolderPath")).to_have_text("Dropbox")
                page.get_by_role("button", name="Open folder Festivals").click()
                page.get_by_role("button", name="More videos").click()
                page.get_by_role("button", name="Import Opening.mp4").click()
                receipt = page.locator("#driveReceipt")
                expect(receipt).to_have_text("Library video: Opening.mp4 (private)", timeout=30000)
                self.assertEqual(1, observed["downloads"])
                detail = receipt.get_by_role("link")
                work = detail.get_attribute("href").split("/")[-1]
                self.assertEqual(404, requests.get(base + f"/watch/local/{work}", timeout=5).status_code)
                detail.click()
                preview = page.locator("#workspacePreview")
                expect(preview).to_be_visible()
                self.assertEqual(320, preview.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}"))
                page.goto(base + "/operator/integrations")
                page.get_by_role("button", name="Disconnect Dropbox").click()
                expect(page.locator("#integrationStatus")).to_have_text("Dropbox disconnected.")
                self.assertEqual(1, observed["revocations"])
        print("Dropbox folder navigation, paginated video listing, revision download, content hash, "
              "private Library playback at 320px and disconnect passed.")


if __name__ == "__main__":
    unittest.main()
