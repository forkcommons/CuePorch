"""Verify independent account-free browser speakers in a real hosted broadcast."""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import json

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_events import archive_has_scene_audio, play_event


MEDIA_FIXTURE = """(color => {
  navigator.mediaDevices.getUserMedia = async () => {
    const canvas = document.createElement('canvas');
    canvas.width = 640;canvas.height = 360;
    const painter = canvas.getContext('2d');
    painter.fillStyle = color;
    const paint = () => painter.fillRect(0, 0, canvas.width, canvas.height);
    paint();
    setInterval(paint, 40);
    const media = canvas.captureStream(25);
    const sound = new AudioContext();
    const output = sound.createMediaStreamDestination();
    const tone = sound.createOscillator();
    tone.frequency.value = 1232;
    tone.connect(output);
    tone.start();
    await sound.resume();
    media.addTrack(output.stream.getAudioTracks()[0]);
    return media;
  };
})"""


def wait_color(video, rgb):
    observed = video.evaluate("""async (v,rgb) => {
      const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
      const ctx=canvas.getContext('2d',{willReadFrequently:true});
      const deadline=Date.now()+45000;
      let pixel;
      while(Date.now()<deadline) {
        if(v.videoWidth && v.readyState>=2) {
          ctx.drawImage(v,v.videoWidth*.24,v.videoHeight*.48,1,1,0,0,1,1);
          pixel=[...ctx.getImageData(0,0,1,1).data].slice(0,3);
          if(pixel.every((point,index)=>Math.abs(point-rgb[index])<45)) return {matched:true};
        }
        await new Promise(resolve=>setTimeout(resolve,300));
      }
      return {matched:false,pixel,width:v.videoWidth,position:v.currentTime};
    }""", rgb)
    assert observed["matched"], observed


def check_speaker_delivery(base, keys, ffmpeg):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    end = (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
    auth = {"Authorization": "Bearer " + keys["operator"]}
    created = requests.post(base + "/api/live-events", headers=auth, timeout=10, json=dict(
        title="Independent speaker broadcast", visibility="public", type="webinar", latency="standard",
        schedule=dict(timezone="UTC", start=start, end=end, repeat="none"), save_replay=False,
    ))
    created.raise_for_status()
    entry = created.json()
    (slot,) = entry["occurrences"]
    suffix = f"{entry['id']}/{slot['id']}"
    roster_url = f"{base}/api/live-events/{suffix}/speakers"
    assert requests.get(roster_url, timeout=10).status_code == 403
    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.goto(f"{base}/operator/live-events/{suffix}/studio")
            panel = operator.locator("#studioSpeakers")
            panel.get_by_text("Speakers", exact=True).click()
            review = panel.get_by_label("Approve invited speakers before they connect")
            expect(review).not_to_be_checked()

            def invite(name, color):
                panel.get_by_label("Speaker name").fill(name)
                with operator.expect_response(lambda result: result.url == roster_url and result.request.method == "POST") as issued:
                    panel.get_by_role("button", name="Create invitation").click()
                assert issued.value.status == 201
                address = panel.get_by_label("Speaker invitation link").input_value()
                assert urlsplit(address).fragment
                participant = browser.new_page()
                participant.add_init_script(MEDIA_FIXTURE + f"({json.dumps(color)})")
                participant.goto(address)
                participant.get_by_role("button", name="Join with camera and mic").click()
                expect(participant.get_by_label("My camera preview")).to_be_visible()
                return participant, address

            first, invitation = invite("Visiting artist", "#00ff00")
            expect(first.locator("#guestSpeakerStatus")).to_contain_text("Your feed is off air", timeout=25000)
            wait_color(panel.get_by_label("Preview of Visiting artist"), [0, 255, 0])
            old = urlsplit(invitation)
            endpoint = base + "/api" + old.path
            assert requests.post(endpoint, headers={"Authorization": "Bearer " + old.fragment}, timeout=10).status_code == 404
            assert requests.get(endpoint, timeout=10).status_code == 404
            with operator.expect_response(lambda result: result.url == roster_url and result.request.method == "PUT") as required:
                review.check()
            assert required.value.status == 204
            second, _ = invite("Visiting producer", "#ff00ff")
            expect(second.locator("#guestSpeakerStatus")).to_contain_text("Waiting for the producer")
            expect(panel.get_by_label("Preview of Visiting producer")).to_have_count(0)
            expect(second.get_by_role("region", name="Speaker chat")).to_have_count(0)
            with operator.expect_response(lambda result: result.url == roster_url and result.request.method == "PUT") as automatic:
                review.uncheck()
            assert automatic.value.status == 204
            expect(second.locator("#guestSpeakerStatus")).to_contain_text("Your feed is off air", timeout=25000)
            wait_color(panel.get_by_label("Preview of Visiting producer"), [255, 0, 255])
            first.get_by_label("Message to speakers").fill("Please play the archival introduction first.")
            first.get_by_role("button", name="Send to speakers").click()
            expect(panel.get_by_label("Private speaker messages")).to_contain_text("Please play the archival introduction first.")
            expect(second.get_by_label("Private speaker messages")).to_contain_text("Please play the archival introduction first.")
            assert requests.get(roster_url + "/chat", timeout=10).status_code == 403
            panel.get_by_role("button", name="Put on air · Visiting artist").click()
            expect(first.locator("#guestSpeakerStatus")).to_have_text("You are on air.")
            watch = base + entry["watch"] + f"?occurrence={slot['id']}"
            audience = browser.new_page()
            audience.goto(watch)
            expect(audience.get_by_role("region", name="Speaker chat")).to_have_count(0)
            operator.get_by_role("button", name="Go live").click()
            expect(operator.locator("#recordStatus")).to_contain_text("Sending the live broadcast", timeout=45000)
            expect(audience.locator("#eventStatus")).to_have_text("Live", timeout=65000)
            _, width, _ = play_event(audience, watch, "Live", navigate=False)
            player = audience.frame_locator('iframe[title="Event video"]').locator("video").first
            wait_color(player, [0, 255, 0])
            panel.get_by_role("button", name="Remove Visiting artist").click()
            expect(first.locator("#guestSpeakerStatus")).to_contain_text("expired or was removed", timeout=12000)
            panel.get_by_role("button", name="Put on air · Visiting producer").click()
            expect(second.locator("#guestSpeakerStatus")).to_have_text("You are on air.")
            wait_color(player, [255, 0, 255])
            operator.get_by_role("button", name="Stop", exact=True).click()
            expect(operator.locator("#recordStatus")).to_contain_text("Recording ready.", timeout=20000)
            assert archive_has_scene_audio(operator, ffmpeg), "The private recording is missing the guest WebRTC audio tone"
            return dict(single_use_invitation=True, guest_joins_without_account=True, approved_entry_held_when_enabled=True,
                        turning_gate_off_releases_guest=True, authorized_producer_only=True, removal_revokes_guest=True,
                        two_actual_webrtc_feeds_seen_through_peertube=True, guest_width=width, guest_audio_recorded=True,
                        speaker_chat_excluded_from_audience=True)
        finally:
            browser.close()
