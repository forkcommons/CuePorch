"""Test a released and captioned fallback recording on the installed event player."""

import csv
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
from io import StringIO
from threading import Thread
import time

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_bandwidth import bandwidth
from installed_embed_builder import PublisherPage
from installed_events import play_event, send_signal, wait_for_delivery


def check_waiting_replay(base, keys, ffmpeg, verify_bandwidth=False, verify_streaming=False):
    auth = {"Authorization": "Bearer " + keys["operator"]}

    def api(method, path, data=None):
        response = requests.request(method, base + path, headers=auth, timeout=30, json=data)
        response.raise_for_status()
        return response.json() if response.content else None

    now = datetime.now(timezone.utc)
    title = "Before the next live broadcast"
    event = api("POST", "/api/live-events", dict(
        title=title, slug="recording-before-live", type="webinar", latency="standard", visibility="public",
        save_replay=True, archive_replay=False, schedule=dict(timezone="UTC", repeat="days", interval=1, count=2,
            start=(now - timedelta(days=1, minutes=2)).strftime("%Y-%m-%dT%H:%M"),
            end=(now + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M"))))

    def event_bytes():
        result = bandwidth(base, auth, event["id"], bandwidth_group="video")
        return result["totals"], result["rows"]

    def streaming_report(**filters):
        today = datetime.now(timezone.utc).date().isoformat()
        return api("POST", "/api/analytics/video-report",
                   {"videos": [event["id"]], "from": today, "to": today, "breakdown": "streaming"} | filters)

    def watched(row):
        return row["views"] >= 1 and row["watched_seconds"] > 0

    def wait_report(check):
        deadline = time.monotonic() + 15
        result = streaming_report()
        while not check(result) and time.monotonic() < deadline:
            time.sleep(.2)
            result = streaming_report()
        assert check(result), result
        return result

    if verify_bandwidth:
        assert event_bytes()[0]["total_bytes"] == 0
    older, upcoming = [item["id"] for item in event["occurrences"]]
    operator_path = f"/api/live-events/{event['id']}"
    old_path = f"{operator_path}/{older}"
    new_path = f"{operator_path}/{upcoming}"
    gate = f"/api/actions/live-replay-{event['id']}-{older}"
    watch = f"{base}{event['watch']}?occurrence={upcoming}"
    embed = f"{base}{event['embed']}?occurrence={upcoming}"
    registration = api("GET", operator_path + "/registration")
    registration["settings"].update(enabled=True, heading="Join the broadcast")
    api("PUT", operator_path + "/registration", registration)
    api("PUT", "/api/policy/replay", {"required": True})
    encoder = api("POST", old_path + "/encoder")
    with send_signal(ffmpeg, encoder["server"], encoder["key"], 35) as process:
        wait_for_delivery(base + old_path, auth, "live", 29, process)
        assert process.wait(timeout=45) == 0
    print("The first PeerTube broadcast ended. Waiting for its held replay.", flush=True)
    deadline = time.monotonic() + 425
    while time.monotonic() < deadline:
        approval = requests.get(base + gate, headers=auth, timeout=10)
        if approval.ok and approval.json()["status"] == "waiting":
            break
        assert approval.ok or approval.status_code == 404
        time.sleep(2)
    else:
        raise AssertionError("The recorded stream did not reach its optional Replay approval.")
    caption = "A caption from the previous broadcast"
    track = requests.post(base + old_path + "/replay-captions", headers=auth, timeout=15, data={"revision": "", "language": "en"},
                          files={"track": ("previous.vtt", f"WEBVTT\n\n00:00:00.000 --> 00:00:04.000\n{caption}\n", "text/vtt")})
    track.raise_for_status()
    assert track.json()["published"] and api("GET", operator_path + "/playlist")["enabled"] is False

    with sync_playwright() as pw, ThreadingHTTPServer(("127.0.0.1", 0), PublisherPage) as publisher:
        publisher.code = f'<iframe title="Waiting room" src="{embed}" width="800" height="650" allowfullscreen></iframe>'
        thread = Thread(target=publisher.serve_forever, daemon=True)
        thread.start()
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.goto(base + "/operator/live-events")
            operator.get_by_role("button", name=f"Open production for {title}", exact=False).last.click()
            panel = operator.locator("#eventPlayerSettings")
            panel.get_by_text("Event player", exact=True).click()
            setting = panel.get_by_label("Show the latest released replay before going live")
            save = panel.get_by_role("button", name="Save event player")
            expect(save).to_be_enabled()
            expect(setting).not_to_be_checked()

            def save_latest(enabled):
                setting.set_checked(enabled)
                save.click()
                expect(panel.locator("#eventPlayerStatus")).to_have_text("Event player saved.")

            panel.get_by_label("Show the next broadcast time in the player").check()
            save_latest(True)
            watch_page = browser.new_page()
            watch_page.goto(watch)
            outside = browser.new_page()
            outside.goto(f"http://127.0.0.1:{publisher.server_port}/")
            frame = outside.get_by_title("Waiting room").element_handle().content_frame()
            assert frame

            for page, email in ((watch_page, "watcher@example.invalid"), (frame, "embedder@example.invalid")):
                expect(page.locator("#eventStatus")).to_have_text("Registration required")
                form = page.get_by_role("form", name="Join the broadcast")
                form.get_by_label("Email").fill(email)
                form.get_by_role("button", name="Register and watch").click()
                expect(page.locator("#eventStatus")).to_have_text("Scheduled")
                expect(page.locator("#eventPlayer video")).to_have_count(0)
                assert page.locator("#eventLobby").get_attribute("data-viewer-grant")
            api("POST", gate + "/decision", {"decision": "approve"})
            for page in (watch_page, frame):
                expect(page.locator("#eventStatus")).to_have_text("Replay", timeout=45000)
                expect(page.locator("#eventLobby")).to_have_attribute("data-preview-replay", "yes")
            assert requests.get(watch, headers={"Accept": "application/json"}, timeout=10).json()["address"] is None
            _, old_width, duration = play_event(watch_page, watch, "Replay", navigate=False)
            _, old_embed_width, _ = play_event(frame, embed, "Replay", navigate=False)
            assert (old_width, old_embed_width) == (320, 320) and duration >= 30
            if verify_streaming:
                token = watch_page.locator("#eventLobby").get_attribute("data-viewer-grant")
                opted_out = requests.get(watch + "&dnt=1", timeout=10, headers={
                    "Accept": "application/json", "X-CuePorch-Event-Viewer": token,
                })
                opted_out.raise_for_status()
                assert opted_out.json()["address"] and not opted_out.json()["analytics_token"]
                for page in (watch_page, frame):
                    page.locator("#eventPlayer video").evaluate("""v => new Promise(resolve => {
                        if (v.currentTime >= 1) { v.pause(); resolve(); return; }
                        v.addEventListener('timeupdate', function sample() {
                            if (v.currentTime >= 1) { v.removeEventListener('timeupdate', sample); v.pause(); resolve(); }
                        });
                    })""")
                replay = wait_report(lambda result: result["totals"]["views"] == 2 and watched(result["totals"]))
                assert {row["breakdown"] for row in replay["rows"]} == {"Replay"}
                assert replay["totals"]["average_watched_fraction"] > 0
            if verify_bandwidth:
                replay_bytes, rows = event_bytes()
                assert replay_bytes["live_bytes"] == 0 < replay_bytes["vod_bytes"]
                assert rows == [replay_bytes | {"label": title, "item": event["id"], "video": None}]
            for page in (watch_page, frame):
                page.get_by_role("button", name="Show replay captions").click(timeout=18000)
                page.get_by_text("Replay transcript", exact=True).click()
                expect(page.get_by_label("Caption lines")).to_contain_text(caption, timeout=16000)
            print("The registered watch page and external embed decoded the released replay and its captions.", flush=True)

            save_latest(False)
            for page in (watch_page, frame):
                expect(page.locator("#eventPlayer video")).to_have_count(0, timeout=16000)
                expect(page.get_by_role("region", name="Before the broadcast")).to_be_visible()
            save_latest(True)
            for page in (watch_page, frame):
                expect(page.locator("#eventStatus")).to_have_text("Replay", timeout=16000)

            second = api("POST", new_path + "/encoder")
            with send_signal(ffmpeg, second["server"], second["key"], 55, size="640x360") as process:
                wait_for_delivery(base + new_path, auth, "live", 42, process)
                for page in (watch_page, frame):
                    expect(page.locator("#eventStatus")).to_have_text("Live", timeout=45000)
                    expect(page.locator("#eventLobby")).to_have_attribute("data-preview-replay", "no")
                _, live_width, _ = play_event(watch_page, watch, "Live", navigate=False)
                _, live_embed_width, _ = play_event(frame, embed, "Live", navigate=False)
                assert 320 < live_width <= 640 and 320 < live_embed_width <= 640
                assert watch_page.url == watch and frame.url == embed
                if verify_streaming:
                    grouped = wait_report(lambda result: any(
                        row["breakdown"] == "Live" and row["views"] == 2 and watched(row) for row in result["rows"]))
                assert process.wait(timeout=65) == 0
            evidence = dict(approval_and_registration_gate_replay=True, direct_unauthorized_replay_denied=True,
                            independent_embed_replay=True, recorded_width=old_width, recorded_duration=round(duration, 1),
                            replay_captions_decoded_on_both_surfaces=True, opt_out_restored_schedule=True,
                            live_preempted_replay=True, live_watch_width=live_width, live_embed_width=live_embed_width, stable_links=True)
            if verify_bandwidth:
                delivered, _ = event_bytes()
                assert delivered["live_bytes"] > 0 and delivered["vod_bytes"] >= replay_bytes["vod_bytes"]
                assert delivered["total_bytes"] == delivered["live_bytes"] + delivered["vod_bytes"]
                content = api("GET", "/api/analytics/videos?events=1")
                assert {"id": event["id"], "title": title, "event": True} in content
                evidence["gateway_bandwidth"] = delivered | {"vod_before_live_bytes": replay_bytes["vod_bytes"]}
            if verify_streaming:
                phases = {row["breakdown"]: row for row in grouped["rows"]}
                assert phases["Live"]["average_watched_fraction"] is None
                assert phases["Replay"]["views"] == 2 and phases["Live"]["views"] == 2
                assert grouped["totals"]["unique_viewers"] == 2
                assert streaming_report(streaming="vod")["totals"]["views"] == 0
                assert streaming_report(streaming="live", surface="watch")["totals"]["views"] == 1
                assert streaming_report(streaming="live", surface="embed", source=f"http://127.0.0.1:{publisher.server_port}")["totals"]["views"] == 1
                assert streaming_report(streaming="replay", **{"from": "2000-01-01", "to": "2000-01-01"})["totals"]["views"] == 0
                operator.goto(f"{base}/operator/analytics?video={event['id']}")
                operator.locator('select[name="streaming"]').select_option("live")
                operator.locator('select[name="breakdown"]').select_option("streaming")
                operator.get_by_role("button", name="Apply filters").click()
                expect(operator.locator("#reportRows")).to_contain_text("Live")
                expect(operator.locator("#reportRows")).not_to_contain_text("Replay")
                with operator.expect_download() as downloaded:
                    operator.get_by_role("button", name="Download CSV").click()
                rows = list(csv.DictReader(StringIO(downloaded.value.path().read_text(encoding="utf-8"))))
                assert [(row["video_id"], row["streaming"], row["views"]) for row in rows] == [(event["id"], "Live", "2")]
                evidence["streaming"] = dict(live_views=2, replay_views=2, unique_browsers=2, date_and_surface_filters=True,
                                             external_origin_retained=True, dnt=True, filtered_csv=True)
            return evidence
        finally:
            browser.close()
            publisher.shutdown()
            thread.join()
