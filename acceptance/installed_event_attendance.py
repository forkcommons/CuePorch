"""Exercise personal attendee links through a real password-protected broadcast."""

from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
import uuid

import requests
from playwright.sync_api import expect, sync_playwright

from browser import chromium
from installed_events import play_event, send_signal, wait_for_delivery


def check_event_attendance(base, keys, ffmpeg):
    operator = {"Authorization": "Bearer " + keys["operator"]}
    root = base + "/api/live-events"
    credentials = base + "/api/operator/live-api/credentials"
    issued = requests.post(credentials, headers=operator, timeout=10,
                           json=dict(name="Attendee intake", scopes=["registrations"]))
    issued.raise_for_status()
    client = {"Authorization": "Bearer " + issued.json()["token"]}
    password = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    event = dict(title="Personal link webinar", slug="attendee-link-webinar", visibility="password", view_password=password,
                 type="webinar", latency="standard", save_replay=False, dvr_seconds=120,
                 schedule=dict(timezone="UTC", repeat="days", interval=1, count=2,
                               start=(now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                               end=(now + timedelta(minutes=8)).strftime("%Y-%m-%dT%H:%M")))
    created = requests.post(root, headers=operator, json=event, timeout=10)
    created.raise_for_status()
    entry = created.json()
    uid = entry["id"]
    opens, following = (slot["id"] for slot in entry["occurrences"])
    registration = f"{root}/{uid}/registration"
    form = requests.get(registration, headers=operator, timeout=10).json()
    form["settings"].update(enabled=True, heading="Register for the webinar", first_name="required")
    requests.put(registration, headers=operator, json=form, timeout=10).raise_for_status()
    assert requests.post(f"{base}/api/v1/live-events/{uid}/{opens}/registrants", headers=client,
                         json={"email": "tv-viewer@example.invalid"}, timeout=10).status_code == 422
    intake = f"{base}/api/v1/live-events/{uid}/{opens}/registrants"
    attendee = {"email": "tv-viewer@example.invalid", "first_name": "Sam"}
    submitted = requests.post(intake, headers=client, timeout=10, json=attendee)
    assert submitted.status_code == 201
    link = submitted.json()
    repeated = requests.post(intake, headers=client, timeout=10, json=attendee | {"first_name": "Samira"})
    assert repeated.status_code == 200 and repeated.json()["join"] == link["join"]
    rows = requests.get(registration, headers=operator, timeout=10).json()["registrants"]
    assert len(rows) == 1 and "Samira" in str(rows[0])
    assert password not in link["join"] and "tv-viewer" not in link["join"]
    production = f"{root}/{uid}/{opens}"
    setup = requests.post(production + "/encoder", headers=operator, timeout=45)
    setup.raise_for_status()
    encoder = setup.json()
    watch = urljoin(base, entry["watch"]) + f"?occurrence={opens}"
    join = urljoin(base, link["join"])

    def unlock(page):
        page.get_by_label("Event password").fill(password)
        page.get_by_role("button", name="Unlock event").click()

    try:
        with sync_playwright() as pw, chromium(pw) as browser:
            context = browser.new_context()
            viewer = context.new_page()
            viewer.goto(join)
            expect(viewer).to_have_url(watch)
            cookies = [cookie for cookie in context.cookies(watch) if cookie["name"].startswith("cueporch_event_")]
            assert len(cookies) == 1 and cookies[0]["httpOnly"] and cookies[0]["sameSite"] == "Lax"
            expect(viewer.get_by_label("Event password")).to_be_visible()
            unlock(viewer)
            expect(viewer.locator("#eventStatus")).to_have_text("Waiting for the encoder")
            expect(viewer.get_by_role("button", name="Register and watch")).to_have_count(0)
            other = context.new_page()
            other.goto(urljoin(base, entry["watch"]) + f"?occurrence={following}")
            unlock(other)
            expect(other.locator("#eventStatus")).to_have_text("Registration required")
            frame = context.new_page()
            frame.goto(urljoin(base, entry["embed"]) + f"?occurrence={opens}")
            unlock(frame)
            expect(frame.locator("#eventStatus")).to_have_text("Registration required")
            with send_signal(ffmpeg, encoder["server"], encoder["key"], 65) as sending:
                wait_for_delivery(production, operator, "live", 45, sending)
                expect(viewer.locator("#eventStatus")).to_have_text("Live", timeout=20000)
                manifest, width, _ = play_event(viewer, watch, "Live", navigate=False)
                media = requests.get(manifest, timeout=10)
                media.raise_for_status()
                segments = [line for line in media.text.splitlines() if line and not line.startswith("#")]
                if segments[0].endswith(".m3u8"):
                    manifest = urljoin(manifest, segments[0])
                    media = requests.get(manifest, timeout=10)
                    media.raise_for_status()
                    segments = [line for line in media.text.splitlines() if line and not line.startswith("#")]
                fragment = urljoin(manifest, segments[0])
                assert requests.get(fragment, timeout=10).ok
                revoked = requests.delete(intake + "/" + link["id"] + "/join", headers=client, timeout=10)
                assert revoked.status_code == 204
                assert requests.get(manifest, timeout=10).status_code == 404
                assert requests.get(fragment, timeout=10).status_code == 404
                expect(viewer.locator("#eventStatus")).to_have_text("Registration required", timeout=12000)
                expect(viewer.locator("video")).to_have_count(0)
                viewer.goto(join)
                expect(viewer.locator("#attendanceStatus")).to_contain_text("This personal link is unavailable.")
                assert requests.post(intake, headers=client, timeout=10, json=attendee).status_code == 409
    finally:
        requests.delete(production + "/delivery", headers=operator, timeout=35).raise_for_status()
    return dict(decoded_live_width=width, repeated_intake_preserved_link=True, required_answers_enforced=True,
                password_unchanged=True, cross_occurrence_denied=True, embed_requires_form=True,
                first_party_cookie=True, revoked_view_and_media=True, guest_link_contains_no_contact_data=True)
