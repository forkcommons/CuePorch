"""Exercise the packaged calendar, publication switch and stable viewer address."""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import time
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import expect, sync_playwright

from browser import chromium


def check_event_calendar(page, base, auth):
    portal = base + "/api/live-events"
    publication = base + "/api/policy/publication"
    tomorrow = datetime.now(ZoneInfo("America/Denver")).date() + timedelta(days=1)
    local = str(tomorrow)
    page.goto(base + "/operator/live-events")
    page.get_by_role("button", name="New event").click()
    editor = page.locator("#newEventForm")
    editor.get_by_label("Title", exact=True).fill("City and Arts Live")
    editor.get_by_label("Time zone").fill("America/Denver")
    editor.get_by_label("Start", exact=True).fill(local + "T19:00")
    editor.get_by_label("End", exact=True).fill(local + "T20:30")
    editor.get_by_label("Link name").fill("city-and-arts-live")
    editor.get_by_role("combobox", name="Visibility").select_option("public")
    editor.get_by_role("combobox", name="Repeat").select_option("days")
    editor.get_by_label("Number of occurrences").fill("2")
    editor.get_by_role("button", name="Save event").click()
    expect(page.locator("#eventsStatus")).to_have_text("Event saved.")
    visible = page.get_by_role("link", name="Open event page").get_attribute("href")
    assert visible and urlsplit(visible).path == "/events/city-and-arts-live"
    watch = urljoin(base, urlsplit(visible).path)
    guest = requests.get(visible, timeout=10)
    assert guest.status_code == 200 and "Scheduled" in guest.text and "<iframe" not in guest.text
    assert guest.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    values = requests.get(portal, headers=auth, timeout=10).json()
    original = next(item for item in values["events"] if item["event"]["title"] == "City and Arts Live")
    assert original["publication"] == "eligible" and len(original["occurrences"]) == 2
    assert original["event"]["schedule"]["timezone"] == "America/Denver"

    body = original["event"] | {"title": "Other venue", "slug": "other-live"}
    requests.put(publication, json={"required": True}, headers=auth, timeout=10).raise_for_status()
    try:
        waiting = requests.post(portal, json=body, headers=auth, timeout=10)
        waiting.raise_for_status()
        other = waiting.json()
        assert other["publication"] == "waiting" and other["watch"] is None
        assert requests.get(base + "/events/other-live", timeout=10).status_code == 404
        provision = f"{portal}/{other['id']}/{other['occurrences'][0]['id']}/encoder"
        assert requests.post(provision, headers=auth, timeout=10).status_code == 409
        overlaps = requests.get(portal, headers=auth, timeout=10).json()["overlaps"]
        assert {original["id"], other["id"]} <= set(overlaps)
    finally:
        requests.put(publication, json={"required": False}, headers=auth, timeout=10).raise_for_status()
    assert requests.get(base + "/events/other-live/embed", timeout=10).status_code == 200
    assert requests.post(provision, timeout=10).status_code == 403
    assert requests.get(portal, timeout=10).status_code == 403

    episode = body | {"visibility": "private", "slug": "tz-weekly", "title": "Mountain time workshops",
                      "schedule": dict(timezone="America/Denver", start="2026-10-25T19:00", end="2026-10-25T20:30",
                                       repeat="weeks", interval=1, weekdays=[6], count=3)}
    fixed = "?from=2026-10-24&to=2026-11-09"
    historic = requests.post(portal + fixed, json=episode, headers=auth, timeout=10)
    historic.raise_for_status()
    copies = historic.json()["occurrences"]
    assert [item["start"] for item in copies] == [
        "2026-10-26T01:00:00+00:00", "2026-11-02T02:00:00+00:00", "2026-11-09T02:00:00+00:00",
    ]
    assert requests.get(base + "/events/tz-weekly", timeout=10).status_code == 404
    invalid = episode | {"slug": "spring-gap", "schedule": episode["schedule"] | {
        "start": "2027-03-14T02:30", "end": "2027-03-14T04:00",
    }}
    assert requests.post(portal, json=invalid, headers=auth, timeout=10).status_code == 422
    edit_url = f"{portal}/{original['id']}"
    page.get_by_role("button", name="Open production for City and Arts Live", exact=False).first.click()
    page.get_by_role("button", name="Edit event", exact=True).click()
    editor.get_by_label("Title", exact=True).fill("City and Arts Encore")
    editor.get_by_role("button", name="Save event").click()
    expect(page.locator("#eventsStatus")).to_have_text("Event saved.")
    assert "City and Arts Encore" in requests.get(visible, timeout=10).text
    old_revision = requests.put(edit_url, json=original["event"] | {"revision": original["revision"]},
                                headers=auth, timeout=10)
    assert old_revision.status_code == 409
    assert requests.put(edit_url, json=original["event"], timeout=10).status_code == 403
    planned = original["occurrences"][0]["id"]
    guest = page.context.browser.new_page()
    try:
        guest.goto(f"{watch}?occurrence={planned}")
        expect(guest.locator("#eventStatus")).to_have_text("Scheduled")
        page.get_by_role("button", name="Cancel this occurrence").click()
        expect(page.locator("#eventsStatus")).to_have_text("Occurrence withdrawn.")
        expect(guest.locator("#eventStatus")).to_have_text("This occurrence was withdrawn", timeout=12000)
        encoder = f"{edit_url}/{planned}/encoder"
        assert requests.post(encoder, headers=auth, timeout=10).status_code == 409
        other = requests.get(f"{watch}?occurrence={original['occurrences'][1]['id']}", timeout=10)
        assert other.ok and "Scheduled" in other.text
    finally:
        guest.context.close()
    registration_url = f"{edit_url}/registration"
    page.get_by_role("button", name="Registration", exact=True).click()
    registration = page.locator("#registrationEditor")
    registration.get_by_label("Require registration before playback").check()
    registration.get_by_label("Form heading").fill("Reserve a seat")
    registration.get_by_label("Save UTM campaign fields").check()
    registration.get_by_role("button", name="Save registration").click()
    expect(page.locator("#registrationStatus")).to_have_text("Registration saved.")
    guest = page.context.browser.new_page()
    registration_page = f"{watch}?occurrence={original['occurrences'][1]['id']}&utm_source=screening"
    try:
        guest.goto(registration_page)
        expect(guest.locator("#eventStatus")).to_have_text("Registration required")
        assert requests.post(registration_page, data={"email": "audience@example.invalid"}, timeout=10).status_code == 403
        form = guest.get_by_role("form", name="Reserve a seat")
        form.get_by_label("Email").fill("audience@example.invalid")
        form.get_by_role("button", name="Register and watch").click()
        expect(guest.locator("#eventStatus")).to_have_text("Scheduled")
        expect(form).to_have_count(0)
        export_url = registration_url + "/export"
        assert requests.get(export_url, timeout=10).status_code == 403
        report = requests.get(export_url, headers=auth, timeout=10)
        assert report.ok and "audience@example.invalid" in report.text and "screening" in report.text
    finally:
        guest.context.close()
    return dict(calendar_ui=True, daylight_saving_recurrence=True, rejects_skipped_hour=True,
                overlap_warning=True, automatic_publication_and_opt_in_gate=True,
                gate_switch_releases_same_event=True, public_waiting_room=True, operator_encoder_protected=True,
                revision_preserves_link=True, prevents_stale_edits=True,
                cancels_only_selected_occurrence=True, updates_open_waiting_room=True,
                event_registration_preset_editor=True, authorized_csv_and_utm=True)


