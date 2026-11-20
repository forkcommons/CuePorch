"""Decode synthetic panoramic masters through the authorized player."""

import hashlib
import json
import subprocess
import uuid
from urllib.parse import urljoin, urlsplit

import requests
from playwright.sync_api import expect


def painted_master(path):
    painting = ",".join([
        "drawbox=x=0:y=0:w=144:h=192:color=red:t=fill",
        "drawbox=x=144:y=0:w=96:h=192:color=lime:t=fill",
        "drawbox=x=240:y=0:w=144:h=192:color=blue:t=fill",
        "drawbox=x=0:y=0:w=384:h=64:color=yellow:t=fill",
        "drawbox=x=0:y=128:w=384:h=64:color=magenta:t=fill",
    ])
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=384x192:r=12:d=16",
                    "-vf", painting, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(path)], check=True)


def detect_color(canvas, target):
    def is_color(rgb):
        channels = ((0, 180), (1, 180), (2, 180))
        literal = {"red": {0}, "green": {1}, "blue": {2}, "yellow": {0, 1}}[target]
        return all(rgb[index] > limit if index in literal else rgb[index] < 75
                   for index, limit in channels)

    for _ in range(5):
        image = canvas.screenshot(animations="disabled")
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", "pipe:0", "-vf", "crop=8:8,scale=1:1",
             "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
            input=image, capture_output=True, check=True,
        )
        rgb = tuple(result.stdout)
        if len(rgb) == 3 and is_color(rgb):
            return rgb
    raise AssertionError(f"Panoramic viewport center was {rgb}, expected {target}.")


