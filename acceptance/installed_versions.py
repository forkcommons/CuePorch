"""Check exact-cut continuity plus same-link hosted version changes."""

import hashlib
import subprocess
import time

import requests
from browser import chromium
from installed_cards import select_action
from playwright.sync_api import expect, sync_playwright


def generate_replacement(path):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=256x144:r=24:d=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
    )
    return path


def replace(page, title, file, uid=None):
    select_action(page, title, "Replace", uid)
    page.get_by_label("Replacement video file").set_input_files(file)
    page.get_by_role("button", name="Save as new version").click()
    expect(page.locator("#result")).to_have_text("New version saved.", timeout=40000)


def restore(page, title, uid=None):
    select_action(page, title, "Replace", uid)
    page.get_by_role("button", name="Restore previous version 1", exact=True).click()
    expect(page.locator("#result")).to_have_text("Previous version restored.")


def read(url, auth):
    response = requests.get(url, headers=auth, timeout=10)
    response.raise_for_status()
    return response.json()


def check_versions(page, base, auth, work, grant, venue, embed, file):
    title = work["title"]
    endpoint = base + "/api/library/work/" + work["work"] + "/versions"
    assert requests.get(endpoint, timeout=10).status_code == 403
    before = read(endpoint, auth)[0]
    folders = read(base + "/api/library/folders", auth)
    folder = next(item["id"] for item in folders if item["name"] == "November meetings")
    requests.put(
        base + "/api/library/placement",
        headers=auth,
        timeout=10,
        json={"folder": folder, "works": [work["work"]]},
    ).raise_for_status()
    page.goto(base + "/operator/library")
    page.get_by_role("button", name="Load library").click()
    page.get_by_role("navigation", name="Folders").get_by_role(
        "button", name="November meetings", exact=True
    ).click()
    replace(page, title, file, work["work"])
    revised = read(endpoint, auth)
    assert revised[0]["current"] and revised[0]["key"] != before["key"]
    assert revised[1]["key"] == before["key"]
    assert {f["role"] for f in revised[0]["video_files"]} == {"original", "playback"}
    library = next(
        w for w in read(base + "/api/library", auth) if w["id"] == work["work"]
    )
    assert library["modified_at"] >= revised[0]["saved_at"]
    assert (
        requests.post(
            endpoint + "/restore",
            headers=auth,
            json={"current": before["key"], "restore": before["key"]},
            timeout=10,
        ).status_code
        == 409
    )
    assert any(
        row["id"] == grant["id"] and row["version"] == work["version"]
        for row in read(base + "/api/grants", auth)
    )
    stream = requests.get(embed + "/media", headers={"Referer": embed}, timeout=10)
    stream.raise_for_status()
    prior_bytes = next(
        f["bytes"] for f in before["video_files"] if f["sha256"] == work["version"]
    )
    assert len(stream.content) == prior_bytes
    assert hashlib.sha256(stream.content).hexdigest() == work["version"]
    page.goto(venue)
    original_width = (
        page.frame_locator("iframe")
        .locator("video")
        .evaluate("async v => {v.muted=true;await v.play();return v.videoWidth}")
    )
    assert original_width == 320
    page.goto(base + "/operator/library")
    page.get_by_role("button", name="Load library").click()
    page.get_by_role("navigation", name="Folders").get_by_role(
        "button", name="November meetings", exact=True
    ).click()
    restore(page, title, work["work"])
    restored = read(endpoint, auth)
    assert restored[0]["key"] == before["key"]
    assert restored[1]["key"] == revised[0]["key"]
    return {
        "replacement_and_restore_kept_both_cuts": True,
        "stale_version_write_rejected": True,
        "existing_venue_grant_played_original_width": original_width,
    }


def wait_delivery(base, auth, uid, previous, remote):
    deadline = time.monotonic() + 130
    while time.monotonic() < deadline:
        job = next(
            job for job in read(base + "/api/delivery", auth) if job["work"] == uid
        )
        if job["id"] != previous:
            if job["status"] == "failed":
                raise AssertionError(f"Hosted video replacement failed: {job['error']}")
            if job["status"] == "playable":
                assert job["remote"] == remote
                return job["id"]
        time.sleep(0.4)
    raise AssertionError("Hosted video version did not become playable")


def check_hosted_versions(base, origin, keys, file):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    first = next(
        job
        for job in read(base + "/api/delivery", auth)
        if job["want_public"] and job["status"] == "playable"
    )
    work, remote = first["work"], first["remote"]
    route = "/" + first["kind"] + "/" + work
    watch = base + "/watch" + route
    host = origin + "/api/v1/videos/" + remote
    title = "Station meeting replay"
    destination = requests.get(
        base + "/embed" + route, allow_redirects=False, timeout=10
    ).headers["Location"]
    original_duration = read(host, {})["duration"]
    assert original_duration == 4
    assert read(host + "/captions", {})["data"]
    assert read(host + "/chapters", {})["chapters"][0]["title"] == "Opening"
    with sync_playwright() as pw:
        browser = chromium(pw)
        page = browser.new_page()
        page.goto(base + "/operator/library")
        page.get_by_label("Operator credential").fill(keys["operator"])
        page.get_by_role("button", name="Load library").click()
        replace(page, title, file)
        new_job = wait_delivery(base, auth, work, first["id"], remote)
        assert read(host, {})["duration"] == 2
        assert not read(host + "/captions", {})["data"]
        assert not read(host + "/chapters", {})["chapters"]
        page.goto(watch)
        expect(page.locator("iframe")).to_have_attribute("src", destination)
        frame = page.frame_locator("iframe")
        frame.get_by_role("button", name="Play Video", exact=True).click(timeout=45000)
        updated = frame.locator("video").first.evaluate(
            "async v => {v.muted=true;await v.play();return v.videoWidth}"
        )
        assert updated == 256
        page.goto(base + "/operator/library")
        page.get_by_label("Operator credential").fill(keys["operator"])
        page.get_by_role("button", name="Load library").click()
        restore(page, title)
        wait_delivery(base, auth, work, new_job, remote)
        assert read(host, {})["duration"] == original_duration
        assert read(host + "/captions", {})["data"]
        assert read(host + "/chapters", {})["chapters"][0]["title"] == "Opening"
        page.goto(watch)
        expect(page.locator("iframe")).to_have_attribute("src", destination)
        frame = page.frame_locator("iframe")
        frame.get_by_role("button", name="Play Video", exact=True).click(timeout=45000)
        restored = frame.locator("video").first.evaluate(
            "async v => {v.muted=true;await v.play();return v.videoWidth}"
        )
        assert restored == 320
        browser.close()
    return {
        "same_peertube_video_id_and_cueporch_watch_url": True,
        "replacement_playback_width": updated,
        "restored_playback_width": restored,
        "timed_captions_restored_with_original_version": True,
        "timed_chapters_restored_with_original_version": True,
    }