def check_live_delivery(base, origin, keys, ffmpeg, port):
    assert urlsplit(origin).hostname in ("localhost", "127.0.0.1")
    assert ffmpeg.is_file(), "Bundled encoder is missing"
    now = datetime.now(timezone.utc)
    event = dict(title="Installed studio signal", slug="installed-studio-signal", visibility="public",
                 type="live", latency="standard", save_replay=True,
                 schedule=dict(timezone="UTC", start=(now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M"),
                               end=(now + timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M"), repeat="none"))
    auth = {"Authorization": "Bearer " + keys["operator"]}
    response = requests.post(base + "/api/live-events", json=event, headers=auth, timeout=10)
    response.raise_for_status()
    entry = response.json()
    assert entry["publication"] == "eligible"
    (session,) = entry["occurrences"]
    production = f"{base}/api/live-events/{entry['id']}/{session['id']}"
    assert requests.post(production + "/encoder", timeout=10).status_code == 403
    assert requests.get(production + "/health", timeout=10).status_code == 403
    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(
                f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])});"
            )
            operator.goto(base + "/operator/live-events")
            operator.get_by_role("button", name="Open production for Installed studio signal", exact=False).click()
            prepare = operator.get_by_role("button", name="Prepare encoder")
            with operator.expect_response(lambda result: result.url == production + "/encoder", timeout=45000) as prepared:
                prepare.click()
            assert prepared.value.ok, f"Encoder setup returned HTTP {prepared.value.status}"
            expect(operator.locator("#encoderSettings")).to_be_visible()
            key = operator.locator("#encoderKey").input_value()
            server = operator.locator("#rtmpServer").input_value().rstrip("/")
            endpoint = urlsplit(server)
            assert endpoint.scheme == "rtmp" and endpoint.hostname in ("localhost", "127.0.0.1")
            assert key and endpoint.port == port and endpoint.path == "/live", "Incorrect encoder endpoint"
            repeated = requests.post(production + "/encoder", headers=auth, timeout=25)
            repeated.raise_for_status()
            assert repeated.json()["key"] == key, "Retry prepared a different stream"
            stable = operator.locator("#eventWatch").get_attribute("href")
            assert stable and stable.startswith(base + "/events/")
            guest = browser.new_page()
            guest.goto(stable)
            expect(guest.locator("#eventStatus")).to_have_text("Waiting for the encoder")
            assert key not in guest.content()
            assert guest.locator('iframe[title="Event video"]').count() == 0
            gate = base + "/api/policy/replay"
            requests.put(gate, json={"required": True}, headers=auth, timeout=10).raise_for_status()
            with send_signal(ffmpeg, server, key, 65) as encoder:
                wait_for_delivery(production, auth, "live", 45, encoder)
                hosted, live_width, _ = play_event(guest, stable, "Live")
                assert encoder.poll() is None, "Playback started after the live encoder ended"
                assert key not in guest.content()
                print(f"Guest decoded live video at {live_width}px", flush=True)
                assert encoder.wait(timeout=75) == 0, "The installed encoder stream failed"
            uid = urlsplit(hosted).path.rsplit("/", 1)[-1]
            remote_api = f"{origin}/api/v1/videos/{uid}"
            action = f"{base}/api/actions/live-replay-{entry['id']}-{session['id']}"
            # PeerTube waits five minutes after RTMP disconnect before turning the live into a replay.
            replay_deadline = time.monotonic() + 420
            while time.monotonic() < replay_deadline:
                response = requests.get(action, headers=auth, timeout=10)
                if response.ok and response.json()["status"] == "waiting":
                    break
                assert response.ok or response.status_code == 404
                time.sleep(2)
            else:
                raise AssertionError("The opt-in replay gate did not hold the recorded video")
            assert requests.get(remote_api, timeout=10).status_code in (401, 403, 404)
            guest.goto(stable)
            expect(guest.locator('iframe[title="Event video"]')).to_have_count(0)
            print("Finished recording remains private under opt-in replay approval", flush=True)
            requests.put(gate, json={"required": False}, headers=auth, timeout=10).raise_for_status()
            replay_deadline = time.monotonic() + 150
            while time.monotonic() < replay_deadline:
                response = requests.get(remote_api, timeout=10)
                if response.ok:
                    video = response.json()
                    if not video["isLive"] and video["state"]["id"] == 1 and video["duration"] >= 50:
                        break
                time.sleep(2)
            else:
                raise AssertionError("PeerTube did not publish the recorded replay")
            replay, replay_width, duration = play_event(guest, stable, "Replay")
            assert replay == hosted and 50 <= duration <= 75, "Stable event did not play its completed live recording"
            withdraw = operator.get_by_role("button", name="Withdraw this occurrence")
            with operator.expect_response(lambda result: result.url == production + "/delivery", timeout=45000) as removed:
                withdraw.click()
            assert removed.value.status == 204, f"Withdrawal returned HTTP {removed.value.status}"
            expect(operator.locator("#eventsStatus")).to_have_text("Occurrence withdrawn.")
            guest.goto(stable)
            expect(guest.locator('iframe[title="Event video"]')).to_have_count(0)
            assert requests.get(remote_api, timeout=10).status_code in (401, 403, 404)
            return dict(repeat_prepare_same_stream=True, credentials_require_operator=True, live_width=live_width,
                        displayed_encoder_endpoint_accepts_stream=True,
                        replay_width=replay_width, replay_seconds=round(duration, 2),
                        opt_in_gate_holds_private_recording_and_releases_original=True,
                        original_event_and_hosted_video_reused=True, withdrawal_denies_guest_host_access=True)
        finally:
            browser.close()


