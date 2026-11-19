"""Record simulated devices through browser MediaRecorder and open the stored video."""

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


def check_recording(playwright, base, auth):
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
        page.get_by_text("Teleprompter", exact=True).click()
        page.get_by_label("Script", exact=True).fill("Library access works at the speed of trust.")
        expect(page.locator("#recordPrompter")).to_contain_text("Library access")
        page.get_by_role("button", name="Connect camera and microphone").click()
        expect(page.locator("#recordStatus")).to_have_text("Recording devices connected.")
        page.get_by_role("button", name="Share screen", exact=True).click()
        expect(page.locator("#recordStatus")).to_have_text("Screen sharing is ready.")
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
        assert video.evaluate("""async v=>{
          v.muted=true;await v.play();
          const c=document.createElement('canvas'); c.width=720;c.height=720;
          const ctx=c.getContext('2d');ctx.drawImage(v,0,0);
          const left=ctx.getImageData(180,360,1,1).data;
          return v.videoWidth===720 && left[2]>200 && left[0]<40;
        }""")
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
        page.goto(base + "/operator/record")
        expect(page.get_by_role("button", name="Recover " + title, exact=True)).to_have_count(0)
    finally:
        browser.close()
    return dict(browser_mediarecorder=True, fake_camera_and_microphone=True, simulated_screen_capture_source=True,
                side_by_side_canvas_720p=True, pause_and_resume=True, recovered_browser_draft=True,
                saved_to_selected_folder=True, private_before_share=True, installed_library_playback_and_audio=True)
