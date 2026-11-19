"""Exercise timed editor selection, GIF download and reversible cut changes."""

import hashlib
import json
import subprocess
import uuid

import requests
from playwright.sync_api import expect


def fixture(path):
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=256x144:r=24:d=1",
        "-f", "lavfi", "-i", "color=blue:s=256x144:r=24:d=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-c:a", "aac", str(path),
    ], check=True)


def check_editing(page, browser, base, auth, collection, master):
    public = {"visibility": "public", "embed": "anywhere", "domains": [], "download": False}
    with master.open("rb") as stream:
        upload = requests.post(
            base + "/api/library/uploads", headers=auth, timeout=30,
            files={"file": ("Editor colors.mp4", stream, "video/mp4")},
            data=dict(upload_id=str(uuid.uuid4()), privacy=json.dumps(public)),
        )
    upload.raise_for_status()
    uid = upload.json()["id"]
    endpoint = f"{base}/api/library/work/{uid}"
    edit_url = endpoint + "/editing"

    def read(suffix):
        response = requests.get(endpoint + suffix, headers=auth, timeout=12)
        response.raise_for_status()
        return response.json()

    source = read("/editing")
    prior = source["version"]
    assert 3900 <= source["duration"] <= 4100
    assert requests.get(edit_url, timeout=10).status_code == 403
    assert requests.post(edit_url + "/trim", headers=auth, timeout=10,
                         json=dict(version=prior, start=800, end=99999)).status_code == 422
    subtitle = (b"WEBVTT\n\n00:00:00.100 --> 00:00:01.400\nOpening colors\n\n"
                b"00:00:01.200 --> 00:00:02.000\nBlue begins\n\n"
                b"00:00:03.200 --> 00:00:03.800\nEnding outside the cut\n")
    requests.post(endpoint + "/languages", headers=auth, timeout=12, data=dict(language="en", version=prior),
                  files={"track": ("colors.vtt", subtitle, "text/vtt")}).raise_for_status()
    requests.put(endpoint + "/languages", headers=auth, timeout=10,
                 json=dict(language="en", version=prior, generate=False)).raise_for_status()
    prior_chapter = read("/chapters")
    chapter_list = [dict(title="Opening", timecode=0), dict(title="Blue sequence", timecode=2),
                    dict(title="Original end", timecode=3)]
    requests.put(endpoint + "/chapters", headers=auth, timeout=10,
                 json=prior_chapter | {"chapters": chapter_list}).raise_for_status()

    page.goto(f"{base}/operator/videos/{uid}")
    page.get_by_role("link", name="Edit video", exact=True).click()
    expect(page.get_by_role("heading", name="Edit Editor colors")).to_be_visible()
    page.get_by_label("Find in transcript").fill("Blue begins")
    expect(page.locator("#editCues > li")).to_have_count(1)
    page.get_by_role("button", name="Set start", exact=True).click()
    expect(page.get_by_label("Start (seconds)")).to_have_value("1.200")
    page.wait_for_function("document.querySelector('#editPreview').readyState >= 2")
    page.get_by_role("slider", name="Timeline").evaluate(
        "(slider, end) => {slider.value=String(end);slider.dispatchEvent(new Event('input'))}", 2.7)
    page.wait_for_function("Math.abs(document.querySelector('#editPreview').currentTime - 2.7) < .02")
    page.get_by_role("button", name="End at preview time").click()
    expect(page.get_by_label("End (seconds)")).to_have_value("2.700")

    with page.expect_download(timeout=25000) as downloaded:
        page.get_by_role("button", name="Download GIF").click()
    gif = downloaded.value.path().read_bytes()
    assert gif[:6] == b"GIF89a"
    assert int.from_bytes(gif[6:8], "little") == 256
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(downloaded.value.path()), "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-frames:v", "2", "-"],
        capture_output=True, check=True,
    )
    assert len(result.stdout) == 2 * 256 * 144 * 3
    assert all(result.stdout[index + 2] > 200 and result.stdout[index] < 40
               for index in [0, 256 * 144 * 3])
    assert read("/editing")["version"] == prior
    page.get_by_role("button", name="Save as new version").click()
    expect(page.locator("#editStatus")).to_have_text("New version saved.", timeout=30000)
    trimmed = read("/editing")
    assert trimmed["version"] != prior and 1400 <= trimmed["duration"] <= 1600
    assert requests.post(edit_url + "/trim", headers=auth, timeout=10,
                         json=dict(version=prior, start=100, end=1200)).status_code == 409
    cues = read("/languages/en")["cues"]
    assert [(cue["start"], cue["end"], cue["text"]) for cue in cues] == [
        (0, 200, "Opening colors"), (0, 800, "Blue begins"),
    ]
    assert read("/languages")["original_language"] == "en"
    assert read("/chapters")["chapters"] == [chapter_list[0], dict(title="Blue sequence", timecode=1)]
    original = next(item for item in read("/versions") if item["key"] == prior)
    digest = next(f["sha256"] for f in original["video_files"] if f["role"] == "original")
    assert digest == hashlib.sha256(master.read_bytes()).hexdigest()
    assert hashlib.sha256((collection / "objects" / digest).read_bytes()).hexdigest() == digest

    guest = browser.new_context()
    try:
        player = guest.new_page()
        player.goto(f"{base}/watch/work/{uid}")
        video = player.locator("video")
        assert video.evaluate("""async v => {
          v.muted=true;await v.play();
          const canvas = document.createElement('canvas');
          canvas.width=1;canvas.height=1;
          const context=canvas.getContext('2d');
          context.drawImage(v,0,0,1,1);v.pause();
          const pixel=context.getImageData(0,0,1,1).data;
          return v.videoWidth===256 && v.duration>1.4 && v.duration<1.6 && pixel[2]>200 && pixel[0]<40;
        }""")
        requests.post(endpoint + "/versions/restore", headers=auth, timeout=12,
                      json=dict(current=trimmed["version"], restore=prior)).raise_for_status()
        player.reload()
        assert video.evaluate("async v => {v.muted=true;await v.play();return v.videoWidth===256 && v.duration>=3.9}")
        assert read("/languages/en")["cues"][-1]["start"] == 3200
        assert read("/chapters")["chapters"] == chapter_list
    finally:
        guest.close()
    return dict(timeline_and_transcript=True, decoded_gif_frames=2, gif_did_not_change_version=True,
                guest_trimmed_cut_decoded=True, clipped_captions_and_chapters=True,
                earlier_master_digest_unchanged=True, restored_cut_and_captions_played=True,
                rejects_invalid_and_stale_ranges=True)
