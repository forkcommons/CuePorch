"""Exercise the packaged calendar, publication switch and stable viewer address."""

from contextlib import closing, contextmanager
from array import array
from datetime import datetime, timedelta, timezone
import json
import math
import socket
import sqlite3
import subprocess
import time
import uuid
from urllib.parse import urlencode, urljoin, urlsplit
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


def check_event_setup(page, base, auth):
    portal = base + "/api/live-events"
    tomorrow = datetime.now(ZoneInfo("America/Denver")).date() + timedelta(days=1)
    page.goto(base + "/operator/live-events")
    editor = page.locator("#newEventForm")

    def combo(name):
        return editor.get_by_role("combobox", name=name, exact=True)

    def schedule(title, slug, hour):
        if title is not None:
            editor.get_by_label("Title", exact=True).fill(title)
        editor.get_by_label("Link name").fill(slug)
        editor.get_by_label("Time zone").fill("America/Denver")
        editor.get_by_label("Start", exact=True).fill(f"{tomorrow}T{hour}:00")
        editor.get_by_label("End", exact=True).fill(f"{tomorrow}T{hour}:30")

    def save(slug):
        editor.get_by_role("button", name="Save event").click()
        expect(page.locator("#eventsStatus")).to_have_text("Event saved.")
        response = requests.get(portal, headers=auth, timeout=10)
        response.raise_for_status()
        return next(item for item in response.json()["events"] if item["slug"] == slug)

    def read(path):
        response = requests.get(path, headers=auth, timeout=10)
        response.raise_for_status()
        return response.json()

    created = {}
    defaults = {
        "live": ("encoder", "standard", True, True, False, False),
        "webinar": ("browser", "standard", True, True, True, True),
        "team": ("browser", "low", True, True, False, False),
        "learning": ("browser", "standard", False, True, True, True),
        "custom": ("browser", "standard", False, False, False, False),
    }
    for kind, expected in defaults.items():
        page.get_by_role("button", name="New event").click()
        combo("Setup").select_option("type")
        combo("Event type").select_option(kind)
        slug = "setup-" + kind
        schedule("Setup " + kind, slug, "09")
        if kind == "webinar":
            editor.get_by_label("Description").fill("Live demonstrations and audience questions")
            combo("Visibility").select_option("public")
            combo("Caption language").select_option("fr")
            editor.get_by_label("Live DVR window in seconds").fill("180")
        item = save(slug)
        created[kind] = item
        target = f"{portal}/{item['id']}"
        config = read(target + "/setup")
        assert (config["method"], item["event"]["latency"], config["chat"]["enabled"],
                config["captions"]["enabled"], config["registration"], config["qa"]["enabled"]) == expected
        assert not config["preview_encoder"] and not config["qa"]["review"]
        assert item["publication"] == ("eligible" if kind == "webinar" else "private")

    source = created["webinar"]
    webinar = f"{portal}/{source['id']}"
    opens = source["occurrences"][0]["id"]
    production = f"{webinar}/{opens}"
    form = page.context.browser.new_page()
    try:
        form.goto(base + source["watch"] + f"?occurrence={opens}")
        expect(form.locator("#eventStatus")).to_have_text("Registration required")
        form.get_by_label("Email").fill("prior-attendee@example.invalid")
        form.get_by_role("button", name="Register and watch").click()
        expect(form.locator("#eventStatus")).to_have_text("Scheduled")
    finally:
        form.context.close()
    requests.put(webinar + "/player", json={"show_schedule": True, "customize": True, "accent": "#18a2ad"},
                 headers=auth, timeout=10).raise_for_status()
    requests.put(production + "/chat", json={"enabled": True, "interval": 12}, headers=auth, timeout=10).raise_for_status()
    requests.put(production + "/captions", json={"enabled": True, "language": "es"}, headers=auth, timeout=10).raise_for_status()
    requests.put(production + "/engagement", json={"feature": "qa", "enabled": True, "anonymous": True, "review": False},
                 headers=auth, timeout=10).raise_for_status()
    assert requests.get(webinar + "/setup", timeout=10).status_code == 403
    assert "prior-attendee@example.invalid" in read(webinar + "/registration")["registrants"][0]["email"]

    page.get_by_role("button", name="New event").click()
    combo("Setup").select_option("reuse")
    combo("Reuse event settings").select_option(source["id"])
    expect(page.locator("#eventsStatus")).to_have_text("Event settings loaded.")
    expect(editor.get_by_label("Title", exact=True)).to_have_value("Setup webinar")
    expect(editor.get_by_label("Start", exact=True)).to_have_value("")
    expect(editor.get_by_label("Link name")).to_have_value("")
    expect(combo("Visibility")).to_have_value("private")
    expect(editor.get_by_label("Live DVR window in seconds")).to_have_value("180")
    expect(editor.get_by_label("Allow anonymous questions")).to_be_checked()
    expect(combo("Caption language")).to_have_value("es")
    expect(editor.get_by_label("Chat delay in seconds")).to_have_value("12")
    schedule(None, "setup-reused", "10")
    combo("Repeat").select_option("days")
    editor.get_by_label("Number of occurrences").fill("2")
    combo("Latency").select_option("fail-safe")
    copied = save("setup-reused")
    assert copied["id"] != source["id"] and copied["publication"] == "private"
    assert copied["event"]["latency"] == "fail-safe"
    assert copied["event"]["description"] == source["event"]["description"]
    assert all(item["id"] != opens and item["delivery"] == "scheduled" for item in copied["occurrences"])
    assert len(copied["occurrences"]) == 2
    copy = f"{portal}/{copied['id']}"
    assert read(copy + "/player")["accent"] == "#18a2ad"
    for item in copied["occurrences"]:
        occurrence = f"{copy}/{item['id']}"
        assert read(occurrence + "/chat")["interval"] == 12
        assert read(occurrence + "/captions")["language"] == "es"
        assert read(occurrence + "/engagement")["qa"]["policy"]["anonymous"]
    first = f"{copy}/{copied['occurrences'][0]['id']}"
    second = f"{copy}/{copied['occurrences'][1]['id']}"
    requests.put(first + "/captions", json={"enabled": False, "language": "en"}, headers=auth, timeout=10).raise_for_status()
    assert not read(first + "/captions")["enabled"] and read(second + "/captions")["enabled"]
    exports = requests.get(copy + "/registration/export", headers=auth, timeout=10)
    exports.raise_for_status()
    assert "prior-attendee@example.invalid" not in exports.text
    assert read(copy + "/registration")["settings"]["enabled"]

    page.get_by_role("button", name="New event").click()
    combo("Event type").select_option("webinar")
    schedule("A scratch webinar", "setup-scratch", "11")
    scratch = save("setup-scratch")
    draft = read(f"{portal}/{scratch['id']}/setup")
    assert not draft["registration"] and not draft["chat"]["enabled"] and not draft["captions"]["enabled"]
    return dict(types=list(defaults), scratch_without_preset=True, editable_latency=True,
                preferred_method=True, viewer_registration_enforced=True, private_copy_new_schedule=True,
                reused_player_and_audience_settings=True, occurrence_override_isolated=True,
                new_event_has_no_transferred_attendees=True, setup_staff_only=True)


