"""Verify the waiting view on stable watch and embed links before a timed stream."""

from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from threading import Thread
from zoneinfo import ZoneInfo

from PIL import Image
from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_embed_builder import PublisherPage
from installed_events import play_event, send_signal, wait_for_delivery


def check_event_waiting(base, keys, ffmpeg):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    zone = "America/Denver"
    now = datetime.now(ZoneInfo(zone))
    start = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    end = (now + timedelta(minutes=18)).strftime("%Y-%m-%dT%H:%M")
    title = "Open rehearsal player"
    payload = dict(title=title, slug="rehearsal-player", visibility="public", type="live",
                   latency="standard", save_replay=False, schedule=dict(
                       timezone=zone, start=start, end=end, repeat="days", interval=2, count=2))

    def api(method, path, data=None):
        response = requests.request(method, base + path, headers=auth, timeout=25, json=data)
        response.raise_for_status()
        return response.json() if response.content else None

    event = api("POST", "/api/live-events", payload)
    assert event["publication"] == "eligible"
    first = event["occurrences"][0]["id"]
    api_path = f"/api/live-events/{event['id']}"
    settings_path = api_path + "/player"
    production = f"{api_path}/{first}"
    watch = f"{base}{event['watch']}?occurrence={first}"
    embed = f"{base}{event['embed']}?occurrence={first}"

    raw = BytesIO()
    Image.new("RGB", (320, 180), "#8e467a").save(raw, format="PNG")
    poster_bytes = raw.getvalue()
    asset = dict(name="rehearsal-art.png", mimeType="image/png", buffer=poster_bytes)
    public_json = {"Accept": "application/json"}
    assert requests.get(base + settings_path, timeout=10).status_code == 403
    assert requests.get(watch, headers=public_json, timeout=10).json()["lobby_preview"] is None

    with sync_playwright() as pw, ThreadingHTTPServer(("127.0.0.1", 0), PublisherPage) as publisher:
        publisher.code = f'<iframe title="Station player" src="{embed}" width="800" height="600" allowfullscreen></iframe>'
        thread = Thread(target=publisher.serve_forever, daemon=True)
        thread.start()
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.goto(base + "/operator/live-events")
            operator.get_by_role("button", name=f"Open production for {title}", exact=False).first.click()
            panel = operator.locator("#eventPlayerSettings")
            panel.get_by_text("Event player", exact=True).click()
            save = panel.get_by_role("button", name="Save event player")
            expect(save).to_be_enabled()
            schedule = panel.get_by_label("Show the next broadcast time in the player")
            recurrence = panel.get_by_label("Show the recurring schedule in the player")
            expect(schedule).not_to_be_checked()
            expect(recurrence).not_to_be_checked()
            schedule.check()
            recurrence.check()
            panel.get_by_label("Event poster").set_input_files(asset)

            def view_waiting(page):
                lobby = page.get_by_role("region", name="Before the broadcast")
                expect(lobby).to_be_visible(timeout=15000)
                expect(lobby.locator("time")).to_contain_text(zone)
                expect(lobby.locator("[data-event-repeat]")).to_have_text(f"Every 2 days at {start[11:]} ({zone})")
                image = lobby.get_by_role("img", name="Event artwork")
                expect(image).to_be_visible(timeout=15000)
                assert image.evaluate("async i => { await i.decode(); return i.naturalWidth }") == 320
                return image

            def persist():
                save.click()
                expect(panel.locator("#eventPlayerStatus")).to_have_text("Event player saved.")

            view_waiting(panel)
            persist()
            digest = api("GET", settings_path)["poster"]
            watch_page = browser.new_page()
            watch_page.goto(watch)
            expect(watch_page.locator("#eventStatus")).to_have_text("Scheduled")
            picture = view_waiting(watch_page)
            artwork_url = base + picture.get_attribute("src")
            answer = requests.get(artwork_url, timeout=10)
            assert answer.ok and answer.content == poster_bytes, (answer.status_code, len(answer.content))
            assert "no-store" in answer.headers["Cache-Control"].split(", ")
            outside = browser.new_page()
            outside.goto(f"http://127.0.0.1:{publisher.server_port}/")
            frame = outside.get_by_title("Station player").element_handle().content_frame()
            assert frame
            view_waiting(frame)

            api("PUT", "/api/policy/publication", {"required": True})
            try:
                held = api("POST", "/api/live-events", payload | {"slug": "held-rehearsal", "title": "Held rehearsal"})
                assert held["publication"] == "waiting"
                held_id = held["occurrences"][0]["id"]
                api("PUT", f"/api/live-events/{held['id']}/player", dict(
                    show_schedule=True, show_recurring=False, poster=digest))
                assert requests.get(base + f"/events/held-rehearsal/{held_id}/poster/{digest}", timeout=10).status_code == 404
            finally:
                api("PUT", "/api/policy/publication", {"required": False})

            panel.get_by_role("button", name="Remove poster").click()
            persist()
            assert requests.get(artwork_url, timeout=10).status_code == 404
            expect(frame.locator("[data-event-poster]")).to_be_hidden(timeout=16000)
            expect(frame.get_by_role("region", name="Before the broadcast")).to_be_visible()
            panel.get_by_label("Event poster").set_input_files(asset)
            persist()
            view_waiting(frame)
            print("Both stable event links decoded custom artwork and displayed the local start and recurrence.", flush=True)

            encoder = api("POST", production + "/encoder")
            with send_signal(ffmpeg, encoder["server"], encoder["key"], 55) as process:
                wait_for_delivery(base + production, auth, "live", 38, process)
                expect(watch_page.locator("#eventStatus")).to_have_text("Live", timeout=55000)
                expect(frame.locator("#eventStatus")).to_have_text("Live", timeout=55000)
                expect(watch_page.locator("[data-event-waiting]")).to_be_hidden()
                expect(frame.locator("[data-event-waiting]")).to_be_hidden()
                _, watch_width, _ = play_event(watch_page, watch, "Live", navigate=False)
                _, embed_width, _ = play_event(frame, embed, "Live", navigate=False)
                assert watch_page.url == watch and frame.url == embed
                assert process.wait(timeout=60) == 0
            return dict(operator_preview=True, poster_decoded_at=320, scheduled_local_time=True,
                        recurrence_on_existing_embed=True, saved_poster_revocation=True,
                        publication_approval_blocks_artwork=True, waiting_view_hidden_when_live=True,
                        watch_playback_width=watch_width, embed_playback_width=embed_width, stable_links=True)
        finally:
            browser.close()
            publisher.shutdown()
            thread.join()
