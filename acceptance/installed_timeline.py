"""Exercise queued multi-clip editing with recovered drafts and guest playback."""

import json
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing

import requests
from playwright.sync_api import expect


def timeline_fixtures(root):
    video, audio = root / "green.mp4", root / "music.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=lime:s=320x180:r=24:d=1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=262:duration=3",
                    str(audio)], check=True)
    return video, audio


def check_timeline(page, browser, base, auth, master, green, music, database):
    def upload(path, name, mime, public=False):
        settings = {"visibility": "public", "embed": "anywhere", "domains": [], "download": False}
        data = {"upload_id": str(uuid.uuid4())}
        if public:
            data["privacy"] = json.dumps(settings)
        with path.open("rb") as stream:
            result = requests.post(base + "/api/library/uploads", headers=auth, timeout=30,
                                   data=data, files={"file": (name, stream, mime)})
        result.raise_for_status()
        return result.json()["id"]

    uid = upload(master, "Timeline original.mp4", "video/mp4", True)
    added = upload(green, "Title lead-in.mp4", "video/mp4")
    soundtrack = upload(music, "Fixture score.wav", "audio/wav")
    endpoint = f"{base}/api/library/work/{uid}"

    def read(suffix):
        response = requests.get(endpoint + suffix, headers=auth, timeout=15)
        response.raise_for_status()
        return response.json()

    assert requests.get(endpoint + "/timeline", timeout=10).status_code == 403
    initial = read("/timeline")
    cut = initial["current"]
    captions = (b"WEBVTT\n\n00:00:00.100 --> 00:00:00.900\nIntro outside edit\n\n"
                b"00:00:01.200 --> 00:00:01.700\nJoin blue scene\n")
    requests.post(endpoint + "/languages", headers=auth, timeout=12, data=dict(version=cut, language="en"),
                  files={"track": ("narration.vtt", captions, "text/vtt")}).raise_for_status()
    page.goto(f"{base}/operator/videos/{uid}")
    page.get_by_role("link", name="Timeline editor").click()
    clips = page.locator("#timelineClips > li")
    expect(clips).to_have_count(1)
    clips.nth(0).get_by_role("button", name="Timeline original").click()
    expect(page.get_by_label("Timeline source video")).to_be_visible()
    page.get_by_label("Find text").fill("Join blue")
    expect(page.locator("#timelineCues > li")).to_have_count(1)
    page.get_by_role("button", name="Split before").click()
    expect(clips).to_have_count(2)
    clips.nth(0).get_by_role("button", name="Remove", exact=True).click()
    expect(clips).to_have_count(1)
    page.get_by_role("button", name="Undo", exact=True).click()
    expect(clips).to_have_count(2)
    page.get_by_role("button", name="Redo", exact=True).click()
    expect(clips).to_have_count(1)
    clips.nth(0).get_by_label("End (seconds)").fill("2.500")
    clips.nth(0).get_by_label("End (seconds)").press("Tab")
    page.get_by_label("Add video", exact=True).select_option(added)
    page.get_by_role("button", name="Add to timeline", exact=True).click()
    expect(clips).to_have_count(2)
    clips.nth(1).get_by_role("button", name="Earlier", exact=True).click()
    page.get_by_role("combobox", name="Aspect ratio").select_option("9:16")
    page.get_by_label("Audio work", exact=True).select_option(soundtrack)
    page.get_by_label("Volume (%)").fill("20")
    page.get_by_role("button", name="Apply background audio", exact=True).click()
    expect(page.locator("#timelineStatus")).to_have_text("Draft saved.")
    page.reload()
    expect(page.get_by_text("Opened your saved draft.")).to_be_visible()
    expect(clips.nth(0)).to_contain_text("Title lead-in")
    expect(clips.nth(1)).to_contain_text("Timeline original")
    expect(page.get_by_role("combobox", name="Aspect ratio")).to_have_value("9:16")
    assert requests.put(endpoint + "/timeline", headers=auth, timeout=10,
                        json=dict(revision=None, base=cut, settings=initial["settings"])).status_code == 409
    saved = read("/timeline")
    page.get_by_role("button", name="Render as a new version").click()
    expect(page.locator("#timelineStatus")).to_have_text("New version saved.", timeout=60000)
    rendered = read("/timeline")
    job = rendered["job"]
    assert job["state"] == "saved" and job["result"] != cut and rendered["current"] == job["result"]
    retry = requests.post(endpoint + "/timeline/render", headers=auth, timeout=10,
                          json=dict(revision=saved["revision"]))
    assert retry.status_code == 200 and retry.json() == job
    saved_versions = read("/versions")
    with closing(sqlite3.connect(database, timeout=10)) as con:
        with con:
            con.execute("UPDATE video_editor_job SET state='rendering',result=NULL WHERE id=?", (job["id"],))
    deadline = time.monotonic() + 10
    receipt = read("/timeline/render/" + job["id"])
    while receipt["state"] == "rendering" and time.monotonic() < deadline:
        time.sleep(.15)
        receipt = read("/timeline/render/" + job["id"])
    assert receipt == job and read("/versions") == saved_versions
    captions = read("/languages/en")["cues"]
    assert [(c["start"], c["end"], c["text"]) for c in captions] == [(1000, 1500, "Join blue scene")]
    media = read("/details")["media"]
    assert media["width"] == 720 and media["height"] == 1280 and media["audio"]
    assert any(version["key"] == cut and not version["current"] for version in saved_versions)
    old = requests.post(endpoint + "/timeline/preview", headers=auth, timeout=10, json={"cut": cut})
    old.raise_for_status()
    old_preview = requests.get(base + old.json()["preview"], timeout=10, headers={"Range": "bytes=0-127"})
    assert old_preview.status_code == 206 and b"ftyp" in old_preview.content

    guest = browser.new_context()
    try:
        screened = guest.new_page()
        screened.goto(f"{base}/watch/work/{uid}")
        video = screened.locator("video")
        assert video.evaluate("""async v=>{
          v.muted=true;await v.play();v.pause();
          async function sample(t) {
            const settled=new Promise(resolve=>v.addEventListener('seeked',resolve,{once:true}));
            v.currentTime=t;await settled;
            const canvas=document.createElement('canvas');canvas.width=1;canvas.height=1;
            const ctx=canvas.getContext('2d');
            ctx.drawImage(v,Math.floor(v.videoWidth/2),Math.floor(v.videoHeight/2),1,1,0,0,1,1);
            return ctx.getImageData(0,0,1,1).data;
          }
          const green=await sample(.2),blue=await sample(1.3);
          return v.videoWidth===720 && v.videoHeight===1280 && v.duration>2.2 && v.duration<2.4 &&
            green[1]>190&&green[2]<40&&blue[2]>190&&blue[1]<40;
        }""")
    finally:
        guest.close()
    return dict(transcript_split_and_remove=True, undo_and_redo=True, added_and_reordered_library_media=True,
                recovered_draft=True, rejected_stale_autosave=True, queued_render_with_idempotency=True,
                reconciled_interrupted_render_receipt=True,
                decoded_portrait_render_in_correct_order=True, rebased_caption_and_mixed_audio=True,
                original_cut_retained=True)
