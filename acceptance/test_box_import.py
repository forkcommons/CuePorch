"""Exercise the Box OAuth and signed file-download contract in the browser."""

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
from urllib.parse import urlencode, urlsplit

from flask import Flask, Response, redirect, request
from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
import library_integrations as cloud
from workspace_oauth import provider_request


class BoxImport(unittest.TestCase):
    def test_version_download_and_revocation(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264",
                            "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            media = clip.read_bytes()
            file = dict(id="12", type="file", name="Box screening.mp4", item_status="active",
                        file_version=dict(id="721"), size=len(media), is_download_available=True,
                        permissions=dict(can_download=True), sha1=hashlib.sha1(media, usedforsecurity=False).hexdigest())
            folder = dict(id="8", type="folder", name="Archives")
            observed = dict(refresh=0, revoked=False, transfers=0, unsafe=True)
            provider = Flask("mock-box")
            server = make_server("127.0.0.1", 0, provider, threaded=True, request_handler=QuietHandler)
            provider_url = f"http://127.0.0.1:{server.server_port}"
            signed_path = "/d/1/selected-version/download"
            signed_url = "https://dl.boxcloud.com" + signed_path

            def local_cdn(method, url, **kwargs):
                self.assertEqual(("GET", signed_url), (method, url))
                self.assertNotIn("Authorization", kwargs.get("headers", {}))
                return provider_request(method, provider_url + urlsplit(url).path, **kwargs)

            cleanup.enter_context(patch.multiple(
                cloud, BOX_AUTH=f"http://localhost:{server.server_port}/authorize", BOX_ORIGIN=provider_url,
                provider_request=local_cdn,
            ))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_BOX_CLIENT_ID": "fixture-client", "CUEPORCH_BOX_CLIENT_SECRET": "fixture-secret",
                "CUEPORCH_PUBLIC_URL": "", "CUEPORCH_DROPBOX_CLIENT_ID": "", "CUEPORCH_DROPBOX_CLIENT_SECRET": "",
            }))
            operator = secrets.token_urlsafe(32)
            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, root / "workspace.sqlite", operator, "station")
            for name, stop in app.extensions.items():
                if name.startswith("stop_"):
                    cleanup.callback(stop)
            service = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/box/callback"

            @provider.get("/authorize")
            def consent():
                assert request.args["scope"] == "root_readwrite"
                assert request.args["redirect_uri"] == callback
                return redirect(callback + "?" + urlencode(dict(state=request.args["state"], code="box-code")))

            @provider.post("/oauth2/token")
            def token():
                assert request.form["client_id"] == "fixture-client" and request.form["client_secret"] == "fixture-secret"
                if request.form["grant_type"] == "authorization_code":
                    assert request.form["code"] == "box-code"
                    access, refresh = "expired", "one-use-refresh"
                else:
                    assert request.form["refresh_token"] == "one-use-refresh" and not observed["refresh"]
                    observed["refresh"] += 1
                    access, refresh = "valid", "rotated"
                return dict(access_token=access, refresh_token=refresh, token_type="bearer", expires_in=3600)

            @provider.post("/oauth2/revoke")
            def revoke():
                assert request.form["token"] == "rotated"
                observed["revoked"] = True
                return "", 200

            @provider.before_request
            def authorize_file():
                if request.path.startswith("/2.0/"):
                    if request.headers["Authorization"] == "Bearer expired":
                        return "", 401
                    assert request.headers["Authorization"] == "Bearer valid"
                return None

            @provider.get("/2.0/folders/<source>/items")
            def list_box(source):
                assert request.args["usemarker"] == "true"
                if source == "0":
                    return dict(entries=[folder], next_marker=None)
                assert source == folder["id"]
                if not request.args.get("marker"):
                    return dict(entries=[file | dict(id="13", permissions=dict(can_download=False))],
                                next_marker="next-box-page")
                assert request.args["marker"] == "next-box-page"
                return dict(entries=[file], next_marker=None)

            @provider.get("/2.0/files/12")
            def metadata():
                assert "file_version" in request.args["fields"]
                return file

            @provider.get("/2.0/files/12/content")
            def download():
                assert request.args["version"] == file["file_version"]["id"]
                target = signed_url if not observed["unsafe"] else provider_url + "/should-never-be-called"
                return redirect(target)

            @provider.get(signed_path)
            def cdn():
                assert "Authorization" not in request.headers
                observed["transfers"] += 1
                return Response(media, mimetype="application/octet-stream")

            cleanup.enter_context(service_worker(server))
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                page.goto(base + "/operator/integrations")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open integrations").click()
                page.get_by_role("button", name="Connect Box", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("Box is connected.")
                page.get_by_role("button", name="Browse videos").click()
                page.get_by_role("button", name="Open folder Archives").click()
                expect(page.get_by_text("No downloadable videos on this page.")).to_be_visible()
                page.get_by_role("button", name="More videos").click()
                import_button = page.get_by_role("button", name="Import Box screening.mp4")
                import_button.click()
                expect(page.locator("#integrationStatus")).to_contain_text("unexpected download address")
                self.assertEqual(0, observed["transfers"])
                observed["unsafe"] = False
                import_button.click()
                receipt = page.locator("#driveReceipt")
                expect(receipt).to_have_text("Library video: Box screening.mp4 (private)", timeout=30000)
                self.assertEqual((1, 1), (observed["refresh"], observed["transfers"]))
                detail = receipt.get_by_role("link")
                work = detail.get_attribute("href").split("/")[-1]
                self.assertEqual(404, requests.get(base + f"/watch/local/{work}", timeout=5).status_code)
                detail.click()
                video = page.locator("#workspacePreview")
                expect(video).to_be_visible()
                self.assertEqual(320, video.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}"))
                page.goto(base + "/operator/integrations")
                page.get_by_role("button", name="Disconnect Box", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("Box disconnected.")
                self.assertTrue(observed["revoked"])
        print("Box OAuth, refresh rotation, marker pagination, version download, signed CDN without bearer,"
              " external redirect rejection, private 320px playback and revoke passed.")


if __name__ == "__main__":
    unittest.main()