def wait_for_delivery(url, auth, expected, seconds, encoder):
    deadline = time.monotonic() + seconds
    observed = "unknown"
    while time.monotonic() < deadline:
        response = requests.get(url + "/health", headers=auth, timeout=10)
        response.raise_for_status()
        observed = response.json()["state"]
        if observed == expected:
            return
        if encoder.poll() is not None:
            raise AssertionError(f"The installed RTMP encoder ended early (exit {encoder.returncode})")
        time.sleep(1)
    raise AssertionError(f"Did not observe {expected}; last state was {observed}")


def play_event(page, stable, status):
    page.goto(stable)
    expect(page.locator("#eventStatus")).to_have_text(status)
    selector = 'iframe[title="Event video"]'
    address = urljoin(stable, page.locator(selector).get_attribute("src"))
    player = page.frame_locator(selector)
    player.get_by_role("button", name="Play Video", exact=True).click(timeout=20000)
    video = player.locator("video").first
    video.wait_for(state="visible")
    width, duration = video.evaluate(
        """async v => {
            if (!v.videoWidth) await new Promise((resolve, reject) => {
                v.addEventListener('loadeddata', resolve, {once: true});
                setTimeout(() => reject(Error('No decoded video frame')), 20000);
            });
            return [v.videoWidth, Number.isFinite(v.duration) ? v.duration : 0];
        }"""
    )
    assert width >= 240, f"Expected a decoded video rendition, got {width}px"
    return address, width, duration