def check_event_embed_policy(page, browser, base, auth, exhibition, venue):
    portal = base + "/api/live-events"
    tomorrow = datetime.now(ZoneInfo("America/Denver")).date() + timedelta(days=1)
    page.goto(base + "/operator/live-events")
    page.get_by_role("button", name="New event").click()
    editor = page.locator("#newEventForm")
    editor.get_by_label("Title", exact=True).fill("Ticketed community screening")
    editor.get_by_label("Link name").fill("domain-screening")
    editor.get_by_label("Time zone").fill("America/Denver")
    editor.get_by_label("Start", exact=True).fill(f"{tomorrow}T12:00")
    editor.get_by_label("End", exact=True).fill(f"{tomorrow}T13:00")
    editor.get_by_role("combobox", name="Visibility", exact=True).select_option("public")
    scope = editor.get_by_role("combobox", name="Where may this event be embedded?")
    scope.select_option("domains")
    editor.get_by_label("Allowed domains, one per line").fill(urlsplit(venue).netloc)
    editor.get_by_label("Require event registration").check()
    editor.get_by_role("button", name="Save event").click()
    expect(page.locator("#eventsStatus")).to_have_text("Event saved.")
    listing = requests.get(portal, headers=auth, timeout=10)
    listing.raise_for_status()
    event = next(item for item in listing.json()["events"] if item["slug"] == "domain-screening")
    watch, embed = base + event["watch"], base + event["embed"]
    assert requests.get(watch, timeout=10).status_code == 200
    assert requests.get(embed, timeout=10).status_code == 404
    denied = dict(Referer=base + "/", **{"Sec-Fetch-Dest": "iframe"})
    assert requests.get(embed, headers=denied, timeout=10).status_code == 404
    allowed = dict(Referer=venue + "/", **{"Sec-Fetch-Dest": "iframe"})
    response = requests.get(embed, headers=allowed, timeout=10)
    assert response.ok and urlsplit(venue).netloc in response.headers["Content-Security-Policy"]
    assert requests.get(embed, headers=allowed | {"Accept": "application/json"}, timeout=10).status_code == 404
    assert requests.post(embed, headers=allowed, data={"email": "outsider@example.invalid"}, timeout=10).status_code == 404

    previous = exhibition.embed
    exhibition.embed = embed
    guest = browser.new_page()
    try:
        guest.goto(venue)
        frame = guest.frame_locator("iframe")
        expect(frame.locator("#eventStatus")).to_have_text("Registration required")
        grant = frame.locator("#eventLobby").get_attribute("data-frame-grant")
        assert grant
        frame.get_by_label("Email").fill("venue-attendee@example.invalid")
        frame.get_by_role("button", name="Register and watch").click()
        expect(frame.locator("#eventStatus")).to_have_text("Scheduled")
        with guest.expect_response(lambda value: value.url == embed and value.request.headers.get("accept") == "application/json") as refresh:
            frame.get_by_role("button", name="Check stream status").click()
        assert refresh.value.status == 200
        assert refresh.value.json()["status"] == "Scheduled"

        page.get_by_role("button", name="Edit event", exact=True).click()
        expect(scope).to_have_value("domains")
        scope.select_option("disabled")
        editor.get_by_role("button", name="Save event").click()
        expect(page.locator("#eventsStatus")).to_have_text("Event saved.")
        expect(page.locator("#eventEmbedField")).to_be_hidden()
        assert requests.get(watch, timeout=10).status_code == 200
        assert requests.get(embed, headers=allowed | {"X-CuePorch-Event-Frame": grant, "Accept": "application/json"},
                            timeout=10).status_code == 404
        expect(frame.locator("#eventStatus")).to_have_text("This event is unavailable", timeout=13000)
    finally:
        guest.close()
        exhibition.embed = previous
    return dict(allowed_venue_registration_and_status=True, unrelated_site_denied=True, watch_link_stable=True,
                frame_ancestors=True, unsigned_status_and_form_denied=True, embed_off_revokes_old_frame=True)


