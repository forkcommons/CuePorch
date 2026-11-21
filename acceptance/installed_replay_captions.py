"""Verify local captions on a real replay from the installed production service."""

from datetime import datetime, timedelta, timezone
import json
import re
import time

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_events import play_event, wait_for_delivery
from installed_live_captions import set_caption_policy, spoken_signal


def check_replay_captions(base, keys, ffmpeg, speech, governed=False):
    now = datetime.now(timezone.utc)
    auth = {"Authorization": "Bearer " + keys["operator"]}
    title = "Captioned replay commissioning"
    policy = base + "/api/policy"
    response = requests.get(policy, headers=auth, timeout=10)
    response.raise_for_status()
    assert not any(response.json()[step] for step in ("replay", "caption"))
    if governed:
        set_caption_policy(base, auth, True)
    for step in ("replay", "caption"):
        requests.put(f"{policy}/{step}", headers=auth, json={"required": True}, timeout=10).raise_for_status()
    event = dict(title=title, slug="captioned-replay", visibility="public", type="live",
                 latency="standard", save_replay=True,
                 schedule=dict(timezone="UTC", repeat="none",
                               start=(now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                               end=(now + timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M")))
    response = requests.post(base + "/api/live-events", headers=auth, json=event, timeout=10)
    response.raise_for_status()
    saved = response.json()
    (slot,) = saved["occurrences"]
    prefix = f"{base}/api/live-events/{saved['id']}"
    production = f"{prefix}/{slot['id']}"
    status_url = production + "/replay-captions"
    watch = base + saved["watch"] + f"?occurrence={slot['id']}"
    track = f"{base}/events/{saved['slug']}/{slot['id']}/replay-captions"
    reg = requests.get(prefix + "/registration", headers=auth, timeout=10).json()
    reg["settings"].update(enabled=True, heading="Register for the finished program")
    requests.put(prefix + "/registration", headers=auth, json=reg, timeout=10).raise_for_status()
    enabled = {"enabled": True, "language": "en"}
    requests.put(production + "/captions", headers=auth, json=enabled, timeout=10).raise_for_status()
    setup = requests.post(production + "/encoder", headers=auth, timeout=30)
    setup.raise_for_status()
    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.goto(base + "/operator/live-events")
            operator.get_by_role("button", name=f"Open production for {title}", exact=False).click()
            operator.get_by_text("Replay captions", exact=True).click()
            audience = browser.new_page()
            audience.goto(watch)
            form = audience.get_by_role("form", name="Register for the finished program")
            form.get_by_label("Email").fill("replay-viewer@example.invalid")
            form.get_by_role("button", name="Register and watch").click()
            expect(form).to_have_count(0)
            grant = audience.locator("#eventLobby").get_attribute("data-viewer-grant")
            assert grant
            viewer = {"X-CuePorch-Event-Viewer": grant}
            with spoken_signal(ffmpeg, setup.json(), speech, 42) as encoder:
                wait_for_delivery(production, auth, "live", 45, encoder)
                expect(audience.locator("#eventStatus")).to_have_text("Live", timeout=25000)
                assert encoder.wait(timeout=55) == 0, "The speech fixture did not reach the event host."
            print("Finished sending the speech fixture. Waiting for the host to finalize the recorded media.", flush=True)
            deadline = time.monotonic() + 460
            while time.monotonic() < deadline:
                status = requests.get(status_url, headers=auth, timeout=10)
                status.raise_for_status()
                details = status.json()
                if details["has_track"]:
                    break
                assert details["state"] not in ("failed", "no_speech"), details
                time.sleep(2)
            else:
                raise AssertionError(f"Replay captions were not generated: {details}")
            assert not details["published"]
            assert requests.get(status_url + "?download=1", timeout=10).status_code == 403
            assert requests.get(track, headers=viewer, timeout=10).status_code == 404
            operator.get_by_role("button", name="Refresh replay captions").click()
            expect(operator.locator("#replayCaptionStatus")).to_have_text("Replay captions are awaiting approval.")
            with operator.expect_download() as download:
                operator.get_by_role("button", name="Download WebVTT").click()
            exported = download.value.path().read_text(encoding="utf-8")
            assert exported.startswith("WEBVTT") and re.search(r"city\s+council", exported, re.I), exported
            if governed:
                assert "Fixture City Council" in exported, exported
            print("Downloaded TextFlowKit WebVTT while both approval switches held the result.", flush=True)

            requests.put(policy + "/replay", headers=auth, json={"required": False}, timeout=10).raise_for_status()
            expect(audience.locator("#eventStatus")).to_have_text("Replay", timeout=35000)
            manifest, width, duration = play_event(audience, watch, "Replay", navigate=False)
            assert 30 <= duration <= 55
            assert not requests.get(track, headers=viewer, timeout=10).json()["cues"]
            assert requests.get(track, timeout=10).status_code == 404
            video = audience.locator("#eventPlayer video")
            audience.wait_for_function("document.querySelector('#eventPlayer video')?.currentTime >= 1.5")
            video.evaluate("v => v.pause()")
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                analytics = requests.get(production + "/analytics", headers=auth, timeout=10)
                analytics.raise_for_status()
                if analytics.json()["replay"]["watched_seconds"] >= 1:
                    break
                time.sleep(.25)
            else:
                raise AssertionError("The decoded replay did not report watch time.")
            watched = analytics.json()["replay"]["watched_seconds"]

            requests.put(policy + "/caption", headers=auth, json={"required": False}, timeout=10).raise_for_status()
            audience.get_by_role("button", name="Show replay captions").click(timeout=18000)
            audience.get_by_text("Replay transcript", exact=True).click()
            expect(audience.get_by_label("Caption lines")).to_contain_text(re.compile(r"city\s+council", re.I), timeout=15000)
            transcript = requests.get(track, headers=viewer, timeout=10).json()["cues"]
            assert any(0 <= cue["starts"] < cue["ends"] < duration * 1000 + 750 for cue in transcript)

            corrected = "Corrected city council replay caption"
            if governed:
                set_caption_policy(base, auth, False)
                assert requests.get(track, headers=viewer, timeout=10).json()["cues"] == transcript
            operator.get_by_label("Subtitle file").set_input_files({
                "name": "correction.vtt", "mimeType": "text/vtt",
                "buffer": f"WEBVTT\n\n00:00:02.000 --> 00:00:04.000\n{corrected}\n".encode("utf-8"),
            })
            operator.get_by_role("button", name="Replace replay captions").click()
            expect(operator.locator("#replayCaptionStatus")).to_have_text("Replay captions are ready.")
            lines = audience.get_by_label("Caption lines").get_by_role("button")
            expect(lines).to_have_text([corrected], timeout=15000)
            lines.first.click()
            audience.wait_for_function("""text => {
                const v = document.querySelector('#eventPlayer video');
                return !v.seeking && v.videoWidth > 0 && Math.abs(v.currentTime - 2) < .2 &&
                    [...v.textTracks].some(t => [...(t.activeCues || [])].some(c => c.text === text));
            }""", arg=corrected, timeout=12000)
            assert requests.delete(production + "/delivery", headers=auth, timeout=30).status_code == 204
            assert requests.get(track, headers=viewer, timeout=10).status_code == 404
            assert requests.get(manifest, timeout=10).status_code == 404
            expect(video).to_have_count(0, timeout=15000)
            return dict(width=width, replay_seconds=round(duration, 2), replay_watched_seconds=watched,
                        offline_textflowkit_vtt_from_actual_recording=True,
                        independent_optional_caption_and_replay_approvals=True,
                        operator_can_download_while_held=True, registration_required=True,
                        correction_loaded_in_open_browser=True, transcript_seek_decodes_cue=True,
                        track_and_media_revoked_together=True,
                        workspace_dictionary_and_manual_replacement=governed)
        finally:
            browser.close()
