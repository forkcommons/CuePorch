"""Verify alternate audio signal, controls, archive and access in the installed app."""

import csv
from io import StringIO
import json
import subprocess
import uuid
from urllib.parse import urljoin, urlsplit

import requests
from playwright.sync_api import expect


def make_media(path, tone, video=False):
    picture = ["-f", "lavfi", "-i", "testsrc2=s=256x144:r=24:d=6"] if video else []
    encoding = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"] if video else []
    subprocess.run(["ffmpeg", "-v", "error", *picture, "-f", "lavfi", "-i", f"sine=f={tone}:d=6",
                    *encoding, str(path)], check=True)
    return path


SAMPLE_AUDIO = """async video => {
    let measurement = video.audioFixture;
    if (!measurement) {
      const context = new AudioContext();
      const analyser = context.createAnalyser();
      analyser.fftSize = 4096;
      analyser.smoothingTimeConstant = 0;
      const output = context.createGain();
      output.gain.value = 0;
      context.createMediaElementSource(video).connect(analyser).connect(output).connect(context.destination);
      measurement = video.audioFixture = {context, analyser};
    }
    const {context, analyser} = measurement;
    await Promise.race([context.resume(), new Promise((_, reject) => {
      setTimeout(() => reject(new Error('The browser audio context did not resume.')), 3000);
    })]);
    await video.play();
    await new Promise(resolve => setTimeout(resolve, 130));
    const data = new Float32Array(analyser.frequencyBinCount);
    for (let attempt = 0; attempt < 24; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 45));
      analyser.getFloatFrequencyData(data);
      let peak = 0;
      for (let index = 1; index < data.length; index++) if (data[index] > data[peak]) peak = index;
      if (data[peak] > -50) return Math.round(peak * context.sampleRate / analyser.fftSize);
    }
    throw new Error('No decoded audio signal reached the browser analyser.');
}"""


