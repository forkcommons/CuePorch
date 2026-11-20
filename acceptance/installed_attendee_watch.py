"""Decode registered live and replay media while exercising watch-time consent."""

from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
from urllib.parse import urljoin

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_events import play_event, send_signal, wait_for_delivery


def check_attendee_watch(base, keys, ffmpeg):
    staff = {"Authorization": "Bearer " + keys["operator"]}
    home = base + "/api/live-events"
    now = datetime.now(timezone.utc)
    scheduled = requests.post(home, headers=staff, timeout=10, json=dict(
        title="Attendee watch-time rehearsal", slug="attendee-watch-rehearsal", type="webinar", visibility="public",
        latency="standard", save_replay=True, archive_replay=False,
        schedule=dict(timezone="UTC", repeat="days", count=2,
                      start=(now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                      end=(now + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M"))))
    scheduled.raise_for_status()
    event = scheduled.json()
    uid = event["id"]
    first, second = (slot["id"] for slot in event["occurrences"])
    target = f"{home}/{uid}/{first}"
    form_path = f"{home}/{uid}/registration"
    form = requests.get(form_path, headers=staff, timeout=10).json()
    assert not form["settings"]["attendance_analytics"]
    form["settings"]["enabled"] = True
    requests.put(form_path, headers=staff, json=form, timeout=10).raise_for_status()
    watch = urljoin(base, event["watch"])

    def roster():
        result = requests.get(target + "/registrants", headers=staff, timeout=10)
        result.raise_for_status()
        return {row["email"]: row["watch_time"] for row in result.json()["attendees"]}

    def await_metrics(phase, email):
        until = monotonic() + 18
        while monotonic() < until:
            stats = roster()
            row = stats.get(email)
            if row and row[phase + "_seconds"] > 1:
                return stats
            sleep(.25)
        raise AssertionError(f"No {phase} watch time for the consenting attendee. Roster {roster()}")

    def show_play_for(page, phase, seconds=2.3):
        expect(page.locator("#eventStatus")).to_have_text(phase, timeout=20000)
        _, width, duration = play_event(page, watch, phase, navigate=False)
        page.wait_for_function("""async seconds => {
            const video = document.querySelector('#eventPlayer video');
            const position = video.currentTime;
            await new Promise(resolve => setTimeout(resolve, seconds * 1000));
            return !video.paused && video.currentTime - position > 1.2;
        }""", arg=seconds, timeout=14000)
        page.locator("#eventPlayer video").evaluate("video => video.pause()")
        return width, duration

    field = "Share this viewing session's watch time with the event organizer"
    sharing_address = base + "/api/event-playback/withdraw-sharing"
    assert requests.get(target + "/registrants", timeout=10).status_code == 403
    assert requests.post(sharing_address, timeout=10).status_code == 404
    with sync_playwright() as pw, chromium(pw) as browser:
        participant = browser.new_page()
        participant.goto(watch)
        expect(participant.get_by_role("checkbox", name=field, exact=False)).to_have_count(0)
        operator = browser.new_page()
        operator.goto(base + "/operator/live-events")
        operator.get_by_label("Operator credential").fill(keys["operator"])
        operator.get_by_role("button", name="Open live events").click()
        operator.get_by_role("button", name="Open production for Attendee watch-time rehearsal", exact=False).first.click()
        operator.get_by_role("button", name="Registration", exact=True).click()
        operator.get_by_label("Offer attendees a choice to share watch time").check()
        operator.get_by_role("button", name="Save registration", exact=True).click()
        expect(operator.locator("#registrationStatus")).to_have_text("Registration saved.")
        configured = requests.post(target + "/encoder", headers=staff, timeout=45)
        configured.raise_for_status()
        encoder = configured.json()

        opted_in = "consenting@example.invalid"
        declined = browser.new_page()
        gpc = browser.new_page(extra_http_headers={"Sec-GPC": "1"})
        for page, email in ((participant, opted_in), (declined, "declined@example.invalid"), (gpc, "gpc@example.invalid")):
            page.goto(watch)
            consent = page.get_by_role("checkbox", name=field, exact=False)
            if page == gpc:
                expect(consent).to_have_count(0)
            else:
                expect(consent).not_to_be_checked()
                if page == participant:
                    consent.check()
            page.get_by_label("Email").fill(email)
            page.get_by_role("button", name="Register and watch").click()
            expect(page.locator("#eventStatus")).to_have_text("Waiting for the encoder")

        grant = participant.locator("#eventLobby").get_attribute("data-viewer-grant")
        other = requests.get(watch, params={"occurrence": second}, timeout=10,
                             headers={"Accept": "application/json", "X-CuePorch-Event-Viewer": grant}).json()
        assert other["status"] == "Registration required" and not other["analytics_token"]
        with send_signal(ffmpeg, encoder["server"], encoder["key"], 72) as pushing:
            wait_for_delivery(target, staff, "live", 45, pushing)
            live_width, _ = show_play_for(participant, "Live")
            show_play_for(declined, "Live")
            show_play_for(gpc, "Live")
            assert not gpc.locator("#eventPlayer").get_attribute("data-analytics")
            measured = await_metrics("live", opted_in)
            assert measured["declined@example.invalid"] is None and measured["gpc@example.invalid"] is None
            total = requests.get(target + "/analytics", headers=staff, timeout=10).json()["live"]
            assert total["plays"] == 2 and total["watched_seconds"] > measured[opted_in]["live_seconds"]
        print(f"Registered 320px-class live video and viewer consent passed at {live_width}px. Waiting for replay.", flush=True)

        expect(participant.locator("#eventStatus")).to_have_text("Replay", timeout=420000)
        replay_width, duration = show_play_for(participant, "Replay")
        measured = await_metrics("replay", opted_in)
        assert measured[opted_in]["live_seconds"] > 1 and duration > 0
        participant.get_by_role("button", name="Stop sharing my watch time").click()
        expect(participant.locator("#attendeeSharingStatus")).to_have_text("Watch-time sharing stopped for this viewing session.")
        assert roster()[opted_in] is None
        show_play_for(participant, "Replay")
        assert roster()[opted_in] is None
        retained = requests.get(target + "/analytics", headers=staff, timeout=10).json()
        assert retained["live"]["plays"] == 2 and retained["replay"]["plays"] >= 1
    return dict(actual_live_width=live_width, actual_replay_width=replay_width, replay_duration=duration,
                shared_live_seconds=measured[opted_in]["live_seconds"], shared_replay_seconds=measured[opted_in]["replay_seconds"],
                explicit_choice_default_off=True, declined_still_aggregated=True, gpc_excluded=True,
                sharing_denied_in_other_occurrence=True, withdrawal_keeps_playback_and_removes_named_metrics=True)