def check_live_delivery(base, origin, keys, home, ffmpeg, port):
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
    metrics_url = production + "/analytics"
    observation_url = base + "/api/event-playback"
    assert requests.post(production + "/encoder", timeout=10).status_code == 403
    assert requests.get(production + "/health", timeout=10).status_code == 403
    assert requests.get(metrics_url, timeout=10).status_code == 403
    assert requests.post(observation_url, json={}, timeout=10).status_code == 404

    def metrics():
        response = requests.get(metrics_url, headers=auth, timeout=10)
        response.raise_for_status()
        return response.json()

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
            expect(operator.get_by_role("button", name="Withdraw this occurrence")).to_be_enabled()
            key = operator.locator("#encoderKey").input_value()
            server = operator.locator("#rtmpServer").input_value().rstrip("/")
            endpoint = urlsplit(server)
            assert endpoint.scheme == "rtmp" and endpoint.hostname in ("localhost", "127.0.0.1")
            assert key and endpoint.port == port and endpoint.path == "/live", "Incorrect encoder endpoint"
            repeated = requests.post(production + "/encoder", headers=auth, timeout=25)
            repeated.raise_for_status()
            assert repeated.json()["key"] == key, "Retry prepared a different stream"
            with closing(sqlite3.connect(home / "institution.sqlite")) as database:
                (uid,) = database.execute("SELECT remote FROM live_occurrence WHERE event=? AND opens=?",
                                          (entry["id"], session["id"])).fetchone()
            remote_api = f"{origin}/api/v1/videos/{uid}"
            stable = operator.locator("#eventWatch").get_attribute("href")
            assert stable and stable.startswith(base + "/events/")
            guest = browser.new_page()
            guest.goto(stable)
            expect(guest.locator("#eventStatus")).to_have_text("Waiting for the encoder")
            assert key not in guest.content()
            assert guest.locator("#eventPlayer video").count() == 0
            operator.get_by_text("Audience analytics", exact=True).click()
            stats_rows = operator.locator("#eventAnalyticsRows tr")
            expect(stats_rows.first.locator("td")).to_have_text(["0", "0", "0", "0", "0 s", "0 s"])
            gate = base + "/api/policy/replay"
            requests.put(gate, json={"required": True}, headers=auth, timeout=10).raise_for_status()
            with send_signal(ffmpeg, server, key, 95) as encoder:
                wait_for_delivery(production, auth, "live", 45, encoder)
                expect(guest.locator("#eventStatus")).to_have_text("Live", timeout=30000)
                live, live_width, _ = play_event(guest, stable, "Live", navigate=False)
                assert live.startswith(base + "/event-media/")
                assert requests.get(remote_api, timeout=10).status_code in (401, 403, 404)
                embed_url = base + entry["embed"] + f"?occurrence={session['id']}"
                guest_embed = browser.new_page()
                guest_embed.goto(embed_url)
                expect(stats_rows.first.locator("td").nth(2)).to_have_text("2", timeout=15000)
                assert metrics()["live"]["plays"] == 1, "Loading a second player counted as playback"
                _, embedded_width, _ = play_event(guest_embed, embed_url, "Live", navigate=False)
                try:
                    expect(stats_rows.first.locator("td").nth(0)).to_have_text("2", timeout=15000)
                except AssertionError as error:
                    state = [page.locator("#eventPlayer video").evaluate("""v => ({
                        paused: v.paused, time: v.currentTime, ready: v.readyState, hidden: document.hidden
                    })""") for page in (guest, guest_embed)]
                    raise AssertionError(f"Expected two live viewers, got {metrics()['live']}; players {state}") from error
                health = requests.get(production + "/health", headers=auth, timeout=10)
                assert health.ok and health.json()["viewers"] == 2
                guest_embed.locator("#eventPlayer video").evaluate("video => video.pause()")
                expect(stats_rows.first.locator("td").nth(0)).to_have_text("1", timeout=15000)
                assert metrics()["live"]["peak"] == 2
                guest_embed.close()
                untracked = browser.new_page(extra_http_headers={"DNT": "1"})
                with untracked.expect_response(lambda result: result.url == stable) as guest_page:
                    play_event(untracked, stable, "Live")
                assert guest_page.value.ok and untracked.locator("#eventPlayer").get_attribute("data-analytics") == ""
                untracked.close()
                assert metrics()["live"]["loads"] == 2
                assert encoder.poll() is None, "Playback started after the live encoder ended"
                assert key not in guest.content()
                print(f"Guest decoded {live_width}px live video. A second viewer raised current and peak to two; "
                      "pausing reduced current to one; the opted-out viewer played without adding a load.", flush=True)
                assert encoder.wait(timeout=100) == 0, "The installed encoder stream failed"
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
            expect(guest.locator("#eventPlayer video")).to_have_count(0)
            print("Finished recording remains private under opt-in replay approval", flush=True)
            requests.put(gate, json={"required": False}, headers=auth, timeout=10).raise_for_status()
            replay_deadline = time.monotonic() + 150
            while time.monotonic() < replay_deadline:
                response = requests.get(stable, headers={"Accept": "application/json"}, timeout=10)
                if response.ok and response.json()["status"] == "Replay":
                    break
                time.sleep(2)
            else:
                raise AssertionError("CuePorch did not publish the recorded replay")
            replay_token = response.json()["analytics_token"]
            assert replay_token
            expect(guest.locator("#eventStatus")).to_have_text("Replay", timeout=20000)
            replay, replay_width, duration = play_event(guest, stable, "Replay", navigate=False)
            assert replay.startswith(base + "/event-media/") and 80 <= duration <= 105
            assert requests.get(live, timeout=10).status_code == 404
            old_token_replay = live.rsplit("/", 1)[0] + "/" + replay.rsplit("/", 1)[-1]
            assert requests.get(old_token_replay, timeout=10).status_code == 404
            with closing(sqlite3.connect(home / "institution.sqlite")) as database:
                assert database.execute("SELECT remote,delivery FROM live_occurrence WHERE event=? AND opens=?",
                                        (entry["id"], session["id"])).fetchone() == (uid, "replay")
            assert requests.get(remote_api, timeout=10).status_code in (401, 403, 404)
            print(f"Released stable-link replay decoded at {replay_width}px for {duration:.2f}s, with the old live token denied.", flush=True)
            expect(stats_rows.nth(1).locator("td").nth(0)).to_have_text("1", timeout=15000)
            report = metrics()
            assert report["live"]["current"] == 0 and report["live"]["peak"] == 2 and report["live"]["plays"] == 2
            assert report["live"]["loads"] == 2 and report["live"]["watched_seconds"] > 0
            assert report["replay"]["loads"] == 1 and report["replay"]["plays"] == 1, report["replay"]
            withdraw = operator.get_by_role("button", name="Withdraw this occurrence")
            with operator.expect_response(lambda result: result.url == production + "/delivery", timeout=45000) as removed:
                withdraw.click()
            assert removed.value.status == 204, f"Withdrawal returned HTTP {removed.value.status}"
            expect(operator.locator("#eventsStatus")).to_have_text("Occurrence withdrawn.")
            expect(guest.locator("#eventPlayer video")).to_have_count(0, timeout=15000)
            assert requests.get(replay, timeout=10).status_code == 404
            assert requests.post(observation_url, headers={"X-CuePorch-Playback": replay_token}, json={},
                                 timeout=10).status_code == 404
            assert metrics()["replay"]["current"] == 0
            return dict(repeat_prepare_same_stream=True, credentials_require_operator=True, live_width=live_width,
                        displayed_encoder_endpoint_accepts_stream=True, embed_live_width=embedded_width,
                        replay_width=replay_width, replay_seconds=round(duration, 2),
                        opt_in_gate_holds_private_recording_and_releases_original=True,
                        original_event_and_hosted_video_reused=True, peer_copy_kept_private=True,
                        old_live_token_cannot_read_replay=True,
                        withdrawal_revokes_existing_replay_manifest=True,
                        audience_analytics_requires_operator=True, separate_live_and_replay_metrics=report,
                        do_not_track_honored=True, paused_player_removed_from_current_but_peak_retained=True,
                        withdrawal_revokes_playback_telemetry=True,
                        withdraw_label_updates_on_original_production_panel=True)
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


def play_event(page, stable, status, navigate=True):
    if navigate:
        page.goto(stable)
    expect(page.locator("#eventStatus")).to_have_text(status)
    address = urljoin(stable, page.locator("#eventPlayer").get_attribute("data-address"))
    video = page.locator("#eventPlayer video")
    page.wait_for_function("document.querySelector('#eventPlayer video')?.readyState >= 2", timeout=30000)
    width, duration = video.evaluate(
        """async v => {
            v.muted = true;
            await Promise.race([v.play(), new Promise((_, reject) =>
                setTimeout(() => reject(Error('No decoded video frame')), 10000))]);
            return [v.videoWidth, Number.isFinite(v.duration) ? v.duration : 0];
        }"""
    )
    assert width >= 240, f"Expected a decoded video rendition, got {width}px"
    return address, width, duration