def check_audio_tracks(page, browser, base, staff, museum, venue, replacement):
    root = replacement.parent
    label = uuid.uuid4().hex
    master = make_media(root / f"recording-{label}.mp4", 440, video=True)
    dubbed = make_media(root / f"dubbed-{label}.wav", 880)
    narration = make_media(root / f"described-{label}.mp4", 660, video=True)
    alternate = make_media(root / f"revised-{label}.wav", 990)
    password = "CuePorch alternate audio rehearsal!"
    privacy = dict(visibility="password", embed="domains", domains=[urlsplit(venue).netloc],
                   password=password, download=True)
    title = "Bilingual public screening"
    with master.open("rb") as stream:
        result = requests.post(base + "/api/library/uploads", headers=staff, timeout=40,
                               files={"file": (title + ".mp4", stream, "video/mp4")},
                               data=dict(upload_id=label, privacy=json.dumps(privacy)))
    result.raise_for_status()
    uid = result.json()["id"]
    work = f"{base}/api/library/work/{uid}"
    audio = work + "/audio-tracks"
    watch = f"{base}/watch/work/{uid}"
    embed = f"{base}/embed/work/{uid}"

    def get(url):
        reply = requests.get(url, headers=staff, timeout=10)
        reply.raise_for_status()
        return reply.json()

    def report(kind="video", **options):
        reply = requests.post(f"{base}/api/analytics/{kind}-report", headers=staff, timeout=10,
                              json={"videos": [uid], "group": "audio"} | options)
        reply.raise_for_status()
        return reply.json()

    def pause_and_flush(target, player):
        with target.expect_response(lambda reply: "/viewer-analytics/" in reply.url and reply.status == 204):
            player.evaluate("v => v.pause()")

    def unlock(target):
        expect(target.locator("video")).to_have_count(0)
        expect(target.get_by_role("combobox", name="Audio track", exact=True)).to_have_count(0)
        target.get_by_label("Video password").fill(password)
        target.get_by_role("button", name="Watch video").click()
        video = target.locator("video")
        video.click(position={"x": 12, "y": 12})
        return video

    def tone(video, frequency):
        measured = video.evaluate(SAMPLE_AUDIO)
        assert abs(measured - frequency) < 24, (measured, frequency)
        return measured

    def choose(target, value):
        control = target.get_by_role("combobox", name="Audio track", exact=True)
        control.select_option(value)
        video = target.locator("video")
        expect(video).to_have_attribute("data-audio-track", value)
        expect(control).to_be_enabled()
        return video

    def upload_operator(path, language, kind):
        page.get_by_label("Audio language").fill(language)
        page.get_by_label("Audio type").select_option(kind)
        page.get_by_label("Audio file").set_input_files(path)
        page.get_by_role("button", name="Upload audio", exact=True).click()
        expect(page.locator("#audioUploadStatus")).to_have_text("Audio track saved.", timeout=30000)

    assert requests.get(audio, timeout=10).status_code == 403
    original = get(work + "/versions")[0]["key"]
    page.goto(f"{base}/operator/videos/{uid}")
    page.get_by_role("link", name="Languages and subtitles").click()
    expect(page.get_by_role("heading", name="Languages for " + title)).to_be_visible()
    page.get_by_label("Original language", exact=True).fill("en")
    page.get_by_label("Generate subtitles when setting the original language").uncheck()
    page.get_by_role("button", name="Set original language").click()
    expect(page.locator("#languagesStatus")).to_have_text("Original language saved.")
    page.get_by_label("Subtitle language").fill("en")
    page.get_by_label("Subtitle file").set_input_files({
        "name": "english.vtt", "mimeType": "text/vtt", "buffer": b"WEBVTT\n\n00:00:00.000 --> 00:00:06.000\nEnglish caption\n",
    })
    page.get_by_role("button", name="Upload subtitles").click()
    expect(page.locator("#languagesStatus")).to_have_text("Subtitles uploaded.")
    upload_operator(dubbed, "es", "dubbed")
    upload_operator(narration, "en", "description")
    assert get(audio)["version"] == original
    assert requests.get(audio + "/dubbed/es", headers=staff, timeout=10).content == dubbed.read_bytes()
    with dubbed.open("rb") as stream:
        repeated = requests.post(audio, headers=staff, timeout=10, files={"track": ("dubbed.wav", stream)},
                                 data=dict(version=original, language="es", kind="dubbed"))
    assert repeated.ok and len(repeated.json()["tracks"]) == 2
    invalid = requests.post(audio, headers=staff, timeout=10, files={"track": ("bad.wav", b"invalid audio")},
                            data=dict(version=original, language="fr", kind="commentary"))
    assert invalid.status_code == 422 and len(get(audio)["tracks"]) == 2

    guest = browser.new_context()
    prior_embed = museum.embed
    try:
        viewer = guest.new_page()
        viewer.goto(watch)
        video = unlock(viewer)
        expect(viewer.get_by_role("combobox", name="Audio track", exact=True)).to_have_value("original")
        original_tone = tone(video, 440)
        viewer.wait_for_function("document.querySelector('video').currentTime >= 1.1")
        video.evaluate("v=>{v.currentTime=2;v.playbackRate=1.25;v.textTracks[0].mode='showing';}")
        viewer.wait_for_function("!document.querySelector('video').seeking && document.querySelector('track').readyState === 2")
        video = choose(viewer, "dubbed:es")
        state = video.evaluate("v=>[v.currentTime,v.playbackRate,v.textTracks[0].mode,v.videoWidth,v.paused]")
        assert 1.9 <= state[0] < 3 and state[1:] == [1.25, "showing", 256, False], state
        dubbed_tone = tone(video, 880)
        viewer.wait_for_function("document.querySelector('video').currentTime >= 3.4")
        pause_and_flush(viewer, video)
        tracks = report()
        assert tracks["totals"]["views"] == tracks["totals"]["impressions"] == tracks["totals"]["unique_viewers"] == 1
        by_audio = {row["category"]: row["watched_seconds"] for row in tracks["rows"]}
        assert set(by_audio) == {"en · Original audio", "es · Dubbed audio"} and min(by_audio.values()) >= 0.9, by_audio
        assert abs(sum(by_audio.values()) - tracks["totals"]["watched_seconds"]) < .15, tracks["totals"]
        paired = report("device", breakdown="audio", audio_language="ES", audio_kind="dubbed", caption_kind="subtitles")
        assert paired["totals"]["views"] == paired["totals"]["unique_viewers"] == 1
        assert paired["totals"]["watched_seconds"] >= .85
        assert [(row["category"], row["breakdown"]) for row in paired["rows"]] == [("Desktop", "es · Dubbed audio")]
        assert report(audio_kind="dubbed", caption_kind="none")["totals"]["views"] == 0
        assert any(row["category"] == "en · Original audio" and row["breakdown"] == "Off"
                   and row["watched_seconds"] >= 0.9 for row in report(breakdown="caption")["rows"])
        retention = {row["second"]: row["percent"] for row in get(work + "/analytics")["retention"]}
        assert retention[0] == retention[3] == 1, retention

        with browser.new_context(extra_http_headers={"DNT": "1"}) as private:
            private_page = private.new_page()
            private_page.goto(watch)
            private_video = unlock(private_page)
            assert private_video.get_attribute("data-analytics") is None
            assert abs(tone(choose(private_page, "dubbed:es"), 880) - dubbed_tone) < 24
        assert report()["totals"] == tracks["totals"]

        with viewer.expect_download() as received:
            viewer.get_by_role("link", name="Download video").click()
        assert received.value.suggested_filename == "video-es-dubbed.mp4"
        old_url = urljoin(base, video.get_attribute("src"))
        alternate_bytes = requests.get(old_url, timeout=10).content
        assert received.value.path().read_bytes() == alternate_bytes
        fallback = choose(viewer, "original")
        assert abs(tone(fallback, 440) - original_tone) < 24

        museum.embed = embed
        assert requests.get(embed, headers={"Sec-Fetch-Dest": "iframe", "Referer": "http://localhost:3/"}, timeout=10).status_code == 403
        viewer.goto(venue)
        frame = viewer.frame_locator("iframe")
        iframe_video = unlock(frame)
        assert abs(tone(iframe_video, 440) - original_tone) < 24
        described = choose(frame, "description:en")
        described_tone = tone(described, 660)
        audience_frame = viewer.locator("iframe").element_handle().content_frame()
        audience_frame.wait_for_function("end => document.querySelector('video').currentTime >= end",
                                         arg=described.evaluate("v => v.currentTime + .75"))
        pause_and_flush(viewer, described)
        assert report(surface="embed", audio_kind="description", audio_language="en")["totals"]["watched_seconds"] >= 0.65
        assert report(surface="watch", audio_kind="description")["totals"]["views"] == 0

        page.goto(f"{base}/operator/analytics?video={uid}")
        page.get_by_label("Group by").select_option("audio")
        page.get_by_label("Break down by").select_option("caption")
        page.get_by_label("Audio track language").fill("es")
        page.get_by_label("Audio track type").select_option("dubbed")
        page.get_by_label("Text track type").select_option("subtitles")
        page.get_by_role("button", name="Apply filters").click()
        expect(page.locator("#reportRows")).to_contain_text("es · Dubbed audio")
        expect(page.locator("#reportRows > tr")).to_have_count(1)
        expect(page.locator("#trackBreakdownNote")).to_be_visible()
        with page.expect_download() as exported:
            page.get_by_role("button", name="Download CSV").click()
        rows = list(csv.DictReader(StringIO(exported.value.path().read_text(encoding="utf-8"))))
        assert [(row["audio"], row["caption"], row["views"]) for row in rows] == [("es · Dubbed audio", "en · Subtitles", "1")]
        page.goto(f"{base}/operator/videos/{uid}/languages")

        page.get_by_role("button", name="Replace es dubbed audio").click()
        page.get_by_label("Audio file").set_input_files(alternate)
        page.get_by_role("button", name="Replace audio track", exact=True).click()
        expect(page.locator("#audioUploadStatus")).to_have_text("Audio track saved.", timeout=30000)
        assert requests.get(old_url, timeout=10).status_code == 404
        viewer.goto(watch)
        unlock(viewer)
        revised_video = choose(viewer, "dubbed:es")
        replaced_tone = tone(revised_video, 990)
        revised_url = urljoin(base, revised_video.get_attribute("src"))

        versions = work + "/versions"
        with replacement.open("rb") as stream:
            changed = requests.post(versions, headers=staff, timeout=25, data={"current": original},
                                    files={"video": ("replacement.mp4", stream)})
        changed.raise_for_status()
        current = changed.json()["current"]
        assert current != original and not get(audio)["tracks"]
        assert requests.get(revised_url, timeout=10).status_code == 404
        restored = requests.post(versions + "/restore", headers=staff, timeout=20,
                                 json={"current": current, "restore": original})
        assert restored.ok and restored.json()["current"] == original and len(get(audio)["tracks"]) == 2

        appearance = work + "/appearance/watch"
        settings = get(appearance)["settings"]
        requests.put(appearance, headers=staff, timeout=10, json=settings | {"audio_tracks": False}).raise_for_status()
        viewer.goto(watch)
        unlock(viewer)
        expect(viewer.get_by_role("combobox", name="Audio track", exact=True)).to_have_count(0)
        viewer.goto(venue)
        unlock(frame)
        expect(frame.get_by_role("combobox", name="Audio track", exact=True)).to_be_visible()

        page.reload()
        page.get_by_role("button", name="Remove en audio description").click()
        expect(page.locator("#audioUploadStatus")).to_have_text("Audio track removed.")
        assert [entry["language"] for entry in get(audio)["tracks"]] == ["es"]
        privacy["download"] = False
        requests.put(work + "/privacy", headers=staff, json=privacy, timeout=10).raise_for_status()
        assert requests.get(revised_url, timeout=10).status_code == 404
        viewer.goto(venue)
        unlock(frame)
        alt_video = choose(frame, "dubbed:es")
        assert requests.get(urljoin(base, alt_video.get_attribute("src")) + "?download=1", timeout=10).status_code == 403
        assert requests.get(work + "/audio-tracks/dubbed/es", timeout=10).status_code == 403
        return dict(original_hz=original_tone, dubbed_hz=dubbed_tone, embedded_description_hz=described_tone,
                    replacement_hz=replaced_tone, video_width=state[3], position_and_player_state_preserved=True,
                    cut_scoped_restore=True, original_download_and_optional_viewer_download=True,
                    replacement_revokes_old_asset=True, appearance_scope_and_embed_rights=True,
                    invalid_upload_and_retry_preserved_tracks=True,
                    analytics=dict(watch_seconds=by_audio, paired_seconds=paired["totals"]["watched_seconds"],
                                   unique_watch_viewers=1, original_and_dubbed_retention_preserved=True,
                                   alternate_embed_playback=True, opted_out=True, filtered_csv=True))
    finally:
        guest.close()
        museum.embed = prior_embed
