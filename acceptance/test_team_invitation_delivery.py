"""Exercise invitations and account email through the real routes and a local TLS relay."""

from base64 import b64decode
from contextlib import closing, ExitStack
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import secrets
from socketserver import StreamRequestHandler, TCPServer
import sqlite3
import ssl
import sys
import tempfile
from threading import Event, Thread
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from installed_authenticator import app_code
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
                self.server.delivered.set()
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
    def verify_password_recovery(self, browser, base, relay, db, live_account, scope, address, password):
        prefix = "/team" if scope == "team" else "/account"
        header = {"X-CuePorch-" + scope.capitalize(): "1"}
        api = f"{base}/api/{scope}"
        other_scope = "viewer" if scope == "team" else "team"
        account = api + "/account"
        recovery = api + "/password-recovery"
        credentials = dict(email=address, password=password)
        old = live_account.request
        recovery_codes = []
        if scope == "team":
            setup = old.post(api + "/authenticator", headers=header, data={"password": password}).json()
            enrolled = old.put(api + "/authenticator", headers=header, data={
                "setup_ticket": setup["setup_ticket"], "code": app_code(setup["setup_key"]),
            })
            self.assertTrue(enrolled.ok)
            recovery_codes = enrolled.json()["recovery_codes"]

        with browser.new_context() as fresh:
            page = fresh.new_page()
            page.goto(base + prefix)
            page.get_by_role("link", name="Forgot your password?").click()
            email = page.get_by_label("Email")
            send = page.get_by_role("button", name="Send reset link")
            unknown = fresh.request.post(recovery, headers=header, data={"email": "unused@example.test"}, timeout=8000)
            self.assertEqual(unknown.status, 202)
            self.assertEqual(unknown.json(), {"accepted": True})
            status = page.locator("#passwordRecoveryStatus")
            message = "If this address belongs to a verified account, CuePorch will send a reset link."

            def request_link():
                before = len(relay.deliveries)
                relay.delivered.clear()
                email.fill(address)
                with page.expect_response(lambda response: response.url == recovery and response.request.method == "POST", timeout=8000) as accepted:
                    send.click()
                self.assertEqual(accepted.value.status, 202)
                expect(status).to_have_text(message)
                self.assertTrue(relay.delivered.wait(8), "No reset message reached the local relay.")
                self.assertEqual(len(relay.deliveries), before + 1)
                delivered = relay.deliveries[-1]
                self.assertEqual(delivered["To"], address)
                url = next(line for line in delivered.get_content().splitlines() if line.startswith("https://"))
                parsed = urlsplit(url)
                self.assertEqual((parsed.netloc, parsed.path), ("station.example.test", prefix + "/reset"))
                deadline = monotonic() + 3
                while monotonic() < deadline:
                    with closing(sqlite3.connect(db)) as con:
                        stored = con.execute("""SELECT token_hash,delivered FROM account_password_reset
                                                WHERE scope=? AND address=?""", (scope, address)).fetchone()
                    if stored and stored[1]:
                        self.assertNotEqual(stored[0], parsed.fragment)
                        return parsed
                    sleep(0.02)
                self.fail("The reset message arrived but was not marked delivered.")

            expired = request_link()
            duplicate = fresh.request.post(recovery, headers=header, data={"email": address}, timeout=8000)
            self.assertEqual((duplicate.status, duplicate.json()), (unknown.status, unknown.json()))
            changed = f"Rehearsed {scope} password after recovery!"
            with closing(sqlite3.connect(db)) as con, con:
                con.execute("UPDATE account_password_reset SET expires=0 WHERE scope=?", (scope,))
            attempt = dict(reset_token=expired.fragment, password=changed)
            self.assertEqual(fresh.request.put(recovery, headers=header, data=attempt).status, 404)
            parsed = request_link()
            attempt["reset_token"] = parsed.fragment
            self.assertEqual(fresh.request.put(f"{base}/api/{other_scope}/password-recovery",
                             headers={"X-CuePorch-" + other_scope.capitalize(): "1"}, data=attempt).status, 404)
            page = fresh.new_page()
            local = page.goto(base + parsed.path + "#" + parsed.fragment)
            self.assertEqual(local.headers["referrer-policy"], "no-referrer")
            expect(page).to_have_url(base + parsed.path)
            status = page.locator("#passwordRecoveryStatus")
            page.get_by_label("New password", exact=True).fill(changed)
            page.get_by_label("Confirm new password", exact=True).fill(changed)
            confirm = page.get_by_role("button", name="Change password")
            confirm.click()
            if recovery_codes:
                factor = page.get_by_label("Authenticator or recovery code")
                expect(factor).to_be_visible()
                self.assertTrue(old.get(account).ok)
                factor.fill(recovery_codes[0])
                confirm.click()
            expect(status).to_have_text("Password changed. Return to sign in.")
            self.assertEqual(old.get(account).status, 401)
            self.assertEqual(fresh.request.get(account).status, 401)
            self.assertEqual(fresh.request.put(recovery, headers=header, data=attempt).status, 404)
            self.assertEqual(old.post(api + "/session", headers=header, data=credentials).status, 401)
            credentials["password"] = changed
            if recovery_codes:
                self.assertEqual(old.post(api + "/session", headers=header, data=credentials).status, 428)
                credentials["code"] = recovery_codes[1]
            self.assertTrue(old.post(api + "/session", headers=header, data=credentials).ok)
            return changed

    def verify_account_email(self, browser, base, relay, db, operator):
        address = "recipient@example.test"
        credentials = "Rehearsal passphrase"

        def delivered_code():
            return next(line for line in relay.deliveries[-1].get_content().splitlines()
                        if re.fullmatch(r"[0-9]{8}", line))

        with browser.new_context() as browser_scope:
            page = browser_scope.new_page()
            page.goto(base + "/team")
            form = page.locator("#teamSignIn")
            form.get_by_label("Team email").fill(address)
            form.get_by_label("Password").fill(credentials)
            form.get_by_role("button", name="Sign in to team").click()
            page.get_by_role("link", name="Account security").click()
            email_status = page.locator("#verifyAccountEmailStatus")
            expect(email_status).to_have_text("Email is awaiting verification.")
            page.get_by_role("button", name="Send verification code").click()
            expect(email_status).to_have_text("The relay accepted your code. Check your email.")
            team_code = delivered_code()
            self.assertEqual(relay.deliveries[-1]["To"], address)
            with closing(sqlite3.connect(db)) as con:
                stored = con.execute("SELECT code_hash FROM account_email_verification WHERE scope='team'").fetchone()[0]
                self.assertEqual(len(stored), 32)
                self.assertNotIn(team_code.encode(), stored)
            page.reload()
            expect(email_status).to_have_text("Enter the verification code from your email.")
            page.get_by_label("Eight-digit verification code").fill(team_code)
            page.get_by_role("button", name="Verify email", exact=True).click()
            expect(email_status).to_have_text("Email verified.")
            team = browser_scope.request
            self.assertEqual(team.get(base + "/api/team/email-verification").json(), dict(
                address=address, available=True, verified=True, pending=False,
            ))
            audit = team.get(base + "/api/team/audit", headers={"Authorization": "Bearer " + operator}).json()
            self.assertIn("email_verified", [row["action"] for row in audit["events"]])

            page.goto(base + "/account")
            form = page.locator("#viewerSignUp")
            form.get_by_label("Display name").fill("Local viewer")
            form.get_by_label("Email").fill(address)
            form.get_by_label("Password").fill(credentials)
            form.get_by_role("button", name="Create account").click()
            page.get_by_role("link", name="Account security").click()
            expect(email_status).to_have_text("Email is awaiting verification.")
            mailbox = base + "/api/viewer/email-verification"
            headers = {"X-CuePorch-Viewer": "1"}
            sent_before = len(relay.deliveries)
            self.assertEqual(browser_scope.request.post(mailbox, headers=headers | {"Origin": "https://unrelated.invalid"},
                                                        data={}).status, 403)
            self.assertEqual(len(relay.deliveries), sent_before)
            page.get_by_role("button", name="Send verification code").click()
            expect(email_status).to_have_text("The relay accepted your code. Check your email.")
            viewer_code = delivered_code()
            self.assertEqual(browser_scope.request.post(mailbox, headers=headers, data={}).status, 429)
            self.assertEqual(len(relay.deliveries), sent_before + 1)
            with closing(sqlite3.connect(db)) as con, con:
                con.execute("UPDATE account_email_verification SET expires=0 WHERE scope='viewer'")
            page.get_by_label("Eight-digit verification code").fill(viewer_code)
            page.get_by_role("button", name="Verify email", exact=True).click()
            expect(email_status).to_have_text("The verification code did not match or expired.")
            page.get_by_role("button", name="Send verification code").click()
            expect(email_status).to_have_text("The relay accepted your code. Check your email.")
            new_code = delivered_code()
            page.get_by_label("Eight-digit verification code").fill(new_code)
            page.get_by_role("button", name="Verify email", exact=True).click()
            expect(email_status).to_have_text("Email verified.")
            self.assertEqual(browser_scope.request.put(mailbox, headers=headers, data={"code": new_code}).status, 422)

            credentials = self.verify_password_recovery(browser, base, relay, db, browser_scope, "viewer",
                                                        address, credentials)
            self.verify_password_recovery(browser, base, relay, db, browser_scope, "team", address,
                                          "Rehearsal passphrase")
            changed = browser_scope.request.put(base + "/api/viewer/account", headers=headers, data=dict(
                email="fail@example.test", name="Local viewer", current_password=credentials,
            ))
            self.assertTrue(changed.ok)
            page.reload()
            expect(email_status).to_have_text("Email is awaiting verification.")
            page.get_by_role("button", name="Send verification code").click()
            expect(email_status).to_have_text("CuePorch could not send the verification email. Try again later.")
            self.assertFalse(browser_scope.request.get(mailbox).json()["pending"])
            self.assertTrue(team.get(base + "/api/team/email-verification").json()["verified"])

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
            relay.delivered = Event()
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
            cleanup.callback(app.extensions["stop_file_links"])
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
                self.verify_account_email(browser, f"http://127.0.0.1:{service.server_port}", relay, db, operator)


if __name__ == "__main__":
    unittest.main()
