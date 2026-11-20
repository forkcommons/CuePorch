"""Receive signed lifecycle callbacks from an actual registered and recorded event."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Empty, Queue
import ssl
from threading import Thread
from time import monotonic, time
from urllib.parse import urljoin

import requests
from playwright.sync_api import expect, sync_playwright

from browser import chromium
from installed_events import play_event, send_signal, wait_for_delivery
from installed_simulcast import tls_certificate


@contextmanager
def webhook_receiver(root):
    messages = Queue()
    retried = set()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            headers = dict(self.headers)
            uid = headers["X-CuePorch-Delivery"]
            first = headers["X-CuePorch-Event"] == "live-event-updated" and uid not in retried
            if first:
                retried.add(uid)
            status = 503 if first else 204
            messages.put((headers, raw, status))
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_):
            pass

    certificate, key = tls_certificate(root)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origin = f"https://127.0.0.1:{server.server_port}"
        try:
            yield dict(CUEPORCH_EVENT_WEBHOOK_ORIGINS=origin, REQUESTS_CA_BUNDLE=str(certificate)), messages
        finally:
            server.shutdown()
            worker.join(4)


def check_event_webhooks(base, keys, ffmpeg, receiver):
    origins, messages = receiver
    operator = {"Authorization": "Bearer " + keys["operator"]}
    path = base + "/api/operator/live-api/webhooks"
    home = base + "/api/live-events"
    versioned = base + "/api/v1/live-events"
    endpoint = origins["CUEPORCH_EVENT_WEBHOOK_ORIGINS"] + "/receive"
    topics = ["live-event-updated", "live-event-clip-created", "live-event-started", "live-event-ended",
              "live-event-archive-available", "registration-created"]
    wrong = requests.post(path, headers=operator, json=dict(endpoint="https://example.invalid/hook", topics=topics),
                          timeout=10)
    assert wrong.status_code == 422
    created = requests.post(path, headers=operator, json=dict(endpoint=endpoint, topics=topics), timeout=10)
    created.raise_for_status()
    webhook = created.json()
    secret = webhook["signing_secret"].encode("ascii")
    details = path + "/" + webhook["id"]
    assert "signing_secret" not in requests.get(path, headers=operator, timeout=10).json()["webhooks"][0]
    captured = []

    def receive(topic, status=204, timeout=30):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            for payload, delivered in captured:
                if payload["type"] == topic and delivered == status:
                    captured.remove((payload, delivered))
                    return payload
            headers, raw, result = messages.get(timeout=max(.1, deadline - monotonic()))
            stamp = headers["X-CuePorch-Timestamp"]
            expected = hmac.new(secret, stamp.encode("ascii") + b"." + raw, sha256).hexdigest()
            assert hmac.compare_digest(headers["X-CuePorch-Signature"], "v1=" + expected)
            assert abs(time() - int(stamp)) < 30
            body = json.loads(raw)
            assert body["id"] == headers["X-CuePorch-Delivery"] and body["type"] == headers["X-CuePorch-Event"]
            assert b"@example.invalid" not in raw and b"streamKey" not in raw
            captured.append((body, result))
        raise AssertionError(f"Did not receive {topic}.")

    now = datetime.now(timezone.utc)
    proposal = dict(title="Webhook webinar", slug="webhook-webinar", type="webinar", visibility="public",
                    latency="standard", save_replay=True, archive_replay=False,
                    schedule=dict(timezone="UTC", start=(now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                                  end=(now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M"), repeat="none"))
    scheduled = requests.post(home, headers=operator, json=proposal, timeout=10)
    scheduled.raise_for_status()
    entry = scheduled.json()
    uid = entry["id"]
    (slot,) = entry["occurrences"]
    opens = slot["id"]
    failed = receive("live-event-updated", 503)
    accepted = receive("live-event-updated")
    assert failed == accepted and accepted["event"] == uid and accepted["data"]["change"] == "created"
    credential = requests.post(base + "/api/operator/live-api/credentials", headers=operator, timeout=10,
                               json=dict(name="Webhook contact reader", scopes=["registrations"]))
    credential.raise_for_status()
    intake = {"Authorization": "Bearer " + credential.json()["token"]}
    registration = f"{home}/{uid}/registration"
    form = requests.get(registration, headers=operator, timeout=10).json()
    form["settings"].update(enabled=True, heading="Register for the webhook webinar")
    requests.put(registration, headers=operator, json=form, timeout=10).raise_for_status()
    registrants = f"{versioned}/{uid}/{opens}/registrants"
    contact = {"email": "api-attendee@example.invalid"}
    assert requests.post(registrants, headers=intake, json=contact, timeout=10).status_code == 201
    assert requests.post(registrants, headers=intake, json=contact, timeout=10).status_code == 200
    api_contact = receive("registration-created")
    read_contact = f"{versioned}/{uid}/registrants/{api_contact['data']['registrant']}"
    assert requests.get(read_contact, timeout=10).status_code == 401
    assert requests.get(read_contact, headers=intake, timeout=10).json()["email"] == contact["email"]
    production = f"{home}/{uid}/{opens}"
    setup = requests.post(production + "/encoder", headers=operator, timeout=45)
    setup.raise_for_status()
    encoder = setup.json()
    assert receive("live-event-clip-created")["occurrence"] == opens
    watch = urljoin(base, entry["watch"])
    with sync_playwright() as pw, chromium(pw) as browser:
        viewer = browser.new_page()
        viewer.goto(watch)
        viewer.get_by_label("Email").fill("browser-attendee@example.invalid")
        viewer.get_by_role("button", name="Register and watch").click()
        browser_contact = receive("registration-created")
        assert browser_contact["data"]["registrant"] != api_contact["data"]["registrant"]
        with send_signal(ffmpeg, encoder["server"], encoder["key"], 54) as sending:
            wait_for_delivery(production, operator, "live", 45, sending)
            assert receive("live-event-started")["occurrence"] == opens
            expect(viewer.locator("#eventStatus")).to_have_text("Live", timeout=20000)
            _, live_width, _ = play_event(viewer, watch, "Live", navigate=False)
        ended = receive("live-event-ended", timeout=65)
        print(f"Live {live_width}px registered video and signed ended webhook passed. Waiting for the archive.", flush=True)
        archive = receive("live-event-archive-available", timeout=420)
        assert ended["occurrence"] == archive["occurrence"] == opens and archive["data"]["clip"]
        expect(viewer.locator("#eventStatus")).to_have_text("Replay", timeout=30000)
        _, replay_width, duration = play_event(viewer, watch, "Replay", navigate=False)
        assert duration > 0
    recorded = requests.get(details, headers=operator, timeout=10).json()["deliveries"]
    assert len([item for item in recorded if item["topic"] == "registration-created"]) == 2
    retry = next(row for row in recorded if row["id"] == accepted["id"])
    assert retry["state"] == "delivered" and retry["attempts"] == 2
    requests.delete(details, headers=operator, timeout=25).raise_for_status()
    assert requests.get(details, headers=operator, timeout=10).status_code == 404
    requests.post(home, headers=operator, json=proposal | {"slug": "webhook-disconnected"}, timeout=10).raise_for_status()
    try:
        messages.get(timeout=2)
    except Empty:
        pass
    else:
        raise AssertionError("The revoked webhook received another event.")
    return dict(tls_signed_deliveries=True, retried_stable_delivery_id=True, no_contact_data_in_callbacks=True,
                scoped_contact_lookup=True, both_registration_paths=True, real_start_end_archive=True,
                playback_live_px=live_width, playback_replay_px=replay_width, replay_duration=duration,
                allowlist_blocks_unknown_receiver=True, revoked_stopped_delivery=True)
