"""Exercise the real local TextFlowKit runtime from the CuePorch package."""

import json
import subprocess
import uuid

import requests
from playwright.sync_api import expect


def speech_video(audio, output):
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=256x144:r=24",
        "-i", str(audio), "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", "-shortest",
        "-movflags", "+faststart", str(output),
    ], check=True)
    return output


def check_caption_generation(page, browser, base, auth, video):
    policy = base + "/api/policy/caption"

    def set_gate(required):
        requests.put(policy, headers=auth, json={"required": required}, timeout=10).raise_for_status()

    def upload(title):
        with video.open("rb") as stream:
            response = requests.post(
                base + "/api/library/uploads", headers=auth, timeout=30,
                files={"file": (title + ".mp4", stream, "video/mp4")},
                data=dict(upload_id=str(uuid.uuid4()), privacy=json.dumps(
                    {"visibility": "public", "embed": "disabled", "domains": [], "download": False}
                )),
            )
        response.raise_for_status()
        return response.json()["id"]

    def set_language(uid):
        page.goto(f"{base}/operator/videos/{uid}/languages")
        expect(page.get_by_label("Generate subtitles when setting the original language")).to_be_checked()
        page.get_by_label("Original language", exact=True).fill("en")
        page.get_by_role("button", name="Set original language").click()
        expect(page.get_by_role("status")).to_have_text("Original language saved.")
        return f"{base}/api/library/work/{uid}"

    def captions(endpoint):
        expect(page.locator("#captionJobStatus")).to_have_text("TextFlowKit subtitles are ready.", timeout=60000)
        expect(page.get_by_role("button", name="Edit en", exact=True)).to_be_visible()
        response = requests.get(endpoint + "/languages/en", headers=auth, timeout=10)
        response.raise_for_status()
        cues = response.json()["cues"]
        assert any("council" in cue["text"].lower() for cue in cues)
        assert any("public session" in cue["text"].lower() for cue in cues)
        return cues

    set_gate(False)
    first = upload("Automatically captioned meeting")
    first_endpoint = set_language(first)
    real_cues = captions(first_endpoint)
    initial = requests.get(first_endpoint + "/languages", headers=auth, timeout=10).json()
    assert initial["tracks"][0]["provenance"] == "TextFlowKit machine-generated"

    set_gate(True)
    try:
        second = upload("Meeting with caption approval")
        second_endpoint = set_language(second)
        expect(page.locator("#captionJobStatus")).to_have_text("Waiting for caption approval.")
        assert requests.get(second_endpoint + "/languages/en", headers=auth, timeout=10).status_code == 404
    finally:
        set_gate(False)
    assert len(captions(second_endpoint)) == len(real_cues)

    guest = browser.new_context()
    try:
        view = guest.new_page()
        view.goto(base + "/watch/work/" + second)
        view.wait_for_function("""() => {
            const track = document.querySelector('video')?.textTracks[0];
            if (!track) return false;
            track.mode = 'hidden';
            return track.cues?.length > 0;
        }""")
        player = view.locator("video")
        assert player.evaluate("async v => {v.muted=true;await v.play();return v.videoWidth}") == 256
        assert "council" in player.evaluate("v=>v.textTracks[0].cues[0].text.toLowerCase()")
    finally:
        guest.close()
    return dict(actual_textflowkit_engine=True, automatic_metadata_trigger=True,
                holds_generation_with_optional_approval=True, disabling_approval_resumes_same_job=True,
                rendered_timed_synthetic_speech_cues=len(real_cues), decoded_guest_video_width=256)