def check_panorama(page, browser, base, auth, museum, venue, sample):
    title = "Panoramic test patterns"
    master = sample.with_name("panoramic-test.mp4")
    painted_master(master)
    privacy = dict(visibility="public", embed="domains", domains=[urlsplit(venue).netloc], download=False)
    with master.open("rb") as stream:
        response = requests.post(
            base + "/api/library/uploads", headers=auth, timeout=40,
            files={"file": (title + ".mp4", stream, "video/mp4")},
            data=dict(upload_id=uuid.uuid4().hex, privacy=json.dumps(privacy)),
        )
    response.raise_for_status()
    uid = response.json()["id"]
    work = f"{base}/api/library/work/{uid}"
    details = work + "/details"
    watch = f"{base}/watch/work/{uid}"
    embed = f"{base}/embed/work/{uid}"
    original = hashlib.sha256(master.read_bytes()).hexdigest()

    def get(path):
        response = requests.get(path, headers=auth, timeout=10)
        response.raise_for_status()
        return response.json()

    def set_mode(mode):
        response = requests.put(details, headers=auth, timeout=10, json=get(details) | {"panorama": mode})
        response.raise_for_status()

    assert get(details)["panorama"] == "flat"
    assert requests.put(details, headers=auth, timeout=10, json=get(details) | {"panorama": []}).status_code == 400
    assert requests.put(details, timeout=10, json=get(details) | {"panorama": "360-mono"}).status_code == 403
    captions = work + "/languages"
    response = requests.post(
        captions, headers=auth, timeout=10, data=dict(language="en", kind="captions", version=get(captions)["version"]),
        files={"track": ("test.vtt", b"WEBVTT\n\n00:00:00.100 --> 00:00:15.000\nPanorama caption.\n", "text/vtt")},
    )
    response.raise_for_status()
    page.goto(f"{base}/operator/videos/{uid}")
    selection = page.get_by_label("Playback projection")
    selection.select_option("360-mono")
    page.get_by_role("button", name="Save video details").click()
    expect(page.locator("#detailsStatus")).to_have_text("Video details saved.")
    page.reload()
    expect(selection).to_have_value("360-mono")
    assert get(details)["panorama"] == "360-mono"

    def player(target):
        canvas = target.locator("canvas[aria-label='Panoramic view']")
        expect(canvas).to_be_visible()
        target.get_by_role("button", name="Play", exact=True).click()
        video = target.locator("video")
        assert video.evaluate("async video => { await video.play(); return video.videoWidth; }") == 384
        return canvas

    with browser.new_context() as guest:
        viewer = guest.new_page()
        viewer.goto(watch)
        canvas = player(viewer)
        center = detect_color(canvas, "green")
        for _ in range(12):
            canvas.press("ArrowRight")
        turned = detect_color(canvas, "blue")
        viewer.get_by_role("button", name="Reset view").click()
        detect_color(canvas, "green")
        controls = viewer.get_by_role("combobox", name="Captions", exact=True)
        controls.select_option("0")
        expect(viewer.locator("[data-panorama-captions]")).to_have_text("Panorama caption.")
        expect(viewer.locator("[data-panorama-captions]")).to_be_visible()
        viewer.get_by_role("button", name="Watch flat").click()
        expect(canvas).to_be_hidden()
        expect(viewer.locator("[data-panorama-captions]")).to_be_hidden()
        expect(viewer.get_by_role("button", name="Pause", exact=True)).to_be_visible()
        viewer.get_by_role("button", name="Explore panorama").click()
        detect_color(canvas, "green")
        bounds = canvas.bounding_box()
        assert bounds
        cursor_y = bounds["y"] + bounds["height"] * 0.3
        cursor_x = bounds["x"] + bounds["width"] * 0.85
        viewer.mouse.move(cursor_x, cursor_y)
        viewer.mouse.down()
        viewer.mouse.move(cursor_x - 255, cursor_y, steps=6)
        viewer.mouse.up()
        detect_color(canvas, "blue")

        for mode, color in (("360-top-bottom", "yellow"), ("360-side-by-side", "red"), ("180-mono", "green")):
            set_mode(mode)
            viewer.reload()
            canvas = player(viewer)
            detect_color(canvas, color)
        for _ in range(5):
            canvas.press("ArrowRight")
        detect_color(canvas, "blue")

        frame_request = {"Sec-Fetch-Dest": "iframe"}
        assert requests.get(embed, headers=frame_request | {"Referer": "https://elsewhere.example.test/"},
                            timeout=10).status_code == 403
        museum.embed = embed
        viewer.goto(venue)
        frame = viewer.frame_locator("iframe")
        canvas = player(frame)
        detect_color(canvas, "green")
        fetched = urljoin(base, frame.locator("video").get_attribute("src"))
        assert requests.head(fetched, timeout=10).ok
        assert requests.put(work + "/privacy", headers=auth, timeout=10,
                            json=privacy | {"visibility": "private"}).ok
        assert requests.head(fetched, timeout=10).status_code == 404
        assert requests.get(watch, timeout=10).status_code == 404
        assert requests.get(embed, headers=frame_request | {"Referer": venue}, timeout=10).status_code == 404
        assert requests.put(work + "/privacy", headers=auth, timeout=10, json=privacy).ok

    with browser.new_context() as fallback:
        fallback.add_init_script("""const original = HTMLCanvasElement.prototype.getContext;
          HTMLCanvasElement.prototype.getContext = function (name, ...args) {
            return name === 'webgl' ? null : original.call(this, name, ...args);
          };""")
        viewer = fallback.new_page()
        viewer.goto(watch)
        expect(viewer.locator("[data-panorama-controls] [role=status]")).to_contain_text("Flat playback is available.")
        viewer.get_by_role("button", name="Play", exact=True).click()
        assert viewer.locator("video").evaluate("async v => { await v.play(); return v.videoWidth; }") == 384
    assert next(file["sha256"] for file in get(work)["files"] if file["role"] == "original") == original
    return dict(center_rgb=center, after_keyboard_pan_rgb=turned, pointer_pan=True, hemisphere_and_both_stereo_packings=True,
                fallback_decoded_width=384, captions_during_panorama=True, authorized_embed_and_revocation=True,
                original_sha256=original)
