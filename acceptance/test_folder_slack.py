"""Exercise folder settings and delivery through a controlled Slack HTTPS endpoint."""

from contextlib import ExitStack, closing
from dataclasses import replace
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
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from flask import Flask, redirect, request
from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import make_server

from browser import chromium
from installed_simulcast import tls_certificate
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
import folder_slack


class FolderSlack(unittest.TestCase):
    def test_scoped_notices_retry_and_disconnect(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "Confidential title.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264",
                            "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            cert, key = tls_certificate(root)
            remote = Flask("slack-fixture")
            delivery_server = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True,
                                          ssl_context=(str(cert), str(key)))
            service = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True,
                                  ssl_context=(str(cert), str(key)))
            base = f"https://127.0.0.1:{service.server_port}"
            slack = f"https://127.0.0.1:{delivery_server.server_port}"
            callback = base + "/integration/oauth/slack/callback"
            hook = "https://hooks.slack.com/services/T123/B456/FixtureCredential012345"
            delivered, attempts = [], []
            exchanges = []

            @remote.post("/slack")
            def notice():
                assert "Authorization" not in request.headers
                data = request.get_json()
                attempts.append(data)
                if len(attempts) == 1:
                    return "rate_limited", 429, {"Retry-After": "1"}
                delivered.append(data)
                return "ok"

            @remote.get("/oauth/authorize")
            def authorize():
                assert request.args["client_id"] == "cueporch" and request.args["scope"] == "incoming-webhook"
                assert request.args["redirect_uri"] == callback
                return redirect(callback + "?" + urlencode(dict(state=request.args["state"], code="fixture-code")))

            @remote.post("/oauth/access")
            def access():
                assert request.form["client_id"] == "cueporch" and request.form["client_secret"] == "fixture-secret"
                assert request.form["redirect_uri"] == callback and request.form["code"] == "fixture-code"
                exchanges.append(request.form["code"])
                return dict(ok=True, scope="incoming-webhook", token_type="bot", access_token="discarded-token",
                            incoming_webhook={"channel": "#festival", "url": hook})

            original_post = requests.Session.post

            def route_slack(session, address, **options):
                self.assertEqual(address, hook)
                self.assertFalse(options["allow_redirects"])
                self.assertFalse(session.trust_env)
                return original_post(session, f"https://127.0.0.1:{delivery_server.server_port}/slack",
                                     verify=str(cert), **options)

            cleanup.enter_context(patch.object(folder_slack.requests.Session, "post", route_slack))
            cleanup.enter_context(patch.object(folder_slack, "RETRY_SECONDS", (1, 1)))
            cleanup.enter_context(patch.object(folder_slack, "OAUTH", replace(
                folder_slack.OAUTH, authorization_url=slack + "/oauth/authorize", token_url=slack + "/oauth/access",
            )))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_PUBLIC_URL": base, "CUEPORCH_SLACK_CLIENT_ID": "cueporch",
                "CUEPORCH_SLACK_CLIENT_SECRET": "fixture-secret", "REQUESTS_CA_BUNDLE": str(cert),
            }))
            operator = secrets.token_urlsafe(32)
            database = root / "workspace.sqlite"
            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, database, operator, "station")
            for name, stop in app.extensions.items():
                if name.startswith("stop_"):
                    cleanup.callback(stop)
            service.app = app
            cleanup.enter_context(service_worker(service))
            cleanup.enter_context(service_worker(delivery_server))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page(ignore_https_errors=True)
                api = page.context.request
                auth = {"Authorization": "Bearer " + operator}

                def folder(name):
                    result = api.post(base + "/api/library/folders", headers=auth,
                                      data=dict(name=name, parent=None, color="#75198a"))
                    self.assertEqual(201, result.status)
                    return result.json()["id"]

                def upload(destination):
                    result = api.post(base + "/api/library/uploads", headers=auth, multipart={
                        "folder": destination, "upload_id": str(uuid4()),
                        "file": {"name": clip.name, "mimeType": "video/mp4", "buffer": clip.read_bytes()},
                    }, timeout=30000)
                    self.assertEqual(201, result.status, result.text())
                    return result.json()["id"]

                watched, others = folder("Festival edits"), folder("Other work")
                path = base + f"/api/library/folders/{watched}/slack"
                self.assertEqual(403, api.get(path).status)
                page.goto(base + "/operator/library")
                page.locator("#key").fill(operator)
                page.get_by_role("button", name="Load library").click()
                page.get_by_role("navigation", name="Folders").get_by_role("button", name="Festival edits").click()
                page.get_by_role("button", name="Slack notifications").click()
                dialog = page.locator("#workDialog")
                dialog.get_by_label("Slack incoming webhook").fill("https://example.invalid/unsafe")
                dialog.get_by_role("button", name="Save Slack settings").click()
                expect(dialog.get_by_role("status")).to_contain_text("Enter the incoming webhook")
                dialog.get_by_label("Slack incoming webhook").fill(hook)
                dialog.get_by_role("button", name="Save Slack settings").click()
                expect(dialog.get_by_role("status")).to_have_text("Slack settings saved.")
                self.assertNotIn(hook, api.get(path, headers=auth).text())
                with closing(sqlite3.connect(database)) as connection:
                    encrypted = connection.execute("SELECT address FROM folder_slack WHERE folder=?", (watched,)).fetchone()[0]
                self.assertNotIn(hook.encode(), encrypted)

                current = api.get(path, headers=auth).json()
                started = api.post(path + "/connect", headers=auth | {"X-CuePorch-Slack": "1"},
                                   data={"revision": current["revision"], "topics": current["topics"]})
                self.assertTrue(started.ok, started.text())
                state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
                isolated = browser.new_page(ignore_https_errors=True)
                self.assertEqual(403, isolated.request.get(callback, params={"state": state, "code": "fixture-code"}).status)
                self.assertEqual([], exchanges)
                with page.expect_request(lambda item: urlsplit(item.url).path == "/integration/oauth/slack/callback") as completed:
                    dialog.get_by_role("button", name="Choose channel in Slack").click()
                expect(dialog.get_by_role("status")).to_have_text("Slack channel connected.", timeout=15000)
                expect(dialog.get_by_text("Connected channel #festival.", exact=False)).to_be_visible()
                self.assertEqual(["fixture-code"], exchanges)
                self.assertEqual(403, page.request.get(completed.value.url).status)
                other = api.get(base + f"/api/library/folders/{others}/slack", headers=auth).json()
                self.assertIsNone(other["channel"])
                self.assertNotIn("discarded-token", api.get(path, headers=auth).text())

                upload(others)
                work = upload(watched)
                review = api.post(base + f"/api/library/work/{work}/review-links", headers=auth,
                                  data={"name": "Private notes", "allow_status": True})
                self.assertTrue(review.ok, review.text())
                review_path = review.json()["path"]
                reader = browser.new_page(ignore_https_errors=True)
                reader.goto(base + review_path)
                feedback = reader.locator("#reviewFeedback").get_attribute("data-feedback")
                self.assertTrue(reader.request.post(base + feedback,
                    data={"author": "Test reviewer", "body": "Private comment text", "seconds": 0.2}).ok)
                for _ in range(2):
                    self.assertTrue(reader.request.put(base + feedback + "/status", data={"status": "Approved"}).ok)
                deadline = monotonic() + 15
                while len(delivered) < 3 and monotonic() < deadline:
                    sleep(.1)
                self.assertEqual(3, len(delivered))
                self.assertEqual(4, len(attempts))
                self.assertEqual(sorted(folder_slack.TOPICS.values()), sorted(msg["text"].split(" <")[0] for msg in delivered))
                for message in delivered:
                    text = message["text"]
                    self.assertIn(f"{base}/team/video/{work}", text)
                    self.assertFalse(message["unfurl_links"] or message["unfurl_media"])
                    for private in (hook, clip.stem, review_path, "Private comment text", "Test reviewer"):
                        self.assertNotIn(private, text)
                self.assertEqual(401, reader.request.get(base + f"/team/video/{work}").status)
                expect(dialog.get_by_label("Replace webhook (optional)")).to_have_value("")
                dialog.get_by_role("button", name="Disconnect Slack").click()
                expect(dialog.get_by_role("status")).to_have_text("Slack disconnected.")
                self.assertEqual([], api.get(path, headers=auth).json()["deliveries"])
                upload(watched)
                with closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(0, connection.execute("SELECT count(*) FROM folder_slack_notice").fetchone()[0])
                self.assertEqual(3, len(delivered))
        print("Slack manual webhook, browser-bound OAuth and replay rejection, selected channel, scoped notices,"
              " 429 retry, private link and disconnect passed.")


if __name__ == "__main__":
    unittest.main()
