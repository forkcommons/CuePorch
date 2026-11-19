"""Checks for withdrawal and timed recovery through the installed application."""

import sqlite3
import time

import requests
from browser import chromium
from playwright.sync_api import expect, sync_playwright

TRASH = "/api/library/recently-deleted"


def records(base, auth):
    response = requests.get(base + TRASH, headers=auth, timeout=10)
    response.raise_for_status()
    return response.json()


def wait_for(base, auth, predicate):
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        if predicate(records(base, auth)):
            return
        time.sleep(0.4)
    raise AssertionError("Recently deleted did not reach the expected state")


def check_private(page, base, auth, database, work, grant, existing_embed, download_url):
    folders = requests.get(
        base + "/api/library/folders", headers=auth, timeout=10
    ).json()
    child = next(row["id"] for row in folders if row["name"] == "November meetings")
    requests.put(
        base + "/api/library/placement",
        headers=auth,
        timeout=10,
        json=dict(folder=child, works=[work]),
    ).raise_for_status()
    count_in_folder = sum(
        row["folder"] == child
        for row in requests.get(base + "/api/library", headers=auth, timeout=10).json()
    )
    assert requests.head(download_url, timeout=10).status_code == 200
    page.goto(base + "/operator/library")
    page.get_by_role("button", name="Load library").click()
    tree = page.get_by_role("navigation", name="Folders")
    tree.get_by_role("button", name="Station programs", exact=True).click()
    page.get_by_role("button", name="Delete folder", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Recently deleted")
    assert requests.head(download_url, timeout=10).status_code == 404
    assert not any(
        item["id"] == work
        for item in requests.get(base + "/api/library", headers=auth).json()
    )
    grant_url = base + "/api/grants/" + grant["id"] + "/session"
    exhibitor = {"Authorization": "Bearer " + grant["exhibitor_secret"]}
    assert requests.post(grant_url, headers=exhibitor, timeout=10).status_code == 403
    assert (
        requests.get(
            existing_embed + "/media", headers={"Referer": existing_embed}, timeout=10
        ).status_code
        == 403
    )
    assert requests.get(base + TRASH, timeout=10).status_code == 403
    tree.get_by_role("button", name="Recently deleted").click()
    page.get_by_label("Deleted by").fill("different operator")
    expect(page.get_by_text("No recently deleted items match.")).to_be_visible()
    assert requests.head(download_url, timeout=10).status_code == 404
    page.get_by_label("Deleted by").fill("operator")
    page.get_by_label("Type").select_option("folder")
    restore = page.get_by_role("button", name="Restore Station programs", exact=True)
    restore.click()
    expect(page.get_by_text("No recently deleted items match.")).to_be_visible()
    tree.get_by_role("button", name="November meetings", exact=True).click()
    expect(page.locator("#items tr")).to_have_count(count_in_folder)
    restored = requests.post(grant_url, headers=exhibitor, timeout=10)
    restored.raise_for_status()
    media = base + restored.json()["embed"]
    decoded = requests.get(
        media + "/media", headers={"Referer": media, "Range": "bytes=0-127"}, timeout=10
    )
    assert decoded.status_code == 206 and len(decoded.content) == 128

    tree.get_by_role("button", name="Station programs", exact=True).click()
    page.get_by_role("button", name="Delete folder", exact=True).click()
    wait_for(base, auth, lambda data: len(data) == 1 and data[0]["state"] == "deleted")
    deletion = records(base, auth)[0]["id"]
    with sqlite3.connect(database) as con:
        con.execute(
            "UPDATE library_deletion SET deleted_at=? WHERE id=?",
            (time.time() - 31 * 86400, deletion),
        )
    con.close()
    assert not records(base, auth)
    assert (
        requests.post(
            base + TRASH + "/" + deletion + "/restore", headers=auth, timeout=10
        ).status_code
        == 410
    )
    assert requests.post(grant_url, headers=exhibitor, timeout=10).status_code == 403
    return {
        "nested_folder_restored": True,
        "screening_blocked_in_trash": True,
        "download_ticket_revoked_on_delete": True,
        "expiry_enforced": True,
    }


def check_hosted(base, origin, keys):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    public = next(
        job
        for job in requests.get(base + "/api/delivery", headers=auth, timeout=10).json()
        if job["want_public"] and job["status"] == "playable"
    )
    watch = base + "/watch/" + public["kind"] + "/" + public["work"]
    direct = origin + "/api/v1/videos/" + public["remote"]
    info = requests.get(direct, timeout=10)
    info.raise_for_status()
    playlist = info.json()["streamingPlaylists"][0]["playlistUrl"]
    assert requests.get(playlist, timeout=10).status_code == 200
    other_work = next(
        job
        for job in requests.get(base + "/api/delivery", headers=auth, timeout=10).json()
        if job["work"] != public["work"] and job["status"] == "playable"
    )
    protected = requests.put(
        base + "/api/routes/meeting/" + other_work["work"],
        headers=auth,
        timeout=10,
        json=dict(
            revision="protected",
            destination=origin + "/videos/embed/" + other_work["remote"],
            public_authorized=True,
            public_record=True,
        ),
    )
    protected.raise_for_status()
    rejected = requests.post(
        base + TRASH,
        headers=auth,
        timeout=10,
        json=dict(kind="work", items=[public["work"], other_work["work"]]),
    )
    assert rejected.status_code == 409
    assert requests.get(watch, timeout=10).status_code == 200
    assert records(base, auth) == []

    with sync_playwright() as pw:
        browser = chromium(pw)
        page = browser.new_page()
        page.goto(base + "/operator/library")
        page.get_by_label("Operator credential").fill(keys["operator"])
        page.get_by_role("button", name="Load library").click()
        page.get_by_role("button", name="Delete Station meeting", exact=True).click()
        expect(page.get_by_role("status")).to_contain_text("Recently deleted")
        assert requests.get(watch, timeout=10).status_code == 404
        page.get_by_role("button", name="Recently deleted", exact=True).click()
        restore = page.get_by_role("button", name="Restore Station meeting", exact=True)
        expect(restore).to_be_visible(timeout=90000)
        assert requests.get(direct, timeout=10).status_code in (401, 403, 404)
        assert requests.get(playlist, timeout=10).status_code in (401, 403, 404)
        restore.click()
        expect(page.get_by_text("No recently deleted items match.")).to_be_visible(
            timeout=90000
        )
        assert requests.get(direct, timeout=10).status_code == 200
        page.goto(watch)
        frame = page.frame_locator("iframe")
        frame.get_by_role("button", name="Play Video", exact=True).click(timeout=45000)
        width = frame.locator("video").first.evaluate(
            "async v=>{v.muted=true;await v.play();return v.videoWidth}"
        )
        assert width > 0
        browser.close()
    return {
        "managed_public_video_withdrawn": True,
        "previous_public_hls_url_denied": True,
        "same_video_and_watch_link_restored": True,
        "restored_playback_width": width,
        "public_record_batch_rejected": True,
    }
