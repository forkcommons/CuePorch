"""Verify chapter editing and playback on the installed app."""

import json
import uuid

import requests
from playwright.sync_api import expect

from installed_versions import read, wait_delivery


def check_chapters(page, browser, base, auth, source, museum, venue, replacement, station):
    before = requests.get(f"{base}/api/library/work/{station}/chapters", headers=auth, timeout=10)
    before.raise_for_status()
    assert before.json()["chapters"] == [{"title": "Opening", "timecode": 0}]
    original = requests.get(source + "/media/71.mp4", timeout=15)
    original.raise_for_status()
    uploaded = requests.post(
        base + "/api/library/uploads", headers=auth, timeout=30,
        files={"file": ("Chapters fixture.mp4", original.content, "video/mp4")},
        data={"upload_id": str(uuid.uuid4()), "privacy": json.dumps({
            "visibility": "public", "embed": "anywhere", "domains": [], "download": False,
        })},
    )
    uploaded.raise_for_status()
    uid = uploaded.json()["id"]
    endpoint = f"{base}/api/library/work/{uid}"
    initial = requests.get(endpoint + "/chapters", headers=auth, timeout=10).json()
    assert requests.get(endpoint + "/chapters", timeout=10).status_code == 403

    page.goto(f"{base}/operator/videos/{uid}/interactivity")
    preview = page.get_by_label("Video preview")
    expect(preview).to_be_visible()
    page.get_by_role("button", name="Add chapter").click()
    page.get_by_role("textbox", name="Title", exact=True).fill("Welcome")
    page.get_by_role("button", name="Add chapter").click()
    page.get_by_role("textbox", name="Title", exact=True).last.fill("Public comment")
    start = page.get_by_label("Start time").last
    start.fill("00:00:02")
    expect(page.get_by_role("button", name="Preview time").last).to_be_visible()
    page.get_by_role("button", name="Preview time").last.click()
    page.wait_for_function("document.querySelector('video').currentTime >= 1.9")
    page.get_by_role("button", name="Save chapters").click()
    expect(page.locator("#chapterStatus")).to_have_text("Chapters saved.")
    saved = requests.get(endpoint + "/chapters", headers=auth, timeout=10).json()
    assert saved["version"] == initial["version"]
    assert [entry["timecode"] for entry in saved["chapters"]] == [0, 2]
    assert requests.put(endpoint + "/chapters", headers=auth, json=initial, timeout=10).status_code == 409
    invalid = dict(saved, chapters=[{"title": "After the video", "timecode": 300}])
    assert requests.put(endpoint + "/chapters", headers=auth, json=invalid, timeout=10).status_code == 422

    previous_embed = museum.embed
    guest = browser.new_context()
    try:
        watch = guest.new_page()
        watch.goto(base + "/watch/work/" + uid)
        menu = watch.get_by_role("combobox", name="Chapters", exact=True)
        expect(menu).to_be_visible()
        menu.select_option(label="Public comment")
        video = watch.locator("video")
        width = video.evaluate("async v => {v.muted=true;await v.play();return v.videoWidth}")
        assert width == 320
        assert video.evaluate("v => v.currentTime") >= 1.9
        museum.embed = base + "/embed/work/" + uid
        watch.goto(venue)
        frame = watch.frame_locator("iframe")
        expect(frame.get_by_role("combobox", name="Chapters", exact=True)).to_be_visible()
        hidden = requests.put(endpoint + "/appearance/embed", headers=auth, timeout=10, json={"chapters": False})
        hidden.raise_for_status()
        watch.reload()
        expect(frame.locator("video")).to_be_visible()
        expect(frame.get_by_role("combobox", name="Chapters", exact=True)).to_have_count(0)

        with replacement.open("rb") as stream:
            changed = requests.post(
                endpoint + "/versions", headers=auth, timeout=40, data={"current": saved["version"]},
                files={"video": ("next-cut.mp4", stream, "video/mp4")},
            )
        changed.raise_for_status()
        other = requests.get(endpoint + "/chapters", headers=auth, timeout=10).json()
        assert other["version"] == changed.json()["current"] and other["chapters"] == []
        assert requests.put(endpoint + "/chapters", headers=auth, json=saved, timeout=10).status_code == 409
        restored = requests.post(endpoint + "/versions/restore", headers=auth, timeout=20,
                                 json={"current": other["version"], "restore": saved["version"]})
        restored.raise_for_status()
        watch.goto(base + "/watch/work/" + uid)
        expect(watch.get_by_role("combobox", name="Chapters").locator("option")).to_have_count(3)
        assert requests.get(endpoint + "/chapters", headers=auth, timeout=10).json() == saved
    finally:
        museum.embed = previous_embed
        guest.close()
    return dict(migrated_chapters=True, preview_and_viewer_seek=True, decoded_width=width,
                independent_embed_visibility=True, retained_after_version_restore=True,
                concurrent_and_out_of_range_edits_rejected=True)


def check_hosted_chapters(base, origin, keys):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    job = next(item for item in read(base + "/api/delivery", auth)
               if item["want_public"] and item["status"] == "playable" and item["kind"] == "work")
    uid, remote = job["work"], job["remote"]
    endpoint = f"{base}/api/library/work/{uid}/chapters"
    peer = f"{origin.rstrip('/')}/api/v1/videos/{remote}"
    before = read(endpoint, auth)
    assert read(peer + "/chapters", {})["chapters"][0]["title"] == "Opening"
    new = [{"title": "Welcome", "timecode": 0}, {"title": "Public comment", "timecode": 2}]
    edited = requests.put(endpoint, headers=auth, timeout=20, json=dict(before, chapters=new))
    edited.raise_for_status()
    delivery = wait_delivery(base, auth, uid, job["id"], remote)
    assert read(peer + "/chapters", {})["chapters"] == new
    removed = requests.put(endpoint, headers=auth, timeout=20, json=dict(edited.json(), chapters=[]))
    removed.raise_for_status()
    wait_delivery(base, auth, uid, delivery, remote)
    assert read(peer + "/chapters", {})["chapters"] == []
    assert read(peer, {})["uuid"] == remote and read(endpoint, auth)["version"] == before["version"]
    return dict(edited_and_removed_on_peertube=True, same_hosted_id_and_retained_cut=True)