@contextmanager
def send_signal(ffmpeg, server, key, seconds):
    with subprocess.Popen(
        [str(ffmpeg), "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi",
         "-i", "testsrc2=size=320x180:rate=24", "-f", "lavfi", "-i", "sine=frequency=550",
         "-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast", "-g", "48",
         "-sc_threshold", "0", "-c:a", "aac", "-f", "flv", f"{server}/{key}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as encoder:
        try:
            yield encoder
        finally:
            if encoder.poll() is None:
                encoder.terminate()
                encoder.wait(timeout=10)


def check_registered_live(base, origin, keys, home, ffmpeg, port):
    assert urlsplit(origin).hostname in ("localhost", "127.0.0.1")
    now = datetime.now(timezone.utc)
    date = lambda delta: (now + timedelta(minutes=delta)).strftime("%Y-%m-%dT%H:%M")
    event = dict(title="Installed registered webinar", slug="installed-registered-webinar", visibility="public",
                 type="webinar", latency="standard", save_replay=False,
                 schedule=dict(timezone="UTC", start=date(-1), end=date(12), repeat="none"))
    auth = {"Authorization": "Bearer " + keys["operator"]}
    portal = base + "/api/live-events"
    response = requests.post(portal, json=event, headers=auth, timeout=10)
    response.raise_for_status()
    entry = response.json()
    (occurrence,) = entry["occurrences"]
    uid, opens = entry["id"], occurrence["id"]
    registration = f"{portal}/{uid}/registration"
    status = requests.get(registration, headers=auth, timeout=10)
    status.raise_for_status()
    form = status.json()
    form.update(settings=form["settings"] | {"enabled": True, "heading": "Sign up for the live webinar"})
    update = requests.put(registration, json=form, headers=auth, timeout=10)
    update.raise_for_status()
    production = f"{portal}/{uid}/{opens}"
    setup = requests.post(production + "/encoder", headers=auth, timeout=25)
    assert setup.ok, f"Registered encoder returned HTTP {setup.status_code}"
    encoder = setup.json()
    endpoint = urlsplit(encoder["server"])
    assert not encoder["public"] and endpoint.scheme == "rtmp" and endpoint.port == port
    assert endpoint.hostname in ("localhost", "127.0.0.1") and endpoint.path == "/live"
    with closing(sqlite3.connect(home / "institution.sqlite")) as database:
        (hosted,) = database.execute("SELECT remote FROM live_occurrence WHERE event=? AND opens=?", (uid, opens)).fetchone()
    source = f"{origin}/api/v1/videos/{hosted}"
    watch = base + entry["watch"] + f"?occurrence={opens}"
    outside = {"Accept": "application/json"}
    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            attendee, anonymous = browser.new_page(), browser.new_page()
            attendee.goto(watch)
            signup = attendee.get_by_role("form", name="Sign up for the live webinar")
            signup.get_by_label("Email").fill("viewer@example.invalid")
            signup.get_by_role("button", name="Register and watch").click()
            expect(signup).to_have_count(0)
            expect(attendee.locator("#eventStatus")).to_have_text("Waiting for the encoder")
            with send_signal(ffmpeg, encoder["server"], encoder["key"], 65) as process:
                wait_for_delivery(production, auth, "live", 45, process)
                expect(attendee.locator("#eventStatus")).to_have_text("Live", timeout=16000)
                video = attendee.locator("video")
                video.evaluate("v => {v.muted = true; void v.play().catch(() => {});}")
                attendee.wait_for_function("document.querySelector('video').videoWidth >= 240", timeout=25000)
                width = video.evaluate("v => v.videoWidth")
                media = urljoin(watch, attendee.locator("#eventPlayer").get_attribute("data-address"))
                assert urlsplit(media).path.startswith("/event-media/")
                assert requests.get(media, timeout=10).ok
                assert requests.get(source, timeout=10).status_code in (401, 403, 404)
                anonymous.goto(watch)
                expect(anonymous.locator("#eventStatus")).to_have_text("Registration required")
                assert not requests.get(watch, headers=outside, timeout=10).json()["address"]
                assert anonymous.locator("video, iframe[title='Event video']").count() == 0
                current = update.json()
                current["settings"]["heading"] = "Register for the updated webinar"
                requests.put(registration, json=current, headers=auth, timeout=10).raise_for_status()
                assert requests.get(media, timeout=10).status_code == 404
                expect(attendee.locator("video")).to_have_count(0, timeout=12000)
                removed = requests.delete(production + "/delivery", headers=auth, timeout=25)
                assert removed.status_code == 204
                print(f"Registered guest decoded private {width}px live video", flush=True)
            return dict(decoded_video_width=width, registration_required=True, peertube_copy_private=True,
                        waiting_room_opens_player_automatically=True, amended_form_revokes_media=True,
                        withdrawal_succeeded_while_live=True)
        finally:
            browser.close()
