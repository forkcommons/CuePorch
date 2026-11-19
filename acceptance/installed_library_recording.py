"""Record a real hosted broadcast, then play its portable private Library copy."""

from datetime import datetime, timedelta, timezone
from hashlib import file_digest
import json
import math
import re
import struct
import subprocess
import time
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_events import play_event, send_signal, wait_for_delivery


def check_library_recording(base, keys, home, ffmpeg):
    auth = {"Authorization": "Bearer " + keys["operator"]}

    def api(method, path, **kwargs):
        response = requests.request(method, base + path, headers=auth, timeout=30, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    policy = "/api/policy/replay"
    assert api("GET", "/api/policy")["replay"] is False
    api("PUT", policy, json={"required": True})
    now = datetime.now(timezone.utc)
    opened = now - timedelta(minutes=1)
    zone = ZoneInfo("Pacific/Kiritimati" if opened.hour >= 10 else "Pacific/Honolulu")
    local_start = opened.astimezone(zone)
    local_end = (now + timedelta(minutes=15)).astimezone(zone)
    assert local_start.date() != opened.date()
    title = "Library recording rehearsal"
    event = dict(title=title, slug="library-recording", visibility="public", type="live", latency="standard",
                 save_replay=True, schedule=dict(timezone=zone.key, repeat="none",
                     start=local_start.strftime("%Y-%m-%dT%H:%M"), end=local_end.strftime("%Y-%m-%dT%H:%M")))
    saved = api("POST", "/api/live-events", json=event)
    assert saved["event"]["archive_replay"] is True
    (slot,) = saved["occurrences"]
    production = f"/api/live-events/{saved['id']}/{slot['id']}"
    recording = production + "/library-recording"
    watch = base + saved["watch"] + f"?occurrence={slot['id']}"
    assert api("GET", recording)["state"] == "unavailable"
    assert requests.get(base + recording, timeout=10).status_code == 403
    assert requests.post(base + recording, timeout=10).status_code == 403
    encoder = api("POST", production + "/encoder")

    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            audience = browser.new_page()
            with send_signal(ffmpeg, encoder["server"], encoder["key"], 42) as signal:
                wait_for_delivery(base + production, auth, "live", 42, signal)
                audience.goto(watch)
                expect(audience.locator("#eventStatus")).to_have_text("Live", timeout=20000)
                live, width, _ = play_event(audience, watch, "Live", navigate=False)
                assert width == 320
                assert signal.wait(timeout=55) == 0
            print("Installed browser played the 320px live fixture. Waiting for the host replay.", flush=True)
            deadline = time.monotonic() + 425
            gate = f"/api/actions/live-replay-{saved['id']}-{slot['id']}"
            while time.monotonic() < deadline:
                result = api("GET", recording)
                assert result["state"] != "failed", result
                if result["state"] == "ready":
                    approval = requests.get(base + gate, headers=auth, timeout=10)
                    if approval.ok and approval.json()["status"] == "waiting":
                        break
                    assert approval.ok or approval.status_code == 404
                time.sleep(2)
            else:
                raise AssertionError(f"The private Library copy or audience approval did not finish: {result}")
            audience.goto(watch)
            expect(audience.locator("#eventPlayer video")).to_have_count(0)
            uid = result["work"]
            assert uid and api("POST", recording) == result
            info = api("GET", f"/api/library/work/{uid}")
            assert info["title"] == f"{title} · {local_start.date().isoformat()}"
            assert info["source_privacy"] == "private" and info["watch"] is None and info["embed"] is None
            assert 33 <= info["duration_seconds"] <= 48, info["duration_seconds"]
            assert requests.get(base + "/watch/work/" + uid, timeout=10).status_code == 404
            (asset,) = info["files"]
            assert asset["role"] == "playback" and asset["format"] == "mp4"
            media = home / "collection" / "objects" / asset["sha256"]
            with media.open("rb") as stored:
                assert file_digest(stored, "sha256").hexdigest() == asset["sha256"]
            print("The Library recording is checksum-verified and private while audience approval is waiting.", flush=True)

            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.goto(base + "/operator/live-events")
            operator.get_by_role("button", name=f"Open production for {title}", exact=False).click()
            operator.get_by_text("Library recording", exact=True).click()
            expect(operator.locator("#libraryRecordingStatus")).to_have_text("Saved privately in the Library.")
            operator.get_by_role("link", name="Open Library recording").click()
            assert operator.url.endswith("/operator/videos/" + uid)
            player = operator.locator("#workspacePreview")
            ticket = re.compile(r"^/library-media/")
            expect(player).to_have_attribute("src", ticket)
            width = player.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}")
            operator.wait_for_function("document.querySelector('#workspacePreview')?.currentTime > .5")
            assert width == 320
            raw = subprocess.check_output([
                str(ffmpeg), "-nostdin", "-v", "error", "-ss", "2", "-i", str(media),
                "-map", "0:a:0", "-t", "1", "-ac", "1", "-ar", "8000", "-f", "s16le", "-",
            ], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            samples = [sample / 32768 for (sample,) in struct.iter_unpack("<h", raw)]
            in_phase = sum(value * math.cos(i * math.tau * 550 / 8000) for i, value in enumerate(samples))
            quadrature = sum(value * math.sin(i * math.tau * 550 / 8000) for i, value in enumerate(samples))
            amplitude = 2 * math.hypot(in_phase, quadrature) / len(samples)
            assert len(samples) == 8000 and amplitude > .025, amplitude
            deadline = time.monotonic() + 15
            while True:
                response = requests.delete(base + production + "/delivery", headers=auth, timeout=30)
                if (response.status_code != 409 or "Another operation is preparing this event." not in response.text
                        or time.monotonic() >= deadline):
                    response.raise_for_status()
                    assert response.status_code == 204
                    break
                time.sleep(.5)
            assert requests.get(live, timeout=10).status_code == 404
            assert api("GET", recording) == api("POST", recording) == result
            operator.reload()
            expect(player).to_have_attribute("src", ticket)
            assert player.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}") == width
            manifest = json.loads((home / "collection" / "manifest.json").read_text(encoding="utf8"))
            assert sum(work["id"] == uid for work in manifest["works"]) == 1
            expect(operator.get_by_role("link", name="Video privacy and embed rules")).to_be_visible()

            publication = "/api/policy/publication"
            privacy = f"/api/library/work/{uid}/privacy"
            assert api("GET", "/api/policy")["publication"] is False
            default = api("GET", privacy)
            assert default["visibility"] == "private" and default["watch"] is None
            api("PUT", publication, json={"required": True})
            pending = api("PUT", privacy, json={"visibility": "unlisted", "embed": "disabled", "download": False})
            assert pending["visibility"] == "private" and pending["pending"]["visibility"] == "unlisted"
            assert pending["watch"] is None and requests.get(base + "/watch/work/" + uid, timeout=10).status_code == 404
            api("PUT", publication, json={"required": False})
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                released = api("GET", privacy)
                if released["visibility"] == "unlisted" and released["watch"]:
                    break
                time.sleep(.5)
            else:
                raise AssertionError("The archive's independent share did not release.")
            archive_watch = urljoin(base, released["watch"])
            audience.goto(archive_watch)
            public_player = audience.locator("video")
            assert public_player.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}") == width
            archive_media = urljoin(base, public_player.get_attribute("src"))
            assert requests.get(archive_media, timeout=10, headers={"Range": "bytes=0-511"}).status_code == 206
            expect(audience.locator("video")).to_be_visible()
            assert requests.get(live, timeout=10).status_code == 404
            api("PUT", privacy, json={"visibility": "private", "embed": "disabled", "download": False})
            assert requests.get(archive_watch, timeout=10).status_code == 404
            assert requests.get(archive_media, timeout=10).status_code == 404
            return dict(width=width, duration_seconds=round(info["duration_seconds"], 2),
                        decoded_550_hz_audio_amplitude=round(amplitude, 5), saved_by_default=True,
                        audience_replay_approval_held=True, private_ticket_preview=True,
                        retained_after_withdrawal=True, idempotent_library_entry=True,
                        recording_title_uses_scheduled_time_zone=True, optional_publication_approval=True,
                        independent_unlisted_archive_decoded=True, revoking_archive_denies_issued_media=True,
                        stored_playback_sha256=asset["sha256"])
        finally:
            browser.close()
