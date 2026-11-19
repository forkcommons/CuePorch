"""Play and advance through real recurring broadcasts from the installed player."""

from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
from threading import Thread
import time
from urllib.parse import urljoin

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_embed_builder import PublisherPage
from installed_events import play_event, send_signal, wait_for_delivery


def check_event_playlist(base, keys, ffmpeg):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    portal = "/api/live-events"

    def api(method, path, body=None):
        response = requests.request(method, base + path, headers=auth, json=body, timeout=30)
        response.raise_for_status()
        return response.json() if response.content else None

    now = datetime.now(timezone.utc)
    title = "Recurring broadcast playlist rehearsal"
    entry = api("POST", portal, dict(
        title=title, slug="recurring-playlist", type="webinar", latency="standard", visibility="public",
        save_replay=True, archive_replay=False, schedule=dict(timezone="UTC", repeat="days", interval=1, count=2,
            start=(now - timedelta(days=1, minutes=1)).strftime("%Y-%m-%dT%H:%M"),
            end=(now + timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M")),
    ))
    earlier, later = [item["id"] for item in entry["occurrences"]]
    producer = f"{portal}/{entry['id']}"
    path = lambda opens: f"{producer}/{opens}"
    gate = lambda opens: f"/api/actions/live-replay-{entry['id']}-{opens}"
    first_watch = base + entry["watch"] + f"?occurrence={earlier}"
    last_watch = base + entry["watch"] + f"?occurrence={later}"
    embed = base + entry["embed"] + f"?occurrence={earlier}"
    registration = api("GET", producer + "/registration")
    registration["settings"].update(enabled=True, heading="Register for the recurring series")
    api("PUT", producer + "/registration", registration)
    api("PUT", "/api/policy/replay", {"required": True})
    assert api("GET", producer + "/playlist") == dict(enabled=False, autoplay_next=False, loop=False)
    assert requests.get(base + producer + "/playlist", timeout=10).status_code == 403
    for opens, size in [(earlier, "320x180"), (later, "640x360")]:
        connection = api("POST", path(opens) + "/encoder")
        with send_signal(ffmpeg, connection["server"], connection["key"], 30, size=size) as encoder:
            wait_for_delivery(base + path(opens), auth, "live", 27, encoder)
            assert encoder.wait(timeout=42) == 0
    print("Two distinct recordings reached PeerTube from the installed event controls.", flush=True)

    deadline = time.monotonic() + 425
    while time.monotonic() < deadline:
        stages = [requests.get(base + gate(opens), headers=auth, timeout=10) for opens in (earlier, later)]
        if all(result.ok and result.json()["status"] == "waiting" for result in stages):
            break
        assert all(result.ok or result.status_code == 404 for result in stages)
        time.sleep(2)
    else:
        raise AssertionError("The video host did not finish both recurring broadcasts.")
    api("POST", gate(earlier) + "/decision", {"decision": "approve"})

    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.goto(base + "/operator/live-events")
            operator.get_by_role("button", name=f"Open production for {title}", exact=False).first.click()
            operator.get_by_text("Replay playlist", exact=True).click()
            save = operator.get_by_role("button", name="Save replay playlist")
            expect(save).to_be_enabled()
            for label in ("Show past broadcasts in the player", "Automatically play the next replay", "Loop the playlist"):
                operator.get_by_label(label).check()
            save.click()
            expect(operator.locator("#eventPlaylistStatus")).to_have_text("Replay playlist settings saved.")

            def register(page, email):
                form = page.get_by_role("form", name="Register for the recurring series")
                form.get_by_label("Email").fill(email)
                form.get_by_role("button", name="Register and watch").click()
                expect(form).to_have_count(0)

            def options(page, expected):
                select = page.get_by_label("Choose a broadcast")
                expect(select.locator("option:not([disabled])")).to_have_count(len(expected), timeout=23000)
                actual = select.locator("option:not([disabled])").evaluate_all("(items) => items.map(i => i.value)")
                assert actual == [str(opens) for opens in expected], actual

            audience = browser.new_page()
            audience.goto(first_watch)
            expect(audience.get_by_role("navigation", name="Past broadcasts")).to_be_hidden()
            headers = {"Accept": "application/json"}
            before = requests.get(first_watch, headers=headers, timeout=10).json()
            assert before["playlist"] is None and before["address"] is None
            register(audience, "series-viewer@example.invalid")
            expect(audience.locator("#eventStatus")).to_have_text("Replay", timeout=23000)
            grant = audience.locator("#eventLobby").get_attribute("data-viewer-grant")
            assert grant
            registered = headers | {"X-CuePorch-Event-Viewer": grant}
            released = requests.get(first_watch, headers=registered, timeout=10).json()
            assert released["address"] and isinstance(released["playlist"], dict)
            assert [item["id"] for item in released["playlist"]["entries"]] == [earlier]
            options(audience, [earlier])
            held = requests.get(last_watch, headers=registered, timeout=10).json()
            assert held["address"] is None and held["playlist"] is None
            assert requests.get(embed, headers=registered, timeout=10).json()["address"] is None

            api("POST", gate(later) + "/decision", {"decision": "approve"})
            options(audience, [earlier, later])
            first_manifest, first_width, first_duration = play_event(audience, first_watch, "Replay", navigate=False)
            assert first_width == 320 and first_duration >= 20
            video = audience.locator("#eventPlayer video")

            def finish_current():
                video.evaluate("(v) => {v.currentTime = v.duration - 0.4; if (v.paused) void v.play();}")

            def expect_autoplay(opens, minimum, maximum):
                audience.wait_for_function("""([opens, min, max]) => {
                  const v = document.querySelector('#eventPlayer video');
                  return new URL(location.href).searchParams.get('occurrence') === String(opens) &&
                    v && v.videoWidth >= min && v.videoWidth <= max && !v.paused && v.currentTime > .1;
                }""", arg=[opens, minimum, maximum], timeout=34000)
                assert video.evaluate("v => v.duration") >= 20

            finish_current()
            expect_autoplay(later, 321, 640)
            later_width = video.evaluate("v => v.videoWidth")
            finish_current()
            expect_autoplay(earlier, 320, 320)
            print("The registered watch player decoded both replays and looped back to the first.", flush=True)

            with ThreadingHTTPServer(("127.0.0.1", 0), PublisherPage) as publisher:
                publisher.code = f'<iframe title="Series" src="{embed}"></iframe>'
                thread = Thread(target=publisher.serve_forever, daemon=True)
                thread.start()
                try:
                    outside = browser.new_page()
                    outside.goto(f"http://127.0.0.1:{publisher.server_port}/")
                    embedded = outside.get_by_title("Series").element_handle().content_frame()
                    assert embedded
                    expect(embedded.locator("#eventStatus")).to_have_text("Registration required")
                    register(embedded, "embedded-viewer@example.invalid")
                    options(embedded, [earlier, later])
                    embedded.get_by_role("button", name="Next replay").click()
                    expect(embedded.locator("#eventStatus")).to_have_text("Replay", timeout=23000)
                    iframe_manifest, width, _ = play_event(embedded, embed, "Replay", navigate=False)
                    assert width > 320 and urljoin(base, iframe_manifest) != first_manifest

                    api("DELETE", path(later) + "/delivery")
                    assert requests.get(iframe_manifest, timeout=10).status_code == 404
                    expect(embedded.get_by_label("Event video")).to_have_count(0, timeout=16000)
                    options(embedded, [earlier])
                finally:
                    publisher.shutdown()
                    thread.join()
            options(audience, [earlier])
            operator.get_by_label("Show past broadcasts in the player").uncheck()
            save.click()
            expect(audience.get_by_role("navigation", name="Past broadcasts")).to_be_hidden(timeout=16000)
            assert requests.get(first_manifest, timeout=10).ok
            return dict(verified_decoded_widths=[first_width, later_width], iframe_width=width,
                        opt_in_playlist=True, approval_excludes_held_recording=True,
                        registered_once_per_player=True, registration_isolated_between_watch_and_iframe=True,
                        next_replay_played_automatically=True, loop_played_first_replay_again=True,
                        withdrawal_removed_list_entry_and_media=True, disabling_kept_base_playback=True)
        finally:
            browser.close()
