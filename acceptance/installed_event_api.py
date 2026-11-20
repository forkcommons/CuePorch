"""Issue scoped credentials and operate an actual registered broadcast through the event API."""

from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
from urllib.parse import urljoin

import requests
from playwright.sync_api import expect, sync_playwright

from browser import chromium
from installed_events import play_event, send_signal, wait_for_delivery


def check_event_api(base, keys, ffmpeg):
    manage = base + "/api/operator/live-api/credentials"
    portal = base + "/api/v1/live-events"
    policy = base + "/api/policy/publication"
    operator = {"Authorization": "Bearer " + keys["operator"]}

    def issue(name, scopes):
        response = requests.post(manage, headers=operator, timeout=10,
                                 json=dict(name=name, scopes=scopes, expires_in_days=2))
        response.raise_for_status()
        return {"Authorization": "Bearer " + response.json()["token"]}

    rejected = requests.get(portal, headers=operator, timeout=10)
    assert rejected.status_code == 401 and "Bearer" in rejected.headers["WWW-Authenticate"]
    scheduler = issue("Production calendar", ["schedule"])
    producer = issue("Encoder machine", ["produce"])
    registrations = issue("Attendance desk", ["registrations"])
    now = datetime.now(timezone.utc)
    event = dict(title="API webinar rehearsal", slug="event-api-webinar", type="webinar", latency="standard",
                 visibility="public", save_replay=True, archive_replay=False,
                 schedule=dict(timezone="UTC", start=(now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                               end=(now + timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M"),
                               repeat="days", interval=1, count=2))
    with sync_playwright() as pw, chromium(pw) as browser:
        dashboard = browser.new_page()
        dashboard.goto(base + "/operator/live-events")
        login = dashboard.locator("#eventsIdentify")
        login.get_by_label("Operator credential").fill(keys["operator"])
        login.get_by_role("button", name="Open live events").click()
        dashboard.get_by_text("Developer access", exact=True).click()
        dashboard.get_by_label("Token name").fill("Read calendar")
        dashboard.get_by_role("button", name="Create token").click()
        secret = dashboard.get_by_label("New event API token")
        expect(secret).to_be_visible()
        reader = {"Authorization": "Bearer " + secret.input_value()}
        dashboard.get_by_text("Developer access", exact=True).click()
        expect(secret).to_have_value("")
        assert requests.post(portal, headers=reader, json=event, timeout=10).status_code == 403
        assert requests.get(manage, headers=reader, timeout=10).status_code == 403
        assert requests.get(base + "/api/live-events", headers=reader, timeout=10).status_code == 403
        created = requests.post(portal, headers=scheduler, json=event, timeout=10)
        created.raise_for_status()
        entry = created.json()
        assert entry["publication"] == "eligible" and len(entry["occurrences"]) == 2
        target = portal + "/" + entry["id"]
        production = f"{target}/{entry['occurrences'][0]['id']}"
        assert requests.get(target, headers=scheduler, timeout=10).status_code == 403
        assert requests.get(target, headers=reader, timeout=10).json()["embed"] == entry["embed"]
        assert requests.post(production + "/encoder", headers=reader, timeout=10).status_code == 403
        changed = requests.put(target, headers=scheduler, timeout=10,
                               json=event | {"title": "Updated API webinar", "revision": entry["revision"]})
        changed.raise_for_status()
        assert requests.put(target, headers=scheduler, timeout=10,
                            json=event | {"revision": entry["revision"]}).status_code == 409
        requests.put(policy, headers=operator, json={"required": True}, timeout=10).raise_for_status()
        try:
            gated = requests.post(portal, headers=scheduler, timeout=10, json=event | {"slug": "pending-event-api"})
            gated.raise_for_status()
            assert gated.json()["publication"] == "waiting" and gated.json()["embed"] is None
        finally:
            requests.put(policy, headers=operator, json={"required": False}, timeout=10).raise_for_status()

        form = requests.get(target + "/registration", headers=registrations, timeout=10).json()
        form["settings"].update(enabled=True, heading="Register for the API webinar")
        requests.put(target + "/registration", headers=registrations, json=form, timeout=10).raise_for_status()
        assert requests.get(target + "/registration/export", headers=reader, timeout=10).status_code == 403
        setup = requests.post(production + "/encoder", headers=producer, timeout=45)
        setup.raise_for_status()
        encoder = setup.json()
        assert not encoder["public"] and encoder["key"]
        attendee = browser.new_page()
        watch = urljoin(base, entry["watch"])
        attendee.goto(watch)
        attendee.get_by_label("Email").fill("attendee@example.invalid")
        attendee.get_by_role("button", name="Register and watch").click()
        expect(attendee.locator("#eventStatus")).to_have_text("Waiting for the encoder")
        guest = requests.get(watch, headers={"Accept": "application/json"}, timeout=10).json()
        assert guest["status"] == "Registration required" and not guest["address"]
        exported = requests.get(target + "/registration/export", headers=registrations, timeout=10)
        exported.raise_for_status()
        assert "attendee@example.invalid" in exported.text
        with send_signal(ffmpeg, encoder["server"], encoder["key"], 50) as sending:
            wait_for_delivery(production, reader, "live", 45, sending)
            expect(attendee.locator("#eventStatus")).to_have_text("Live", timeout=20000)
            _, live_width, _ = play_event(attendee, watch, "Live", navigate=False)
        print(f"Scoped API registered live playback decoded {live_width}px. Waiting for replay.", flush=True)

        deadline = monotonic() + 420
        while monotonic() < deadline:
            current = requests.get(target, headers=reader, timeout=10)
            current.raise_for_status()
            occurrence = current.json()["occurrences"][0]
            if occurrence["delivery"] == "replay":
                break
            sleep(5)
        else:
            raise AssertionError("The recorded occurrence did not become available via the event API.")
        assert occurrence["watch"] == entry["watch"] + "?occurrence=" + str(occurrence["id"])
        assert occurrence["embed"] == entry["embed"] + "?occurrence=" + str(occurrence["id"])
        expect(attendee.locator("#eventStatus")).to_have_text("Replay", timeout=30000)
        _, replay_width, replay_duration = play_event(attendee, watch, "Replay", navigate=False)
        assert replay_duration > 0
        outside = requests.get(urljoin(base, occurrence["watch"]), headers={"Accept": "application/json"}, timeout=10).json()
        assert outside["status"] == "Registration required" and not outside["address"]
        print(f"Scoped API replay decoded {replay_width}px for {replay_duration:.1f}s. Anonymous replay stayed closed.",
              flush=True)
        dashboard.get_by_text("Developer access", exact=True).click()
        dashboard.get_by_role("button", name="Revoke Read calendar").click()
        expect(dashboard.locator("#liveApiStatus")).to_have_text("Token revoked.")
        assert requests.get(target, headers=reader, timeout=10).status_code == 401

    limited = issue("Rate limit rehearsal", ["read"])
    with requests.Session() as client:
        for _ in range(241):
            response = client.get(target, headers=limited, timeout=10)
            if response.status_code == 429:
                assert 1 <= int(response.headers["Retry-After"]) <= 60
                break
            response.raise_for_status()
        else:
            raise AssertionError("The versioned event API did not rate-limit the test token.")
    issued = requests.get(manage, headers=operator, timeout=10)
    issued.raise_for_status()
    assert all("token" not in item and "digest" not in item for item in issued.json()["credentials"])
    return dict(live_width=live_width, replay_width=replay_width, replay_duration=replay_duration,
                versioned_schedule=True, optional_publication_approval=True, stale_update_rejected=True,
                credential_panel=True, scopes_checked=True, credential_revoked=True, token_rate_limited=True,
                attendee_export_scoped=True, registration_protects_live_and_replay=True, occurrence_links=True)
