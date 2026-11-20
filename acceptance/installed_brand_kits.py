"""Apply a shared logo, colors and retained bookends through product entry points."""

import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import uuid

from PIL import Image
from playwright.sync_api import expect
import requests


def check_brand_kits(page, browser, base, auth, directory, fixture):
    private = {"visibility": "private", "embed": "disabled", "domains": [], "download": False}
    public = private | {"visibility": "public", "embed": "anywhere"}

    def upload(path, title, privacy):
        with path.open("rb") as source:
            response = requests.post(base + "/api/library/uploads", headers=auth, timeout=30,
                                     data={"privacy": json.dumps(privacy), "upload_id": uuid.uuid4().hex},
                                     files={"file": (title + ".mp4", source, "video/mp4")})
        response.raise_for_status()
        return response.json()["id"]

    def color_clip(color):
        path = fixture.with_name(color + "-brand.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color={color}:s=320x180:r=24:d=1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
        return path

    intro = upload(fixture, "Opening for public screenings", private)
    main = upload(color_clip("red"), "Community screening main", public)
    outro = upload(color_clip("blue"), "Closing for public screenings", private)
    url = base + "/api/library/brand-kits"
    work = base + "/api/library/work/" + main
    assert requests.get(url, timeout=10).status_code == 403
    stamp = BytesIO()
    Image.new("RGB", (32, 32), (104, 36, 147)).save(stamp, "PNG")
    logo_bytes = stamp.getvalue()
    logo_hash = hashlib.sha256(logo_bytes).hexdigest()

    def get(path):
        reply = requests.get(path, headers=auth, timeout=10)
        reply.raise_for_status()
        return reply.json()

    page.goto(base + "/operator/brand-kits")
    page.get_by_role("button", name="New brand kit").click()
    page.get_by_label("Kit name").fill("Community screens")
    page.get_by_label("Primary color").fill("#507f11")
    page.get_by_label("Background", exact=True).fill("#221143")
    page.get_by_label("Title font").select_option("mono")
    page.get_by_label("Intro video").select_option(intro)
    page.get_by_label("Outro video").select_option(outro)
    page.get_by_label("Logo image").set_input_files({"name": "screens.png", "mimeType": "image/png", "buffer": logo_bytes})
    page.get_by_role("button", name="Save brand kit").click()
    expect(page.locator("#brandKitStatus")).to_have_text("Brand kit saved.")
    [kit] = get(url)["kits"]
    assert kit["intro"]["work"] == intro and kit["outro"]["work"] == outro
    assert kit["style"]["logo"] == logo_hash
    assert requests.get(base + "/watch/work/" + intro, timeout=10).status_code == 404

    page.goto(f"{base}/operator/videos/{main}/appearance")
    page.get_by_label("Progress", exact=True).uncheck()
    page.get_by_label("Reusable brand kit").select_option(kit["id"])
    page.get_by_role("button", name="Use brand kit").click()
    expect(page.get_by_label("Progress", exact=True)).not_to_be_checked()
    expect(page.get_by_label("Title font")).to_have_value("mono")
    expect(page.get_by_alt_text("Publisher logo")).to_be_visible()
    page.get_by_role("button", name="Save appearance").click()
    expect(page.locator("#appearanceMessage")).to_have_text("Appearance saved.")
    view_settings = get(work + "/appearance/watch")["settings"]
    assert view_settings["background"] == "#221143" and view_settings["logo"] == logo_hash
    assert not view_settings["progress"]

    page.goto(f"{base}/operator/videos/{main}/timeline")
    page.get_by_label("Reusable brand kit").select_option(kit["id"])
    page.get_by_role("button", name="Use brand kit").click()
    expect(page.locator("#timelineStatus")).to_have_text("Draft saved.")
    page.get_by_role("button", name="Use brand kit").click()
    expect(page.locator("#timelineStatus")).to_have_text("Draft saved.")
    page.reload()
    expect(page.locator("#timelineBrandSources")).to_contain_text("Intro: Opening for public screenings")
    expect(page.locator("#timelineBrandSources")).to_contain_text("Outro: Closing for public screenings")
    expect(page.locator("#timelineDuration")).to_have_text("3 seconds")
    assert get(work + "/timeline")["settings"]["brand"]["revision"] == kit["revision"]
    saved = requests.put(url + "/" + kit["id"], headers=auth, timeout=10, json={
        "name": "Community screens revised", "intro": intro, "outro": outro, "revision": kit["revision"],
        "style": kit["style"] | {"background": "#113322"},
    })
    saved.raise_for_status()
    assert requests.delete(url + "/" + kit["id"], headers=auth, timeout=10,
                           json={"revision": kit["revision"]}).status_code == 409
    assert requests.delete(url + "/" + kit["id"], headers=auth, timeout=10,
                           json={"revision": saved.json()["revision"]}).status_code == 204
    assert not get(url)["kits"]
    page.reload()
    expect(page.locator("#timelineBrandSources")).to_contain_text("Community screens")
    expect(page.get_by_label("Background color")).to_have_value("#221143")
    assert get(work + "/appearance/watch")["settings"] == view_settings

    page.get_by_role("button", name="Render as a new version").click()
    expect(page.locator("#timelineStatus")).to_have_text("New version saved.", timeout=90000)
    with browser.new_context() as guest:
        viewer = guest.new_page()
        viewer.goto(base + "/watch/work/" + main)
        expect(viewer.locator("body")).to_have_attribute("data-font", "mono")
        expect(viewer.get_by_alt_text("Publisher logo")).to_be_visible()
        assert viewer.get_by_alt_text("Publisher logo").evaluate("image => image.naturalWidth") == 32
        samples = viewer.locator("video").evaluate("""async video => {
          video.muted = true; await video.play(); video.pause();
          const canvas = document.createElement("canvas");
          canvas.width = canvas.height = 1;
          const paint = canvas.getContext("2d");
          const result = [];
          for (const time of [.25, 1.25, 2.25]) {
            const frame = new Promise(resolve => video.addEventListener("seeked", resolve, {once: true}));
            video.currentTime = time;
            await frame;
            paint.drawImage(video, video.videoWidth / 2, video.videoHeight / 2, 1, 1, 0, 0, 1, 1);
            result.push(Array.from(paint.getImageData(0, 0, 1, 1).data).slice(0, 3));
          }
          return {duration: video.duration, result};
        }""")
    assert 2.95 <= samples["duration"] <= 3.05, samples
    for expected, rgb in zip((1, 0, 2), samples["result"], strict=True):
        assert all(value >= 190 if channel == expected else value < 75 for channel, value in enumerate(rgb)), rgb

    export = fixture.with_name("brand-kit-export")
    exporter = Path(__file__).resolve().parents[1] / "export_institution.py"
    subprocess.run([sys.executable, str(exporter), str(directory / "collection"),
                    str(directory / "institution.sqlite"), str(export)], check=True)
    rights = json.loads((export / "rights-and-routes.json").read_text(encoding="utf8"))
    portable = next(row["settings"] for row in rights["team_brand_kit_revision"] if row["revision"] == kit["revision"])
    assert portable["intro"] == kit["intro"] and portable["outro"] == kit["outro"]
    assert (export / portable["style"]["artwork"]["path"]).read_bytes() == logo_bytes
    return dict(reused_player_palette_font_and_logo=True, original_private_asset_stayed_private=True,
                render_pixels_intro_program_outro=samples["result"], versioned_kit_survived_update_and_removal=True,
                portable_brand_kit_artwork_verified=True)