@contextmanager
def send_signal(ffmpeg, server, key, seconds, muxer="flv", *, size="320x180", video_source=None):
    address = f"{server}/{key}" if key else server
    with subprocess.Popen(
        [str(ffmpeg), "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi",
         "-i", video_source or f"testsrc2=size={size}:rate=24", "-f", "lavfi", "-i", "sine=frequency=550",
         "-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast", "-g", "48",
         "-sc_threshold", "0", "-c:a", "aac", "-f", muxer, address],
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


def prepare_live_scenes(operator, base, studio, auth, scratch, ffmpeg):
    def image(name, color):
        path = scratch / (name + ".png")
        subprocess.run([str(ffmpeg), "-v", "error", "-f", "lavfi", "-i", f"color={color}:s=160x90",
                        "-frames:v", "1", "-threads", "1", str(path)], check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with path.open("rb") as content:
            uploaded = requests.post(base + "/api/library/uploads", headers=auth, timeout=30,
                                     data={"upload_id": str(uuid.uuid4())}, files={"file": (path.name, content, "image/png")})
        uploaded.raise_for_status()
        return uploaded.json()["id"]

    logo, slate = image("Station logo", "yellow"), image("Second slate", "lime")
    clip = scratch / "Archive contribution.mp4"
    subprocess.run([str(ffmpeg), "-v", "error", "-f", "lavfi", "-i", "color=magenta:s=320x180:r=24:d=28",
                    "-f", "lavfi", "-i", "sine=frequency=1232:duration=28", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)], check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with clip.open("rb") as content:
        uploaded = requests.post(base + "/api/library/uploads", headers=auth, timeout=30,
                                 data={"upload_id": str(uuid.uuid4())}, files={"file": (clip.name, content, "video/mp4")})
    uploaded.raise_for_status()
    endpoint = studio.removesuffix("browser") + "scenes"
    assert requests.get(endpoint, timeout=10).status_code == 403
    assert requests.get(f"{endpoint}/image/{logo}", timeout=10).status_code == 403
    operator.reload()
    operator.get_by_text("Scenes and graphics", exact=True).click()
    panel = operator.locator("#studioScenes")
    expect(panel.get_by_role("button", name="Add scene")).to_be_enabled()

    def save(button):
        with operator.expect_response(lambda response: response.url == endpoint and response.request.method == "PUT") as written:
            panel.get_by_role("button", name=button, exact=True).click()
        assert written.value.ok, f"Scene save returned HTTP {written.value.status}"

    editor = panel.locator("#studioSceneForm")
    panel.get_by_label("Graphic color", exact=True).fill("#2146e0")
    panel.get_by_label("Event logo").select_option(logo)
    save("Save brand")
    panel.get_by_label("Scene template").select_option(label="Opening title")
    save("Add scene")
    editor.get_by_label("Scene color").fill("#ba202b")
    editor.get_by_label("Scene background").select_option("solid")
    editor.get_by_label("Lower-third primary text").fill("Festival opening")
    editor.get_by_label("Lower-third secondary text").fill("Host greeting")
    save("Save scene")
    save("Duplicate scene")
    editor.get_by_label("Scene name").fill("Film program")
    editor.get_by_label("Scene image").select_option(slate)
    save("Save scene")
    save("Duplicate scene")
    editor.get_by_label("Scene name").fill("Archive clip")
    editor.get_by_label("Scene image").select_option("")
    editor.get_by_label("Scene video").select_option(label="Archive contribution")
    save("Save scene")
    panel.get_by_role("button", name="Move scene up (Film program)").click()
    expect(panel.locator("#studioSceneList li").first).to_contain_text("Film program")
    operator.reload()
    operator.get_by_text("Scenes and graphics", exact=True).click()
    expect(panel.locator("#studioSceneList li").first).to_contain_text("Film program")
    panel.get_by_role("button", name="Opening title", exact=True).click()
    panel.get_by_role("button", name="Take scene").click()
    expect(panel.locator("#studioSceneStatus")).to_have_text("On stage: Opening title")
    return panel


def wait_broadcast_pixel(video, x, y, rgb, tolerance=45):
    observed = video.evaluate("""async (video, {x,y,rgb,tolerance}) => {
        const canvas = document.createElement('canvas');
        canvas.width = canvas.height = 1;
        const context = canvas.getContext('2d', {willReadFrequently: true});
        const start = video.currentTime;
        const end = performance.now() + 45000;
        let result;
        while (performance.now() < end) {
            if (video.readyState >= 2 && video.videoWidth) {
                context.drawImage(video, video.videoWidth*x, video.videoHeight*y,1,1,0,0,1,1);
                result = [...context.getImageData(0,0,1,1).data].slice(0,3);
                if (result.every((value, index) => Math.abs(value-rgb[index]) < tolerance)) return {matched:true};
            }
            await new Promise(resolve => setTimeout(resolve, 250));
        }
        return {matched:false, result, start, currentTime:video.currentTime, paused:video.paused,
                readyState:video.readyState, width:video.videoWidth,
                bufferedEnd:video.buffered.length ? video.buffered.end(video.buffered.length-1) : null};
    }""", {"x": x, "y": y, "rgb": rgb, "tolerance": tolerance})
    assert observed["matched"], f"Guest did not decode {rgb} at {x}, {y}: {observed}"


def check_broadcast_graphics(guest, panel):
    video = guest.locator("#eventPlayer video")

    def pixel(x, y, rgb):
        panel.page.wait_for_function("""({x,y,rgb}) => {
            const canvas = document.querySelector('#recordCanvas');
            const result = [...canvas.getContext('2d').getImageData(canvas.width*x,canvas.height*y,1,1).data].slice(0,3);
            return result.every((value, index) => Math.abs(value-rgb[index]) < 45);
        }""", arg={"x": x, "y": y, "rgb": rgb}, timeout=12000)
        wait_broadcast_pixel(video, x, y, rgb)

    pixel(.22, .31, [186, 32, 43])
    pixel(.91, .89, [255, 255, 0])
    panel.get_by_role("button", name="Show lower third").click()
    pixel(.05, .81, [33, 70, 224])
    panel.get_by_role("button", name="Hide lower third").click()
    pixel(.05, .81, [186, 32, 43])
    panel.get_by_role("button", name="Film program", exact=True).click()
    panel.get_by_role("button", name="Take scene").click()
    expect(panel.locator("#studioSceneStatus")).to_have_text("On stage: Film program")
    pixel(.25, .28, [0, 255, 0])
    pixel(.91, .89, [255, 255, 0])
    panel.get_by_role("button", name="Archive clip", exact=True).click()
    panel.get_by_role("button", name="Take scene").click()
    expect(panel.locator("#studioSceneStatus")).to_have_text("On stage: Archive clip")
    pixel(.25, .28, [255, 0, 255])
    panel.get_by_role("button", name="Pause clip").click()
    expect(panel.get_by_role("button", name="Play clip")).to_be_visible()
    panel.get_by_role("button", name="Restart clip").click()
    expect(panel.get_by_role("button", name="Pause clip")).to_be_visible()
    return dict(scene_order_saved_after_reopen=True, operator_only_graphic_sources=True, scene_changed_during_live=True,
                lower_third_toggle_decoded=True, logo_remained_across_scenes=True, full_screen_library_image_decoded=True,
                library_video_decoded=True, clip_pause_and_restart=True)


def archive_has_scene_audio(operator, ffmpeg):
    with operator.expect_download() as downloaded:
        operator.get_by_role("button", name="Download recording").click()
    raw = subprocess.run([str(ffmpeg), "-v", "error", "-i", str(downloaded.value.path()), "-map", "0:a:0",
                          "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"], check=True, capture_output=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    samples = array("h", raw)
    size = 2000
    angle = math.tau * 1232 / 8000
    sine = [math.sin(index * angle) for index in range(size)]
    cosine = [math.cos(index * angle) for index in range(size)]
    for start in range(0, len(samples) - size, size):
        part = samples[start:start + size]
        energy = sum(sample * sample for sample in part)
        if energy < size * 200 ** 2:
            continue
        components = (sum(sample * value for sample, value in zip(part, basis)) for basis in (sine, cosine))
        if sum(value ** 2 for value in components) / (size * energy) > .18:
            return True
    return False


def check_browser_delivery(base, keys, graphics=None, diagnostics=False):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    end = (now + timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M")
    event = dict(title="Installed browser broadcast", slug="installed-browser-broadcast", visibility="public",
                 type="live", latency="standard", save_replay=False,
                 schedule=dict(timezone="UTC", start=start, end=end, repeat="none"))
    auth = {"Authorization": "Bearer " + keys["operator"]}
    response = requests.post(base + "/api/live-events", json=event, headers=auth, timeout=10)
    response.raise_for_status()
    entry = response.json()
    (slot,) = entry["occurrences"]
    studio = f"{base}/api/live-events/{entry['id']}/{slot['id']}/browser"
    assert requests.post(studio, timeout=10).status_code == 403
    health = studio.removesuffix("browser") + "health"
    watch = base + entry["watch"] + f"?occurrence={slot['id']}"
    with sync_playwright() as pw:
        browser = chromium(pw, ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        try:
            operator_context = browser.new_context(permissions=["camera", "microphone"])
            operator = operator_context.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.goto(base + "/operator/live-events")
            operator.get_by_role("button", name="Open production for Installed browser broadcast", exact=False).click()
            if diagnostics:
                operator.get_by_role("button", name="Edit event").click()
                operator.get_by_label("Live DVR window in seconds").fill("120")
                operator.get_by_role("button", name="Save event").click()
                expect(operator.locator("#newEventForm")).to_be_hidden()
                operator.get_by_role("button", name="Open production for Installed browser broadcast", exact=False).click()
            operator.get_by_role("link", name="Open browser studio").click()
            expect(operator.get_by_role("heading", name="Live Studio")).to_be_visible()
            panel = prepare_live_scenes(operator, base, studio, auth, *graphics) if graphics else None
            mode = operator.get_by_role("combobox", name="Production mode")
            mode.select_option("record")
            expect(operator.get_by_role("button", name="Start recording")).to_be_visible()
            mode.select_option("broadcast")
            operator.get_by_role("button", name="Connect camera and microphone").click()
            expect(operator.locator("#recordStatus")).to_have_text("Recording devices connected.")
            guest = browser.new_page()
            guest.goto(watch)
            expect(guest.locator("#eventStatus")).to_have_text("Scheduled")
            with operator.expect_response(lambda result: result.url == studio and result.request.method == "POST",
                                          timeout=45000) as opened:
                operator.get_by_role("button", name="Go live").click()
            assert opened.value.status == 201, f"Live Studio returned HTTP {opened.value.status}"
            expect(operator.locator("#recordStatus")).to_contain_text("Sending the live broadcast")
            expect(guest.locator("#eventStatus")).to_have_text("Live", timeout=65000)
            _, width, _ = play_event(guest, watch, "Live", navigate=False)
            assert width >= 320
            scene_result = check_broadcast_graphics(guest, panel) if panel else {}
            if diagnostics:
                scene_result.update(check_live_diagnostics(operator, guest, health, auth))
            operator.get_by_role("button", name="Stop", exact=True).click()
            expect(operator.locator("#recordStatus")).to_contain_text("Recording ready.", timeout=20000)
            expect(operator.get_by_label("Finished recording")).to_be_visible()
            if graphics:
                assert archive_has_scene_audio(operator, graphics[1]), "The downloaded recording lacks the injected clip tone"
                scene_result["recording_contains_scene_video_audio"] = True
            operator.get_by_label("Recording title").fill("Browser live program archive")
            operator.get_by_role("button", name="Save recording", exact=True).click()
            expect(operator.locator("#recordStatus")).to_have_text("Recording saved to the library.", timeout=40000)
            archive = operator.get_by_role("link", name="Open saved video").get_attribute("href").rsplit("/", 1)[-1]
            metadata = requests.get(f"{base}/api/library/work/{archive}/details", headers=auth, timeout=10)
            metadata.raise_for_status()
            assert metadata.json()["media"]["audio"]
            assert requests.get(f"{base}/watch/work/{archive}", timeout=10).status_code == 404
            return dict(browser_camera_and_mic=True, studio_ingest_requires_operator=True, guest_live_width=width,
                        waiting_room_transitions_to_live=True, saved_private_local_archive=True, local_archive_has_audio=True,
                        **scene_result)
        finally:
            browser.close()


def check_live_diagnostics(operator, guest, health, auth):
    import re

    expect(operator.locator("#studioRelayHealth")).to_have_text(re.compile(r"Browser relay.*[1-9].*fps.*[1-9]\d* kbps"), timeout=20000)
    response = requests.get(health, headers=auth, timeout=10)
    response.raise_for_status()
    observed = response.json()
    assert observed["state"] == "live" and observed["delivery_settings"] == dict(latency="standard", dvr_seconds=120)
    values = observed["encoder"]["metrics"]
    assert values["fps"] > 0 and values["bitrate"] > 0 and values["drop_frames"] >= 0
    assert requests.get(health, timeout=10).status_code == 403
    assert requests.get(health + "?source=relay", timeout=10).status_code == 403
    video = guest.locator("#eventPlayer video")
    rewind = video.evaluate("""async v => {
        const until = Date.now() + 45000;
        while (Date.now() < until) {
            const bufferStart = v.seekable.length ? v.seekable.start(0) : v.currentTime;
            if (v.currentTime - bufferStart > 12) break;
            await new Promise(resolve => setTimeout(resolve, 500));
        }
        const from = v.currentTime;
        if (from - v.seekable.start(0) <= 12) throw Error('Live DVR has no past playable buffer');
        v.pause();
        await new Promise((resolve, reject) => {
            v.addEventListener('seeked', resolve, {once: true});
            setTimeout(() => reject(Error('Live DVR rewind did not decode')), 12000);
            v.currentTime = from - 10;
        });
        if (!v.videoWidth || v.readyState < 2) throw Error('No decoded rewind frame');
        return from - v.currentTime;
    }""")
    assert rewind >= 9.5
    return dict(measured_encoder_fps=values["fps"], measured_encoder_kbps=values["bitrate"],
                peer_dvr_window_seconds=120, live_rewind_decoded_seconds=round(rewind, 2),
                diagnostics_require_operator=True)


def check_srt_delivery(base, keys, ffmpeg):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    end = (now + timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M")
    event = dict(title="Installed SRT broadcast", slug="installed-srt-broadcast", visibility="public",
                 type="live", latency="standard", save_replay=False,
                 schedule=dict(timezone="UTC", start=start, end=end, repeat="none"))
    auth = {"Authorization": "Bearer " + keys["operator"]}
    response = requests.post(base + "/api/live-events", json=event, headers=auth, timeout=10)
    response.raise_for_status()
    entry = response.json()
    (slot,) = entry["occurrences"]
    production = f"{base}/api/live-events/{entry['id']}/{slot['id']}"
    endpoint = production + "/srt"
    watch = base + entry["watch"] + f"?occurrence={slot['id']}"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as bind:
        bind.bind(("127.0.0.1", 0))
        port = bind.getsockname()[1]
    details = {"interface": "127.0.0.1", "port": port}
    assert requests.post(endpoint, json=details, timeout=10).status_code == 403
    assert requests.post(endpoint, json=details | {"interface": "0.0.0.0"}, headers=auth, timeout=10).status_code == 422
    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.goto(base + "/operator/live-events")
            operator.get_by_role("button", name="Open production for Installed SRT broadcast", exact=False).click()
            operator.get_by_text("SRT encoder", exact=True).click()
            operator.get_by_label("UDP port").fill(str(port))
            with operator.expect_response(lambda value: value.url == endpoint, timeout=30000) as prepared:
                operator.get_by_role("button", name="Start SRT listener").click()
            assert prepared.value.ok, f"SRT listener returned HTTP {prepared.value.status}"
            expect(operator.locator("#srtSettings")).to_be_visible()
            link = operator.get_by_label("SRT endpoint").input_value()
            secret = operator.get_by_label("SRT passphrase").input_value()
            assert link == f"srt://127.0.0.1:{port}" and len(secret) >= 20
            repeated = requests.post(endpoint, json=details, headers=auth, timeout=12)
            assert repeated.ok and repeated.json()["passphrase"] == secret
            invalid = link + "?mode=caller&conntimeo=1000"
            with send_signal(ffmpeg, invalid, None, 2, "mpegts") as rejected:
                assert rejected.wait(timeout=8) != 0, "Unencrypted sender reached the protected SRT listener"
            caller = link + "?" + urlencode(dict(mode="caller", pbkeylen=16, passphrase=secret, pkt_size=1316))
            guest = browser.new_page()
            guest.goto(watch)
            assert secret not in guest.content()
            with send_signal(ffmpeg, caller, None, 60, "mpegts") as sender:
                wait_for_delivery(production, auth, "live", 45, sender)
                _, width, _ = play_event(guest, watch, "Live")
                assert width >= 240 and sender.poll() is None
                operator.get_by_role("button", name="Stop SRT listener").click()
                expect(operator.locator("#eventsStatus")).to_have_text("SRT listener stopped.")
                expect(operator.locator("#srtSettings")).not_to_be_visible()
            return dict(srt_listener_operator_only=True, supplied_endpoint_works=True, encrypted_sender_played_width=width,
                        unencrypted_sender_rejected=True, retry_preserved_passphrase=True, listener_stopped=True)
        finally:
            browser.close()


def check_event_chat(page, browser, base, auth):
    now = datetime.now(timezone.utc)
    as_field = lambda moment: moment.strftime("%Y-%m-%dT%H:%M")
    event = dict(title="Arts audience chat", slug="arts-audience-chat", visibility="public", type="live",
                 latency="standard", save_replay=False, schedule=dict(timezone="UTC", repeat="none",
                 start=as_field(now - timedelta(minutes=1)), end=as_field(now + timedelta(minutes=12))))
    created = requests.post(base + "/api/live-events", headers=auth, json=event, timeout=15)
    created.raise_for_status()
    entry = created.json()
    (slot,) = entry["occurrences"]
    operator_url = f"{base}/api/live-events/{entry['id']}/{slot['id']}/chat"
    guest_url = f"{base}/events/{entry['slug']}/{slot['id']}/chat"
    watch = f"{base}{entry['watch']}?occurrence={slot['id']}"
    assert requests.get(operator_url, timeout=10).status_code == 403
    initial = requests.get(guest_url, timeout=10)
    assert initial.ok and not initial.json()["enabled"]
    page.goto(base + "/operator/live-events")
    page.get_by_role("button", name="Open production for Arts audience chat", exact=False).click()
    page.get_by_role("button", name="Registration", exact=True).click()
    registration = page.locator("#registrationEditor")
    registration.get_by_label("Require registration before playback").check()
    registration.get_by_label("Form heading").fill("Register for the screening")
    registration.get_by_role("button", name="Save registration").click()
    expect(page.locator("#registrationStatus")).to_have_text("Registration saved.")
    assert requests.get(guest_url, timeout=10).status_code == 404
    page.get_by_text("Audience chat", exact=True).click()
    panel = page.locator("#operatorChat")
    panel.get_by_label("Enable audience chat").check()
    panel.get_by_label("Seconds between messages").fill("30")
    panel.get_by_role("button", name="Save chat settings").click()
    expect(page.locator("#operatorChatStatus")).to_have_text("Chat settings saved.")
    author, reader = browser.new_page(), browser.new_page()
    try:
        for guest, email in ((author, "filmmaker"), (reader, "visitor")):
            guest.goto(watch)
            expect(guest.locator("#audienceChat")).to_have_count(0)
            form = guest.get_by_role("form", name="Register for the screening")
            form.get_by_label("Email").fill(email + "@example.invalid")
            form.get_by_role("button", name="Register and watch").click()
            expect(guest.locator("#audienceChat")).to_be_visible()

        message = "Welcome to the screening. https://example.invalid/program"
        composer = author.locator("#sendChatMessage")
        composer.get_by_label("Display name").fill("Filmmaker")
        composer.get_by_label("Message", exact=True).fill(message)
        composer.get_by_role("button", name="Send message").click()
        published = reader.locator("#chatMessages").get_by_text("Welcome to the screening.", exact=False)
        expect(published).to_be_visible(timeout=10000)
        expect(reader.locator("#chatMessages").get_by_role("link")).to_have_attribute("href", "https://example.invalid/program")
        expect(reader.locator("#chatMessages").get_by_role("button")).to_have_count(0)
        composer.get_by_label("Message", exact=True).fill("Too soon")
        composer.get_by_role("button", name="Send message").click()
        expect(author.locator("#chatStatus")).to_have_text("Wait before sending another chat message.")
        assert requests.get(operator_url + "/export", timeout=10).status_code == 403
        report = requests.get(operator_url + "/export", headers=auth, timeout=10)
        assert report.ok and message in report.text
        panel.get_by_role("button", name="Remove and block viewer (Filmmaker)").click()
        expect(published).to_have_count(0, timeout=10000)
        expect(author.locator("#chatStatus")).to_have_text("You cannot send messages in this event.", timeout=10000)
        expect(composer).not_to_be_visible()
        panel.get_by_label("Enable audience chat").uncheck()
        panel.get_by_role("button", name="Save chat settings").click()
        expect(reader.locator("#audienceChat")).not_to_be_visible(timeout=10000)
        return dict(disabled_before_operator_enables=True, unregistered_access_denied=True,
                    posts_automatically_visible=True, configurable_chat_interval_enforced=True,
                    operator_can_remove_and_block=True, csv_operator_only=True, disable_hides_open_panel=True)
    finally:
        author.context.close()
        reader.context.close()


def check_event_engagement(page, browser, base, auth):
    now = datetime.now(timezone.utc)
    as_field = lambda moment: moment.strftime("%Y-%m-%dT%H:%M")
    event = dict(title="Audience questions and quiz", slug="audience-questions-quiz", visibility="public", type="webinar",
                 latency="standard", save_replay=False, schedule=dict(timezone="UTC", repeat="none",
                 start=as_field(now - timedelta(minutes=1)), end=as_field(now + timedelta(minutes=15))))
    created = requests.post(base + "/api/live-events", headers=auth, json=event, timeout=15)
    created.raise_for_status()
    entry = created.json()
    (slot,) = entry["occurrences"]
    producer_url = f"{base}/api/live-events/{entry['id']}/{slot['id']}/engagement"
    audience_url = f"{base}/events/{entry['slug']}/{slot['id']}/engagement"
    watch = f"{base}{entry['watch']}?occurrence={slot['id']}"
    assert requests.get(producer_url, timeout=10).status_code == 403
    assert requests.post(audience_url, json={"feature": "poll"}, timeout=10).status_code == 403
    page.goto(base + "/operator/live-events")
    page.get_by_role("button", name="Open production for Audience questions and quiz", exact=False).click()
    page.get_by_text("Q&A and polls", exact=True).click()
    producer = page.locator("#operatorEngagement")
    policy = producer.locator("#eventQASettings")
    enabled = policy.get_by_label("Enable audience Q&A")
    review = policy.get_by_label("Approve questions before publishing")

    def save_policy():
        with page.expect_response(lambda response: response.url == producer_url and response.request.method == "PUT") as saved:
            policy.get_by_role("button", name="Save Q&A settings").click()
        assert saved.value.ok

    expect(review).not_to_be_checked()
    enabled.check()
    save_policy()
    author = browser.new_page()
    viewer = browser.new_page()
    try:
        author.goto(watch)
        viewer.goto(watch)
        form = author.locator("#askQuestion")
        form.get_by_label("Display name").fill("Acadia")
        form.get_by_label("Question", exact=True).fill("How was the film restored?")
        form.get_by_role("button", name="Submit question").click()
        visible = viewer.locator("#viewerQuestions li").filter(has_text="How was the film restored?")
        expect(visible).to_be_visible(timeout=10000)
        visible.get_by_role("button", name="Upvote question").click()
        expect(visible.get_by_text("1 votes", exact=False)).to_be_visible()
        primary = producer.locator("#operatorQuestions li").filter(has_text="How was the film restored?")
        primary.get_by_role("button", name="Pin question").click()
        expect(visible.get_by_text("Pinned", exact=False)).to_be_visible(timeout=10000)
        primary.get_by_role("button", name="Reply").click()
        answer = producer.locator("#eventAnswerQuestion")
        answer.get_by_label("Reply", exact=True).fill("From the preserved camera negative.")
        answer.get_by_role("button", name="Mark answered").click()
        expect(visible.get_by_text("From the preserved camera negative.")).to_be_visible(timeout=10000)

        review.check()
        policy.get_by_label("Allow anonymous questions").check()
        save_policy()
        form.get_by_label("Ask anonymously").check()
        form.get_by_label("Question", exact=True).fill("Will the captions be posted?")
        form.get_by_role("button", name="Submit question").click()
        pending = author.locator("#viewerQuestions li").filter(has_text="Will the captions be posted?")
        expect(pending.get_by_text("Awaiting approval", exact=False)).to_be_visible()
        second = viewer.locator("#viewerQuestions li").filter(has_text="Will the captions be posted?")
        expect(second).to_have_count(0)
        review.uncheck()
        save_policy()
        expect(second.get_by_text("Anonymous", exact=False)).to_be_visible(timeout=10000)
        operator_second = producer.locator("#operatorQuestions li").filter(has_text="Will the captions be posted?")
        operator_second.get_by_role("button", name="Archive question").click()
        expect(second).to_have_count(0, timeout=10000)
        operator_second.get_by_role("button", name="Restore question").click()
        expect(second).to_be_visible(timeout=10000)

        poll = producer.locator("#eventPollEditor")

        def stage(question, correct=""):
            poll.get_by_label("Poll question").fill(question)
            poll.get_by_label("Option 1", exact=True).fill("16 mm")
            poll.get_by_label("Option 2", exact=True).fill("35 mm")
            poll.get_by_label("Correct answer").select_option(correct)
            with page.expect_response(lambda response: response.url == producer_url and response.request.method == "POST") as saved:
                poll.get_by_role("button", name="Save poll draft").click()
            assert saved.value.ok

        stage("What was the camera format?", "1")
        stage("Which format should we program next?")
        assert not requests.get(audience_url, timeout=10).json()["polls"]
        quiz = producer.locator("#operatorPolls > li").filter(has_text="What was the camera format?")
        quiz.get_by_role("button", name="Open poll").click()
        guest_poll = viewer.locator("#viewerPoll")
        expect(guest_poll).to_be_visible(timeout=10000)
        assert requests.get(audience_url, timeout=10).json()["polls"][0]["correct"] is None
        assert requests.get(audience_url, timeout=10).json()["polls"][0]["counts"] is None
        other = producer.locator("#operatorPolls > li").filter(has_text="Which format should we program next?")
        other.get_by_role("button", name="Open poll").click()
        expect(producer.locator("#operatorEngagementStatus")).to_have_text("Publish the current poll's results before opening another.")
        guest_poll.get_by_role("button", name="35 mm").click()
        expect(guest_poll.get_by_role("button", name="35 mm (your vote)")).to_be_disabled()
        quiz.get_by_role("button", name="Publish results").click()
        expect(guest_poll.get_by_text("35 mm · 1 votes (100%) · Correct answer")).to_be_visible(timeout=10000)
        quiz.get_by_role("button", name="Archive poll").click()
        expect(guest_poll).not_to_be_visible(timeout=10000)
        exports = [requests.get(f"{producer_url}/{feature}/export", headers=auth, timeout=10) for feature in ("qa", "poll")]
        assert all(response.ok for response in exports)
        assert "From the preserved camera negative." in exports[0].text and "35 mm,1,Yes,archived" in exports[1].text
        assert requests.get(f"{producer_url}/poll/export", timeout=10).status_code == 403
        return dict(questions_automatic_by_default=True, optional_review_and_release=True, anonymous_questions=True,
                    reply_pin_vote_archive_and_restore=True, draft_poll_private=True, open_poll_exclusive=True,
                    results_and_correct_answer_after_publish=True, one_vote_per_session=True, authenticated_exports=True)
    finally:
        author.context.close()
        viewer.context.close()


def check_program_delivery(base, keys, ffmpeg, scratch):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    portal = base + "/api/live-events"
    privacy = {"visibility": "public", "embed": "anywhere", "domains": [], "download": False}

    def upload(name, color, audio, visible=True):
        path = scratch / (name + ".mp4")
        sound = ["-f", "lavfi", "-i", "sine=frequency=262:duration=32", "-c:a", "aac"] if audio else []
        subprocess.run([str(ffmpeg), "-v", "error", "-f", "lavfi", "-i", f"color={color}:s=320x180:r=24:d=32",
                        *sound, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True,
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with path.open("rb") as file:
            params = {"upload_id": str(uuid.uuid4())}
            if visible:
                params["privacy"] = json.dumps(privacy)
            result = requests.post(base + "/api/library/uploads", headers=auth, timeout=30, data=params,
                                   files={"file": (path.name, file, "video/mp4")})
        result.raise_for_status()
        return result.json()["id"]

    upload("Opening red", "red", True)
    upload("Closing green", "lime", False)
    hidden = upload("Private fixture", "blue", False, False)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now + timedelta(minutes=2)
    end = start + timedelta(minutes=10)
    event = dict(title="Scheduled film program", slug="scheduled-film-program", visibility="public",
                 type="live", latency="low", save_replay=False,
                 schedule=dict(timezone="UTC", start=start.strftime("%Y-%m-%dT%H:%M"),
                               end=end.strftime("%Y-%m-%dT%H:%M"), repeat="none"))
    created = requests.post(portal, headers=auth, json=event, timeout=10)
    created.raise_for_status()
    entry = created.json()
    (slot,) = entry["occurrences"]
    production = f"{portal}/{entry['id']}/{slot['id']}"
    watch = f"{base}{entry['watch']}?occurrence={slot['id']}"
    resource = production + "/program"

    def status():
        response = requests.get(resource, headers=auth, timeout=10)
        response.raise_for_status()
        return response.json()

    assert requests.get(resource, timeout=10).status_code == 403
    assert hidden not in {item["work"] for item in status()["choices"]}
    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.goto(base + "/operator/live-events")
            operator.get_by_role("button", name="Open production for Scheduled film program", exact=False).click()
            operator.get_by_text("Prerecorded event", exact=True).click()
            picker = operator.get_by_label("Library video")
            expect(picker).to_be_enabled()
            for title in ("Closing green", "Opening red"):
                picker.select_option(label=title)
                operator.get_by_role("button", name="Add video").click()
            operator.get_by_role("button", name="Move up Opening red").click()
            with operator.expect_response(lambda response: response.url == resource and response.request.method == "PUT") as saved:
                operator.get_by_role("button", name="Schedule playback").click()
            assert saved.value.ok, f"Scheduled program returned HTTP {saved.value.status}"
            assigned = saved.value.json()
            assert len(assigned["sources"]) == 2
            assert requests.post(production + "/encoder", headers=auth, timeout=10).status_code == 409
            assert requests.put(resource, headers=auth, json={"revision": None, "sources": []}, timeout=10).status_code == 422
            operator.close()
            page = browser.new_page()
            page.goto(watch)
            expect(page.locator("#eventStatus")).not_to_have_text("Live")
            timeout = max(1000, round((start.timestamp() - time.time() + 45) * 1000))
            expect(page.locator("#eventStatus")).to_have_text("Live", timeout=timeout)
            _, width, _ = play_event(page, watch, "Live", navigate=False)
            video = page.locator("#eventPlayer video")
            colors = video.evaluate("""async video => {
                const canvas = document.createElement('canvas');
                canvas.width = canvas.height = 1;
                const context = canvas.getContext('2d', {willReadFrequently: true});
                const observed = [];
                const until = performance.now() + 55000;
                while (observed.length < 2 && performance.now() < until) {
                    context.drawImage(video, video.videoWidth / 2, video.videoHeight / 2, 1, 1, 0, 0, 1, 1);
                    const [red, green] = context.getImageData(0, 0, 1, 1).data;
                    const color = red > 140 && green < 80 ? 'red' : green > 140 && red < 80 ? 'green' : null;
                    if (color && color !== observed.at(-1)) observed.push(color);
                    await new Promise(resolve => setTimeout(resolve, 250));
                }
                return observed;
            }""")
            assert colors == ["red", "green"], f"Guest watched the wrong program order {colors}"
            finish = time.monotonic() + 45
            state = status()["state"]
            while state == "broadcasting" and time.monotonic() < finish:
                time.sleep(1)
                state = status()["state"]
            assert state == "finished", f"Expected the program to finish automatically, saw {state}"
            return dict(opening_then_closing=colors, guest_playback_width=width, no_operator_at_airtime=True,
                        anonymous_edit_denied=True, private_source_excluded=True, manual_encoder_denied=True,
                        automatically_finished=True)
        finally:
            browser.close()
