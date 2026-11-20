"""Exercise real TLS mail, the production settings panel and event attendance links."""

from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
from socketserver import TCPServer
import sqlite3
import ssl
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep, time
from unittest.mock import patch
from urllib.parse import urljoin
import unittest

from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import make_server

from browser import chromium
from installed_simulcast import free_port, tls_certificate
from test_team_invitation_delivery import Relay

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


@contextmanager
def service_worker(service):
    worker = Thread(target=service.serve_forever, daemon=True)
    worker.start()
    try:
        yield
    finally:
        service.shutdown()
        worker.join(5)
        service.server_close()


def wait_until(check, seconds=12):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        if check():
            return
        sleep(.1)
    raise AssertionError("A scheduled delivery or state change did not arrive.")


class WebinarEmail(unittest.TestCase):
    def test_webinar_email(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory(prefix="cueporch-webinar-mail-")))
            certificate, keyfile = tls_certificate(root)
            relay = TCPServer(("127.0.0.1", 0), Relay)
            relay.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            relay.tls.load_cert_chain(certificate, keyfile)
            relay.deliveries = []
            relay.delivered = Event()
            cleanup.enter_context(service_worker(relay))
            port = free_port()
            origin = f"https://127.0.0.1:{port}"
            cleanup.enter_context(patch.dict(os.environ, {
                "CUEPORCH_PUBLIC_URL": origin, "CUEPORCH_SMTP_HOST": "127.0.0.1",
                "CUEPORCH_SMTP_PORT": str(relay.server_address[1]), "CUEPORCH_SMTP_FROM": "station@example.invalid",
                "CUEPORCH_SMTP_USER": "fixture", "CUEPORCH_SMTP_PASSWORD": "mail-credential",
                "SSL_CERT_FILE": str(certificate),
            }))
            clock = [None]

            def simulated_time():
                return clock[0] if clock[0] is not None else time()

            for target in ("event_mail.time", "event_attendance.time"):
                cleanup.enter_context(patch(target, simulated_time))
            for worker in ("library_worker", "library_caption_jobs", "review_push"):
                cleanup.enter_context(patch(f"institution.{worker}.start"))
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            operator = secrets.token_urlsafe(32)
            database = root / "institution.sqlite"
            app = create_app(root, database, operator, "station")
            for handler in ("stop_editor", "stop_file_links", "stop_ai_drafts", "stop_event_capture"):
                cleanup.callback(app.extensions[handler])
            site = make_server("127.0.0.1", port, app, ssl_context=(str(certificate), str(keyfile)))
            cleanup.enter_context(service_worker(site))
            client = cleanup.enter_context(requests.Session())
            client.verify = str(certificate)
            client.headers.update(Authorization="Bearer " + operator)
            api = origin + "/api/live-events"
            when = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=6)
            end = when + timedelta(minutes=4)

            def create(slug, title):
                picked = client.post(api, json=dict(title=title, slug=slug, type="webinar", visibility="public",
                                     latency="standard", save_replay=False, schedule=dict(timezone="UTC",
                                     start=when.strftime("%Y-%m-%dT%H:%M"), end=end.strftime("%Y-%m-%dT%H:%M"),
                                     repeat="none")), timeout=8)
                picked.raise_for_status()
                event = picked.json()
                form_url = f"{api}/{event['id']}/registration"
                form = client.get(form_url, timeout=8).json()
                form["settings"]["enabled"] = True
                client.put(form_url, json=form, timeout=8).raise_for_status()
                return event, form_url + "/email"

            def register(event, contact):
                opens = event["occurrences"][0]["id"]
                response = client.post(f"{api}/{event['id']}/{opens}/registrants", json={"email": contact}, timeout=8)
                response.raise_for_status()
                return response

            quiet, quiet_url = create("silent-webinar", "Silent webinar")
            assert client.get(quiet_url, timeout=8).json()["settings"] == dict(
                confirmation=False, reminder_minutes=[], follow_up_hours=None)
            register(quiet, "no-mail@example.invalid")
            event, settings_url = create("mail-webinar", "Mail webinar")
            assert requests.get(settings_url, verify=str(certificate), timeout=8).status_code == 403
            with sync_playwright() as pw, chromium(pw) as browser:
                organizer = browser.new_page(ignore_https_errors=True)
                organizer.goto(origin + "/operator/live-events")
                organizer.get_by_label("Operator credential").fill(operator)
                organizer.get_by_role("button", name="Open live events").click()
                organizer.get_by_role("button", name="Open production for Mail webinar", exact=False).click()
                panel = organizer.locator("#eventMailSettings")
                panel.get_by_text("Webinar emails", exact=True).click()
                confirmation = panel.get_by_label("Send registration confirmations")
                expect(confirmation).to_be_enabled()
                expect(confirmation).not_to_be_checked()
                confirmation.check()
                panel.get_by_label("Minutes before start", exact=False).fill("1, 1")
                panel.get_by_role("button", name="Save webinar emails").click()
                expect(panel.locator("#eventMailStatus")).to_contain_text("distinct reminders")
                panel.get_by_label("Minutes before start", exact=False).fill("1")
                panel.get_by_label("Hours after the end", exact=False).fill("0")
                panel.get_by_role("button", name="Save webinar emails").click()
                expect(panel.locator("#eventMailStatus")).to_have_text("Webinar email settings saved.")
                assert client.put(settings_url, json=dict(revision=None, settings=dict(
                    confirmation=False, reminder_minutes=[], follow_up_hours=None)), timeout=8).status_code == 409
                guest = browser.new_page(ignore_https_errors=True)
                guest.goto(urljoin(origin, event["watch"]))
                guest.get_by_label("Email").fill("browser-attendee@example.invalid")
                guest.get_by_role("button", name="Register and watch").click()
                first = register(event, "api-attendee@example.invalid")
                repeat = register(event, "api-attendee@example.invalid")
                assert first.status_code == 201 and repeat.status_code == 200 and first.json()["join"] == repeat.json()["join"]
                revoke = register(event, "revoked-attendee@example.invalid").json()
                wait_until(lambda: len(relay.deliveries) >= 3)
                assert len(relay.deliveries) == 3
                delivered = {str(mail["To"]): mail for mail in relay.deliveries}
                assert "no-mail@example.invalid" not in delivered
                mail = delivered["browser-attendee@example.invalid"]
                assert mail["Subject"] == "You're registered for Mail webinar"
                href = next(line for line in mail.get_content().splitlines() if line.startswith("https://"))
                assert href.startswith(origin + "/events/mail-webinar/join#")
                assert "browser-attendee@example.invalid" not in href
                personal = browser.new_page(ignore_https_errors=True)
                personal.goto(href)
                personal.wait_for_url(origin + "/events/mail-webinar?occurrence=*")
                expect(personal.get_by_role("button", name="Register and watch")).to_have_count(0)
                opens = event["occurrences"][0]["id"]
                client.delete(f"{api}/{event['id']}/{opens}/registrants/{revoke['id']}/join", timeout=8).raise_for_status()
                clock[0] = opens - 59
                wait_until(lambda: len(relay.deliveries) >= 5)
                reminders = relay.deliveries[3:]
                assert len(reminders) == 2 and all(mail["Subject"] == "Reminder about Mail webinar" for mail in reminders)
                assert {str(mail["To"]) for mail in reminders} == {"api-attendee@example.invalid", "browser-attendee@example.invalid"}
                assert href in next(mail.get_content() for mail in reminders if str(mail["To"]).startswith("browser-"))
                clock[0] = int(end.timestamp()) + 1
                expired = browser.new_page(ignore_https_errors=True)
                expired.goto(href)
                expect(expired.get_by_role("status")).to_contain_text("This personal link is unavailable")
                wait_until(lambda: len(relay.deliveries) >= 7)
                assert len(relay.deliveries) == 7
                followups = relay.deliveries[5:]
                assert all(mail["Subject"] == "Thank you for registering for Mail webinar" for mail in followups)
                assert all("/join#" not in mail.get_content() for mail in followups)
                wait_until(lambda: client.get(settings_url, timeout=8).json()["deliveries"] == dict(accepted=7, canceled=2))
            clock[0] = None
            policy = origin + "/api/policy/publication"
            client.put(policy, json={"required": True}, timeout=8).raise_for_status()
            held, held_url = create("held-webinar", "Held webinar")
            assert held["publication"] == "waiting" and held["watch"] is None
            client.put(held_url, json=dict(revision=None, settings=dict(
                confirmation=True, reminder_minutes=[], follow_up_hours=None)), timeout=8).raise_for_status()
            held_id = register(held, "waiting-attendee@example.invalid").json()["id"]

            def held_by_worker():
                with closing(sqlite3.connect(database)) as db:
                    job = db.execute("SELECT due,state,attempts FROM live_event_mail_job WHERE link=?", (held_id,)).fetchone()
                    return job and job[0] > time() and job[1:] == ("queued", 0)

            wait_until(held_by_worker)
            assert len(relay.deliveries) == 7
            client.put(policy, json={"required": False}, timeout=8).raise_for_status()
            clock[0] = int(time()) + 16
            wait_until(lambda: len(relay.deliveries) == 8)
            assert relay.deliveries[-1]["Subject"] == "You're registered for Held webinar"
            print("Verified 8 messages over TLS, default-off and revocation, expiry, and release after disabling optional approval.")


if __name__ == "__main__":
    unittest.main()
