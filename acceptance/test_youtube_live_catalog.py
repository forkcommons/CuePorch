import base64
import hashlib
import json
import secrets
import sys
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlencode

from flask import Flask, redirect, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
import youtube_live


class YouTubeLiveCatalog(unittest.TestCase):
    def test_connect_catalog_pages_refresh_disconnect(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            remote = Flask("youtube-live-fixture")
            provider = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            origin = f"http://127.0.0.1:{provider.server_port}"
            service = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/youtube-live/callback"
            observed = dict(refresh=False, revoked=False, uploaded=False)
            raw_secrets = ("old-live-access", "fresh-live-access", "live-refresh", "fixture-secret")
            cleanup.enter_context(patch.multiple(
                youtube_live,
                OAUTH=replace(
                    youtube_live.OAUTH,
                    authorization_url=origin + "/oauth/authorize",
                    token_url=origin + "/oauth/token",
                    revoke_url=origin + "/oauth/revoke",
                ),
                API=origin,
            ))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_PUBLIC_URL": "",
                "CUEPORCH_YOUTUBE_LIVE_CLIENT_ID": "cueporch",
                "CUEPORCH_YOUTUBE_LIVE_CLIENT_SECRET": "fixture-secret",
            }))
            operator = secrets.token_urlsafe(32)
            database = root / "workspace.sqlite"
            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, database, operator, "station")
            stops = [stop for name, stop in app.extensions.items() if name.startswith("stop_")]

            def stop_app():
                while stops:
                    stops.pop()()

            cleanup.callback(stop_app)
            service.app = app

            @remote.get("/oauth/authorize")
            def authorize():
                assert request.args["client_id"] == "cueporch"
                assert request.args["redirect_uri"] == callback
                assert request.args["scope"] == youtube_live.OAUTH.scope
                assert request.args["code_challenge_method"] == "S256"
                observed["challenge"] = request.args["code_challenge"]
                return redirect(callback + "?" + urlencode(dict(state=request.args["state"], code="live-code")))

            @remote.post("/oauth/token")
            def token():
                assert request.form["client_id"] == "cueporch"
                assert request.form["client_secret"] == "fixture-secret"
                if request.form["grant_type"] == "authorization_code":
                    assert request.form["code"] == "live-code"
                    assert request.form["redirect_uri"] == callback
                    verifier = request.form["code_verifier"]
                    assert verifier
                    challenge = base64.urlsafe_b64encode(
                        hashlib.sha256(verifier.encode()).digest()
                    ).rstrip(b"=").decode()
                    assert challenge == observed["challenge"]
                    access, refresh = "old-live-access", "live-refresh"
                else:
                    assert request.form["grant_type"] == "refresh_token"
                    assert request.form["refresh_token"] == "live-refresh"
                    observed["refresh"] = True
                    access, refresh = "fresh-live-access", "live-refresh"
                return dict(
                    access_token=access,
                    refresh_token=refresh,
                    scope="https://www.googleapis.com/auth/youtube.force-ssl",
                    token_type="Bearer",
                    expires_in=3600,
                )

            @remote.post("/oauth/revoke")
            def revoke():
                assert request.form["token"] == "live-refresh"
                observed["revoked"] = True
                return "", 200

            @remote.post("/upload")
            def upload():
                observed["uploaded"] = True
                return "", 200

            @remote.get("/channels")
            def channels():
                assert request.args["mine"] == "true"
                assert request.args["part"] == "id,snippet"
                assert request.args["maxResults"] == "50"
                if observed["revoked"]:
                    return "revoked", 401
                auth = request.headers["Authorization"]
                if auth == "Bearer old-live-access":
                    return "expired", 401
                assert auth == "Bearer fresh-live-access"
                if request.args.get("pageToken") == "live-page-2":
                    return {"items": [{"id": "UC-live-2", "snippet": {"title": "Porch Live Two"}}]}
                return {
                    "items": [{"id": "UC-live-1", "snippet": {"title": "Porch Live One"}}],
                    "nextPageToken": "live-page-2",
                }

            cleanup.enter_context(service_worker(provider))
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                api = page.context.request
                self.assertEqual(403, api.get(base + "/api/integrations/youtube-live/channels").status)
                page.add_init_script(
                    f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator)});"
                )
                page.goto(base + "/operator/integrations")
                expect(page.get_by_role("heading", name="YouTube Live")).to_be_visible()
                page.get_by_role("button", name="Connect YouTube Live", exact=True).click()
                expect(page.locator("#integrationStatus")).to_contain_text("YouTube Live is connected.", timeout=15000)
                listed = api.get(
                    base + "/api/integrations/youtube-live/channels",
                    headers={"Authorization": f"Bearer {operator}"},
                )
                self.assertEqual(200, listed.status)
                for secret in raw_secrets:
                    self.assertNotIn(secret, listed.text())
                page.get_by_role("button", name="Show YouTube channels", exact=True).click()
                expect(page.get_by_text("Porch Live One")).to_be_visible(timeout=15000)
                page.get_by_role("button", name="More YouTube channels", exact=True).click()
                expect(page.get_by_text("Porch Live Two")).to_be_visible(timeout=15000)
                self.assertTrue(observed["refresh"])
                for secret in raw_secrets:
                    self.assertNotIn(secret, page.content())
                page.get_by_role("button", name="Disconnect YouTube Live", exact=True).click()
                expect(page.locator("#integrationStatus")).to_contain_text("YouTube Live disconnected.", timeout=15000)
                self.assertTrue(observed["revoked"])
                after = api.get(
                    base + "/api/integrations/youtube-live/channels",
                    headers={"Authorization": f"Bearer {operator}"},
                )
                self.assertEqual(409, after.status)
                self.assertFalse(observed["uploaded"])

                invite = api.post(base + "/api/team/invitations",
                    headers={"Authorization": "Bearer " + operator, "X-CuePorch-Team": "1"}, data={"email": "admin@example.org", "role": "admin"})
                self.assertEqual(invite.status, 201)
                invite_token = invite.json()["path"].split("#", 1)[1]
                with browser.new_context() as team:
                    joined = team.request.post(base + "/api/team/join",
                        headers={"X-CuePorch-Team": "1"}, data={"invitation": invite_token, "name": "Admin", "password": "CuePorch team rehearsal password!"})
                    self.assertEqual(joined.status, 201)
                    connect = team.request.post(base + "/api/integrations/youtube-live/connect",
                        headers={"Authorization": "Bearer team-session", "X-CuePorch-Integration": "1"}, data={})
                    self.assertEqual(connect.status, 200)
                    authorization_url = connect.json()[
                        "authorization_url"]
                    signed_out = team.request.delete(base + "/api/team/session",
                        headers={"X-CuePorch-Team": "1"}, data={})
                    self.assertTrue(signed_out.ok)
                    oauth_page = team.new_page()
                    oauth_response = oauth_page.goto(authorization_url)
                    self.assertEqual(oauth_response.status, 403)
                    oauth_page.close()
                channels = api.get(base + "/api/integrations/youtube-live/channels",
                    headers={"Authorization": "Bearer " + operator, "X-CuePorch-Team": "1"})
                self.assertEqual(channels.status, 409)
        print("YouTube Live OAuth PKCE, catalog page one/two via browser, 409 after disconnect and revoke passed.")


if __name__ == "__main__":
    unittest.main()
