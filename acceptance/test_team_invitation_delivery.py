"""Exercise team mail through a local TLS relay and the actual invitation routes."""

from base64 import b64decode
from contextlib import closing, ExitStack
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import secrets
from socketserver import StreamRequestHandler, TCPServer
import sqlite3
import ssl
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from installed_simulcast import tls_certificate

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


class Relay(StreamRequestHandler):
    def setup(self):
        self.request.settimeout(8)
        self.request = self.server.tls.wrap_socket(self.request, server_side=True)
        super().setup()

    def handle(self):
        self.wfile.write(b"220 fixture ESMTP\r\n")
        while command := self.rfile.readline():
            command = command.rstrip(b"\r\n")
            operation = command.lower()
            if operation.startswith(b"ehlo "):
                reply = b"250-fixture\r\n250 AUTH PLAIN"
            elif operation.startswith(b"auth plain "):
                assert b64decode(command.rsplit(b" ", 1)[-1]) == b"\x00fixture\x00mail-credential"
                reply = b"235 authenticated"
            elif operation.startswith(b"mail from:"):
                reply = b"250 sender accepted"
            elif operation.startswith(b"rcpt to:"):
                reply = b"550 recipient unavailable" if b"fail@" in command else b"250 recipient accepted"
            elif operation == b"data":
                self.wfile.write(b"354 end with dot\r\n")
                lines = []
                while (line := self.rfile.readline()) not in (b"", b".\r\n"):
                    lines.append(line[1:] if line.startswith(b"..") else line)
                self.server.deliveries.append(BytesParser(policy=policy.default).parsebytes(b"".join(lines)))
                reply = b"250 message accepted"
            elif operation == b"quit":
                self.wfile.write(b"221 closed\r\n")
                return
            else:
                reply = b"250 OK"
            self.wfile.write(reply + b"\r\n")

    def finish(self):
        try:
            super().finish()
        finally:
            self.request.close()


class InvitationDelivery(unittest.TestCase):
    def test_relay_expiration_and_copy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as cleanup:
            for worker in ("library_worker", "library_caption_jobs", "review_push"):
                cleanup.enter_context(patch(f"institution.{worker}.start"))
            root = Path(tmp)
            db = root / "institution.db"
            cert, key = tls_certificate(root)
            relay = cleanup.enter_context(TCPServer(("127.0.0.1", 0), Relay))
            relay.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            relay.tls.load_cert_chain(cert, key)
            relay.deliveries = []
            listener = Thread(target=relay.serve_forever, daemon=True)
            listener.start()
            cleanup.callback(listener.join)
            cleanup.callback(relay.shutdown)
            canonical = "https://station.example.test"
            cleanup.enter_context(patch.dict(os.environ, {
                "CUEPORCH_PUBLIC_URL": canonical, "CUEPORCH_SMTP_HOST": "127.0.0.1",
                "CUEPORCH_SMTP_PORT": str(relay.server_address[1]),
                "CUEPORCH_SMTP_FROM": "crew@example.test", "CUEPORCH_SMTP_USER": "fixture",
                "CUEPORCH_SMTP_PASSWORD": "mail-credential", "SSL_CERT_FILE": str(cert),
            }))
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            operator = secrets.token_urlsafe(32)
            app = create_app(root, db, operator, "station")
            cleanup.callback(app.extensions["stop_event_capture"])
            cleanup.callback(app.extensions["stop_editor"])
            request = app.test_client()
            team_headers = {"X-CuePorch-Team": "1"}
            operator_headers = {**team_headers, "Authorization": "Bearer " + operator}

            def invite(address, send_email=True, headers=operator_headers):
                return request.post("/api/team/invitations", headers=headers, json=dict(
                    email=address, role="viewer", send_email=send_email, message="Welcome to the station."
                ))

            def join(invitation):
                return request.post("/api/team/join", base_url=canonical, headers=team_headers, json=dict(
                    invitation=urlsplit(invitation["path"]).fragment, name="New viewer", password="Rehearsal passphrase"
                ))

            self.assertIn(b"Email the invitation", request.get("/team/members").data)
            self.assertEqual(invite("denied@example.test", headers=team_headers).status_code, 403)
            self.assertEqual(relay.deliveries, [])
            result = invite("recipient@example.test")
            self.assertEqual(result.status_code, 201)
            self.assertEqual(result.json["delivery"], "accepted")
            self.assertEqual(result.json["url"], canonical + result.json["path"])
            (message,) = relay.deliveries
            self.assertEqual(message["To"], "recipient@example.test")
            self.assertIn(result.json["url"], message.get_content())
            self.assertIn("Welcome to the station.", message.get_content())
            self.assertNotIn("mail-credential", message.get_content())
            self.assertEqual(join(result.json).status_code, 201)
            self.assertEqual(join(result.json).status_code, 404)

            fallback = invite("fail@example.test")
            self.assertEqual(fallback.status_code, 201)
            self.assertEqual(fallback.json["delivery"], "failed")
            self.assertEqual(join(fallback.json).status_code, 201)
            manual = invite("manual@example.test", False)
            self.assertIsNone(manual.json["delivery"])
            self.assertEqual(len(relay.deliveries), 1)
            token = urlsplit(manual.json["path"]).fragment
            with closing(sqlite3.connect(db)) as con, con:
                record = con.execute("SELECT token_hash,expires FROM team_invitation WHERE id=?",
                                     (manual.json["id"],)).fetchone()
                self.assertNotEqual(record[0], token)
                con.execute("UPDATE team_invitation SET expires=0 WHERE id=?", (manual.json["id"],))
            self.assertEqual(join(manual.json).status_code, 404)
            rotated = invite("manual@example.test", False)
            self.assertEqual(join(manual.json).status_code, 404)
            self.assertEqual(join(rotated.json).status_code, 201)

            service = make_server("127.0.0.1", 0, app)
            runner = Thread(target=service.serve_forever, daemon=True)
            runner.start()
            cleanup.callback(service.server_close)
            cleanup.callback(runner.join)
            cleanup.callback(service.shutdown)
            with sync_playwright() as browser_tool, chromium(browser_tool) as browser:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{service.server_port}/team/members")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open team members").click()
                expect(page.get_by_label("Email the invitation")).to_be_checked()
                page.get_by_label("Invite email").fill("browser@example.test")
                page.get_by_label("Invitation message").fill("Invitation from the team screen.")
                page.get_by_role("button", name="Create invitation").click()
                expect(page.locator("#teamMembersStatus")).to_have_text("The mail relay accepted the invitation.")
                link = page.get_by_label("Private invitation link").input_value()
                self.assertTrue(link.startswith(canonical + "/team/join#"))
                self.assertIn(link, relay.deliveries[-1].get_content())
                self.assertIn("Invitation from the team screen.", relay.deliveries[-1].get_content())


if __name__ == "__main__":
    unittest.main()
