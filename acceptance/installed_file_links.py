"""Play revocable progressive and adaptive file links apart from CuePorch's player."""

import json
import subprocess
import uuid
from urllib.parse import urljoin

import requests
from playwright.sync_api import expect


def file_link_api(base, uid):
    return f"{base}/api/library/work/{uid}/file-links"


def prepare_links(page, base, uid, auth):
    path = f"/operator/videos/{uid}"
    page.goto(base + path)
    page.get_by_text("File actions", exact=True).click()
    page.get_by_role("link", name="Video file links", exact=True).click()
    page.get_by_role("button", name="Prepare file links").click()
    page.wait_for_function("""() => document.querySelector("#fileLinkStatus").textContent === "Video file links ready."
      || document.querySelector("#prepareFileLinks").textContent === "Retry file links" """, timeout=90000)
    expect(page.locator("#fileLinkStatus")).to_have_text("Video file links ready.", timeout=1000)
    response = requests.get(file_link_api(base, uid), headers=auth, timeout=10)
    response.raise_for_status()
    values = response.json()
    assert values["state"] == "ready"
    assert urljoin(base, values["hls"]) == page.get_by_role("link", name="HLS", exact=True).get_attribute("href")
    return values


def check_file_links(page, browser, base, auth, venue, root, public_link, private_work):
    public_id = public_link.rsplit("/", 1)[1]
    station = prepare_links(page, base, public_id, auth)
    item = station["files"][0]
    assert item["height"] == 180 and item["download"] is None
    assert requests.get(base + item["play"] + "?download=1", timeout=10).status_code == 404
    denied = file_link_api(base, private_work)
    private = requests.get(denied, headers=auth, timeout=10).json()
    assert private["state"] == "unavailable"
    assert requests.post(denied, headers=auth, json={"version": private["version"]}, timeout=10).status_code == 409

    source = root / "file-link-proof.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=7",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=7", "-threads", "2",
        "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(source),
    ], check=True, timeout=60)
    settings = dict(visibility="public", embed="anywhere", domains=[], download=True)
    with source.open("rb") as stream:
        upload = requests.post(base + "/api/library/uploads", headers=auth, timeout=45,
                               files={"file": ("Direct link proof.mp4", stream, "video/mp4")},
                               data=dict(upload_id=uuid.uuid4().hex, privacy=json.dumps(settings)))
    upload.raise_for_status()
    uid = upload.json()["id"]
    data = prepare_links(page, base, uid, auth)
    assert [(media["width"], media["height"]) for media in data["files"]] == [(640, 360), (426, 240)]
    assert requests.get(file_link_api(base, uid), timeout=10).status_code == 403
    first = data["files"][0]
    ranged = requests.get(base + first["play"], headers={"Range": "bytes=0-63", "Origin": venue}, timeout=10)
    assert ranged.status_code == 206 and len(ranged.content) == 64
    assert ranged.headers["Access-Control-Allow-Origin"] == "*"
    progress = requests.get(base + first["play"], timeout=10)
    download = requests.get(base + first["download"], timeout=10)
    assert progress.ok and download.ok and progress.content == download.content
    assert "attachment" in download.headers["Content-Disposition"]
    hls = base + data["hls"]
    dash = base + data["dash"]

    with browser.new_context() as independent:
        player = independent.new_page()
        player.goto(venue)
        player.add_script_tag(url=base + "/static/vendor/hls.min.js")
        player.evaluate("""(playlist) => {
          const video = document.createElement("video");
          video.muted = true;
          video.crossOrigin = "anonymous";
          document.body.replaceChildren(video);
          const client = new Hls({startLevel: 0});
          window.directHls = client;
          client.attachMedia(video);
          client.loadSource(playlist);
          client.on(Hls.Events.MANIFEST_PARSED, () => { void video.play().catch(() => {}); });
        }""", hls)
        player.wait_for_function("document.querySelector('video').videoHeight === 240")
        player.evaluate("() => { window.directHls.currentLevel = 1; document.querySelector('video').currentTime = 1; }")
        player.wait_for_function("document.querySelector('video').videoHeight === 360")
        player.evaluate("window.directHls.destroy()")

    pixels = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", dash, "-map", "0:v:0", "-frames:v", "1",
        "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
    ], timeout=35)
    assert len(pixels) == 640 * 360 * 3 and len(set(pixels)) > 20
    samples = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", dash, "-ss", "0.25", "-map", "0:a:0", "-t", "0.12",
        "-ac", "1", "-ar", "8000", "-f", "s16le", "-",
    ], timeout=35)
    pcm = memoryview(samples).cast("h")
    assert 1880 <= len(samples) <= 1952 and max(pcm) - min(pcm) > 100, len(samples)
    fragment = urljoin(hls, "chunk-stream0-00001.m4s")
    assert requests.get(fragment, timeout=10).ok
    page.get_by_role("button", name="Revoke file links").click()
    expect(page.locator("#fileLinkStatus")).to_have_text("Video file links revoked.")
    for address in (hls, dash, fragment, base + first["download"]):
        assert requests.get(address, timeout=10).status_code == 404

    replacement = prepare_links(page, base, uid, auth)
    assert replacement["id"] != data["id"]
    response = requests.put(f"{base}/api/library/work/{uid}/privacy", headers=auth, timeout=10,
                            json=settings | {"visibility": "private"})
    response.raise_for_status()
    assert requests.get(base + replacement["hls"], timeout=10).status_code == 404
    assert requests.get(urljoin(base + replacement["hls"], "init-stream0.m4s"), timeout=10).status_code == 404
    return dict(cross_origin_hls_heights=[240, 360], dash_rgb_bytes=len(pixels), dash_audio_bytes=len(samples),
                conditional_mp4_download=True, revoked_manifests_and_segments=True, stricter_privacy_enforced=True,
                restricted_work_cannot_create_direct_links=True, migrated_video_link=item["play"])
