"""Record simulated devices through browser MediaRecorder and open the stored video."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import wave

import requests
from playwright.sync_api import expect

from browser import chromium


DISPLAY_FIXTURE = """() => {
  navigator.mediaDevices.getDisplayMedia = async () => {
    const canvas = document.createElement('canvas');
    canvas.width = 640;canvas.height = 360;
    const ctx = canvas.getContext('2d');
    const paint = () => {ctx.fillStyle='#0033ff';ctx.fillRect(0,0,640,360);};
    paint();
    const stream = canvas.captureStream(24);
    setInterval(paint, 40);
    return stream;
  };
}"""

PROMPTER_SCRIPT = "\n".join(
    f"Library access and independent arts. Segment {number}." for number in range(1, 22)
)


def check_prompter_controls(page):
    page.get_by_text("Teleprompter", exact=True).click()
    page.get_by_label("Script", exact=True).fill(PROMPTER_SCRIPT)
    view = page.get_by_role("region", name="Teleprompter script")
    expect(view).to_contain_text("Library access")
    speed = page.get_by_role("combobox", name="Scroll speed")
    assert speed.locator("option").all_text_contents() == ["Auto (follow my voice)", "Off", "0.5×", "1×", "1.5×", "2×"]
    play = page.get_by_role("button", name="Play script")
    pause = page.get_by_role("button", name="Pause script")
    speed.select_option("0")
    expect(play).to_be_disabled()
    speed.select_option("2")
    font = page.get_by_role("slider", name="Font size")
    font.focus()
    font.press("End")
    expect(view).to_have_css("font-size", "56px")
    play.click()
    offset = page.wait_for_function("() => document.querySelector('#recordPrompter').scrollTop > 12")
    assert offset.json_value()
    pause.click()
    stationary = view.evaluate("v => v.scrollTop")
    assert view.evaluate("""async v => {
      for (let i = 0; i < 12; i++) await new Promise(requestAnimationFrame);
      return v.scrollTop;
    }""") == stationary
    page.get_by_role("button", name="Reset script").click()
    assert view.evaluate("v => v.scrollTop") == 0


@contextmanager
def synthetic_microphone(playwright, speech):
    with TemporaryDirectory() as folder:
        microphone = Path(folder) / "prompt-microphone.wav"
        with wave.open(str(speech), "rb") as source, wave.open(str(microphone), "wb") as target:
            target.setparams(source.getparams())
            one_second = bytes(source.getframerate() * source.getsampwidth() * source.getnchannels())
            target.writeframes(one_second * 4 + source.readframes(source.getnframes()) + one_second * 10)
        flags = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                 f"--use-file-for-fake-audio-capture={microphone}%noloop"]
        with chromium(playwright, flags) as browser:
            yield browser.new_context(permissions=["camera", "microphone"]).new_page()


def set_voice_script(page):
    script = ("The city council meeting\nwill begin at seven\nthis evening.\n"
              "Welcome to the public session.\n") * 6
    page.get_by_text("Teleprompter", exact=True).click()
    page.get_by_label("Script", exact=True).fill(script)
    font = page.get_by_role("slider", name="Font size")
    font.focus()
    font.press("End")
    page.get_by_role("combobox", name="Scroll speed").select_option("auto")


def spoken_script_offset(page):
    view = page.get_by_role("region", name="Teleprompter script")
    expect(page.locator("#recordPrompterVoice")).to_have_text(
        "Following your voice with local TextFlowKit.", timeout=12000,
    )
    assert view.evaluate("node => node.scrollTop") == 0
    page.wait_for_function("() => document.querySelector('#recordPrompter').scrollTop > 60", timeout=25000)
    return view.evaluate("node => node.scrollTop")


def check_voice_prompter(playwright, base, operator, speech, studio_path="/operator/record"):
    endpoint = base + "/api/studio/teleprompter/voice"
    assert requests.post(endpoint, timeout=10).status_code == 403
    with synthetic_microphone(playwright, speech) as page:
        page.goto(base + studio_path)
        page.evaluate("(key) => sessionStorage.setItem('cueporchOperator', key)", operator["Authorization"][7:])
        page.reload()
        set_voice_script(page)
        page.get_by_role("button", name="Play script").click()
        notice = page.locator("#recordPrompterVoice")
        expect(notice).to_have_text("Connect your microphone to follow your voice.")
        with page.expect_response(lambda response: response.url == endpoint and response.request.method == "POST") as opened:
            page.get_by_role("button", name="Connect camera and microphone").click()
        assert opened.value.status == 201, opened.value.status
        session = endpoint + "/" + opened.value.json()["session"]
        assert requests.post(session, timeout=10).status_code == 403
        assert requests.post(session, headers=operator, data=b"fixture", timeout=10).status_code == 400
        used = spoken_script_offset(page)
        with page.expect_response(lambda response: response.url == session and response.request.method == "DELETE") as stopped:
            page.get_by_role("button", name="Pause script").click()
        assert stopped.value.status == 204
        retired = requests.post(session, headers=operator | {"Content-Type": "application/octet-stream"},
                                data=bytes(16000), timeout=10)
        assert retired.status_code in (404, 410)
        expect(notice).to_have_text("Voice follow paused.")
        return dict(engine="local TextFlowKit", silent_script_offset=0, spoken_script_offset=used,
                    unauthorized_and_malformed_audio_denied=True, stopped_session_rejects_audio=True)


def check_guest_prompter(playwright, base, operator, speech, speakers):
    def invite(name):
        made = requests.post(speakers, headers=operator, json={"name": name}, timeout=10)
        made.raise_for_status()
        entry = made.json()
        return entry, base + "/api/guest-speaker/" + entry["id"]

    first, first_api = invite("Guest with a script")
    speaker = speakers + "/" + first["id"]
    endpoint = first_api + "/teleprompter/voice"
    requests.put(speakers, headers=operator, json={"review": True}, timeout=10).raise_for_status()
    with synthetic_microphone(playwright, speech) as page:
        page.goto(base + first["link"])
        expect(page.get_by_text("Teleprompter", exact=True)).to_be_hidden()
        with page.expect_response(lambda response: response.url == first_api and response.request.method == "POST") as joined:
            page.get_by_role("button", name="Join with camera and mic").click()
        assert joined.value.status == 201
        first_auth = {"Authorization": "Bearer " + joined.value.json()["session"]}
        expect(page.locator("#guestSpeakerStatus")).to_have_text("Waiting for the producer to admit you.")
        assert requests.post(endpoint, headers=first_auth, timeout=10).status_code == 404
        requests.put(speaker, headers=operator, json={"admit": True}, timeout=10).raise_for_status()
        set_voice_script(page)
        with page.expect_response(lambda response: response.url == endpoint and response.request.method == "POST") as opened:
            page.get_by_role("button", name="Play script").click()
        assert opened.value.status == 201
        voice_id = opened.value.json()["session"]
        voice = endpoint + "/" + voice_id
        used = spoken_script_offset(page)

        requests.put(speakers, headers=operator, json={"review": False}, timeout=10).raise_for_status()
        second, second_api = invite("Other guest")
        joined = requests.post(second_api, timeout=10,
                               headers={"Authorization": "Bearer " + second["link"].partition("#")[2]})
        joined.raise_for_status()
        second_auth = {"Authorization": "Bearer " + joined.json()["session"]}
        assert requests.get(second_api, headers=second_auth, timeout=10).json()["admitted"]
        assert requests.delete(second_api + "/teleprompter/voice/" + voice_id, headers=second_auth, timeout=10).status_code == 404
        assert requests.delete(voice, headers=second_auth, timeout=10).status_code == 404

        requests.delete(speaker, headers=operator, timeout=10).raise_for_status()
        assert requests.post(voice, headers=first_auth | {"Content-Type": "application/octet-stream"},
                             data=bytes(16000), timeout=10).status_code == 404
        expect(page.get_by_text("Teleprompter", exact=True)).to_be_hidden()
        requests.delete(second_api, headers=second_auth, timeout=10).raise_for_status()
        return dict(engine="local TextFlowKit", silent_script_offset=0, spoken_script_offset=used,
                    optional_admission_enforced=True, independent_speaker_denied=True, revoked_speaker_ipc_denied=True)


def check_recording(playwright, base, auth, clip_file):
    title = "Studio accessibility rehearsal"
    created = requests.post(base + "/api/library/folders", headers=auth, timeout=12,
                            json=dict(name="Rehearsal recordings", color="#286266", parent=None))
    created.raise_for_status()
    folder = created.json()["id"]
    browser = chromium(playwright, ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
    try:
        context = browser.new_context(permissions=["camera", "microphone"])
        context.add_init_script("(" + DISPLAY_FIXTURE + ")()")
        page = context.new_page()
        page.goto(base + "/operator/library")
        page.evaluate("(key) => sessionStorage.setItem('cueporchOperator',key)", auth["Authorization"][7:])
        page.get_by_role("link", name="Record Studio").click()
        expect(page.get_by_role("heading", name="Record Studio")).to_be_visible()
        page.get_by_label("Recording title").fill(title)
        page.get_by_label("Destination folder").select_option(folder)
        page.get_by_role("combobox", name="Aspect ratio").select_option("1:1")
        page.get_by_role("combobox", name="Layout").select_option("side")
        check_prompter_controls(page)
        page.get_by_role("button", name="Connect camera and microphone").click()
        expect(page.locator("#recordStatus")).to_have_text("Recording devices connected.")
        page.get_by_role("button", name="Share screen", exact=True).click()
        expect(page.locator("#recordStatus")).to_have_text("Screen sharing is ready.")
        expect(page.locator("#recordPrompter")).to_be_hidden()
        expect(page.get_by_text("Script preview is off during screen sharing.")).to_be_visible()
        page.wait_for_function("""() => {
          const canvas=document.querySelector('#recordCanvas');
          return canvas.getContext('2d').getImageData(180,360,1,1).data[2]>235;
        }""")
        page.get_by_role("button", name="Start recording").click()
        expect(page.locator("#recordStatus")).to_have_text("Recording.")
        expect(page.locator("#recordTime")).to_have_text("0:01", timeout=5000)
        page.get_by_role("button", name="Pause", exact=True).click()
        expect(page.locator("#recordStatus")).to_have_text("Recording paused.")
        page.get_by_role("button", name="Resume", exact=True).click()
        expect(page.locator("#recordTime")).to_have_text("0:02", timeout=5000)
        page.get_by_role("button", name="Stop", exact=True).click()
        expect(page.locator("#recordStatus")).to_contain_text("Recording ready.")
        video = page.get_by_label("Finished recording")
        assert video.evaluate("async v=>{v.muted=true;await v.play();return v.videoWidth===720 && v.videoHeight===720;}")
        page.reload()
        page.get_by_role("button", name="Recover " + title, exact=True).click()
        expect(page.locator("#recordStatus")).to_contain_text("Recovered recording.")
        decoded = video.evaluate("""async v=>{
          v.muted=true;await v.play();
          const c=document.createElement('canvas'); c.width=720;c.height=720;
          const ctx=c.getContext('2d');
          return new Promise(resolve => {
            const read = () => {
              ctx.drawImage(v,0,0);
              const left=Array.from(ctx.getImageData(180,360,1,1).data);
              return {width:v.videoWidth, height:v.videoHeight, left, currentTime:v.currentTime};
            };
            let frame;
            const done = value => {clearTimeout(timeout);v.cancelVideoFrameCallback(frame);resolve(value);};
            const timeout = setTimeout(() => done(read()), 4500);
            const sample = () => {
              const value = read();
              if (value.left[2]>200 && value.left[0]<40) done(value);
              else frame = v.requestVideoFrameCallback(sample);
            };
            frame = v.requestVideoFrameCallback(sample);
          });
        }""")
        assert decoded["width"] == decoded["height"] == 720 and decoded["left"][2] > 200 and decoded["left"][0] < 40, decoded
        assert page.get_by_label("Destination folder").input_value() == folder
        page.get_by_role("button", name="Save recording", exact=True).click()
        expect(page.locator("#recordStatus")).to_have_text("Recording saved to the library.", timeout=30000)
        link = page.get_by_role("link", name="Open saved video")
        expect(link).to_be_visible()
        uid = link.get_attribute("href").rsplit("/", 1)[-1]
        listed = requests.get(base + "/api/library", headers=auth, timeout=12)
        listed.raise_for_status()
        work = next(row for row in listed.json() if row["id"] == uid)
        assert work["folder"] == folder and work["title"] == title
        assert requests.get(f"{base}/watch/work/{uid}", timeout=10).status_code == 404
        metadata = requests.get(f"{base}/api/library/work/{uid}/details", headers=auth, timeout=12)
        metadata.raise_for_status()
        media = metadata.json()["media"]
        assert media["width"] == media["height"] == 720 and media["audio"]
        link.click()
        preview = page.locator("video#workspacePreview")
        expect(preview).to_be_visible()
        assert preview.evaluate("""async v=>{
          v.muted=true;await v.play();
          return v.videoWidth===720 && v.videoHeight===720 && v.duration>=1.8;
        }""")
        timeline = page.get_by_role("link", name="Timeline editor").get_attribute("href")
        context.close()
        context = browser.new_context(permissions=["camera", "microphone"])
        context.add_init_script("(" + DISPLAY_FIXTURE + ")()")
        page = context.new_page()
        page.goto(base + "/operator/library")
        page.evaluate("(key) => sessionStorage.setItem('cueporchOperator',key)", auth["Authorization"][7:])
        page.goto(base + timeline)
        clips = page.locator("#timelineClips > li")
        expect(clips).to_have_count(1)
        page.get_by_label("Upload a clip").set_input_files({"name": "Green slate.mp4", "mimeType": "video/mp4",
                                                           "buffer": clip_file.read_bytes()})
        page.get_by_role("button", name="Upload and add clip").click()
        expect(clips.nth(1)).to_contain_text("Green slate", timeout=30000)
        page.get_by_role("button", name="Record a clip").click()
        frame = page.frame_locator("#timelineRecorder")
        frame.get_by_label("Recording title").fill("Recorded inside the editor")
        expect(frame.get_by_label("Destination folder")).to_have_value(folder)
        frame.get_by_role("button", name="Share screen", exact=True).click()
        expect(frame.locator("#recordStatus")).to_have_text("Screen sharing is ready.")
        # The stand-alone path already uses the browser's fake devices. Exercise the same-origin
        # capture policy here, then record a deterministic window with the shared display fixture.
        assert frame.locator("body").evaluate("node => ['camera', 'microphone', 'display-capture']"
                                              ".every(name => node.ownerDocument.featurePolicy.allowsFeature(name))")
        frame.get_by_role("button", name="Start recording").click()
        expect(frame.locator("#recordTime")).to_have_text("0:01", timeout=5000)
        frame.get_by_role("button", name="Stop", exact=True).click()
        frame.get_by_role("button", name="Add recording to timeline").click()
        expect(clips.nth(2)).to_contain_text("Recorded inside the editor", timeout=30000)
        expect(page.get_by_role("dialog", name="Record a clip")).to_be_hidden()
        expect(page.locator("#timelineStatus")).to_have_text("Draft saved.")
        page.reload()
        expect(clips).to_have_count(3)
        project = requests.get(f"{base}/api/library/work/{uid}/timeline", headers=auth, timeout=12)
        project.raise_for_status()
        entries = project.json()["settings"]["clips"]
        assert entries[0]["work"] == uid
        for entry in entries[1:]:
            assert requests.get(f"{base}/watch/work/{entry['work']}", timeout=10).status_code == 404
        clips.nth(2).get_by_role("button", name="Recorded inside the editor").click()
        source = page.get_by_label("Timeline source video")
        expect(source).to_be_visible()
        assert source.evaluate("""async v=>{
          v.muted=true;await v.play();return v.videoWidth===1280 && v.videoHeight===720;
        }""")
        page.get_by_role("button", name="Render as a new version").click()
        expect(page.locator("#timelineStatus")).to_have_text("New version saved.", timeout=90000)
        versions = requests.get(f"{base}/api/library/work/{uid}/versions", headers=auth, timeout=12)
        versions.raise_for_status()
        assert len(versions.json()) == 2
        page.goto(base + "/operator/record")
        expect(page.get_by_role("button", name="Recover " + title, exact=True)).to_have_count(0)
        recorded_sizes = {}
        for quality, width in ((1080, 1920), (1440, 2560), (2160, 3840)):
            page.get_by_label("Recording title").fill(f"Studio {quality}p format rehearsal")
            page.get_by_role("combobox", name="Quality").select_option(str(quality))
            page.get_by_role("button", name="Share screen", exact=True).click()
            expect(page.locator("#recordStatus")).to_have_text("Screen sharing is ready.")
            page.wait_for_function("""() => {
              const canvas = document.querySelector('#recordCanvas');
              return canvas.getContext('2d').getImageData(160,160,1,1).data[2] > 235;
            }""")
            page.get_by_role("button", name="Start recording").click()
            expect(page.locator("#recordStatus")).to_have_text("Recording.")
            page.wait_for_function("""() => {
              const [minutes, seconds] = document.querySelector('#recordTime').textContent.split(':').map(Number);
              return minutes * 60 + seconds >= 3;
            }""", timeout=15000)
            page.get_by_role("button", name="Stop", exact=True).click()
            expect(page.locator("#recordStatus"), f"{quality}p capture").to_contain_text("Recording ready.", timeout=20000)
            facts = page.get_by_label("Finished recording").evaluate("""async v => {
              v.muted = true;
              return new Promise((resolve, reject) => {
                const timeout = setTimeout(() => {
                  v.cancelVideoFrameCallback(id);
                  reject(new Error('No recording frame decoded within 10 seconds.'));
                }, 10000);
                const id = v.requestVideoFrameCallback((now, frame) => {
                  clearTimeout(timeout);
                  const canvas = document.createElement('canvas');
                  canvas.width = canvas.height = 1;
                  const context = canvas.getContext('2d');
                  context.drawImage(v, 160, 160, 1, 1, 0, 0, 1, 1);
                  v.pause();
                  resolve({width: frame.width, height: frame.height,
                           pixel: [...context.getImageData(0,0,1,1).data]});
                });
                v.play().catch(reject);
              });
            }""")
            assert (facts["width"], facts["height"]) == (width, quality), facts
            assert facts["pixel"][2] > 200 and facts["pixel"][0] < 40, facts
            recorded_sizes[str(quality)] = [facts["width"], facts["height"]]
    finally:
        browser.close()
    return dict(browser_mediarecorder=True, fake_camera_and_microphone=True, simulated_screen_capture_source=True,
                side_by_side_canvas_720p=True, pause_and_resume=True, recovered_browser_draft=True,
                saved_to_selected_folder=True, private_before_share=True, installed_library_playback_and_audio=True,
                editor_clip_upload_and_inline_capture=True, private_clip_sources=True, recovered_three_clip_editor_draft=True,
                inline_recording_decoded_and_project_rendered=True, decoded_recording_canvas_sizes=recorded_sizes,
                teleprompter_manual_speed_size_pause_reset_and_screen_suppression=True)
