"""Exercise work-card actions in the installed operator and viewer interfaces."""

import hashlib
import sqlite3
import time
from contextlib import closing

import requests
from browser import chromium
from playwright.sync_api import expect, sync_playwright


def select_action(page, title, action):
    menu = page.locator("details.work-menu").filter(
        has=page.get_by_label("Actions for " + title, exact=True)
    )
    menu.locator("summary").click()
    menu.get_by_role("button", name=action, exact=True).click()


def check_cards(page, base, auth, database, work):
    original = work["title"]
    renamed = 'City council / November "archive"'
    endpoint = base + "/api/library/work/" + work["id"]
    page.goto(base + "/operator/library")
    page.get_by_role("button", name="Load library").click()
    tree = page.get_by_role("navigation", name="Folders")
    tree.get_by_role("button", name="November meetings", exact=True).click()

    dialog = page.locator("#workDialog")
    select_action(page, original, "Share")
    expect(
        dialog.get_by_role("link", name="Manage private screening grants")
    ).to_be_visible()
    expect(dialog.get_by_label("Watch link")).to_have_count(0)
    dialog.get_by_role("button", name="Close").click()

    select_action(page, original, "Watch later")
    expect(page.locator("#result")).to_have_text("Added to Watch later.")
    tree.get_by_role("button", name="Watch later", exact=True).click()
    expect(page.locator("#items .work-title")).to_have_text([original])
    page.reload()
    page.get_by_role("button", name="Load library").click()
    tree.get_by_role("button", name="Watch later", exact=True).click()
    expect(page.locator("#items .work-title")).to_have_text([original])
    second = {"Authorization": "Bearer fixture-distinct-operator-key"}
    assert not any(
        w["watch_later"]
        for w in requests.get(base + "/api/library", headers=second, timeout=10).json()
    )
    select_action(page, original, "Remove from Watch later")
    expect(page.locator("#items tr")).to_have_count(0)
    tree.get_by_role("button", name="November meetings", exact=True).click()

    select_action(page, original, "Rename")
    page.locator("#workTitle").fill(renamed)
    page.get_by_role("button", name="Save title").click()
    expect(page.get_by_text(renamed, exact=True)).to_be_visible()
    info = requests.get(endpoint, headers=auth, timeout=10).json()
    assert info["source_title"] == original and info["title"] == renamed
    assert info["id"] == work["id"] and info["watch"] is None
    select_action(page, renamed, "Video info")
    expect(dialog.get_by_text(original, exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Load video preview").click()
    width = dialog.locator("video").evaluate(
        "async v => {v.muted=true; await v.play(); return v.videoWidth}"
    )
    assert width == 320
    dialog.get_by_role("button", name="Close").click()

    select_action(page, renamed, "Download")
    with page.expect_download() as downloaded:
        dialog.get_by_role("button", name="Download original", exact=True).click()
    download = downloaded.value
    media_hash = next(f["sha256"] for f in info["files"] if f["role"] == "original")
    assert hashlib.sha256(download.path().read_bytes()).hexdigest() == media_hash
    assert download.suggested_filename == "City council _ November _archive_.mp4"
    dialog.get_by_role("button", name="Close").click()
    ticket = endpoint + "/ticket"
    assert (
        requests.post(ticket, json={"sha256": media_hash}, timeout=10).status_code
        == 403
    )
    assert (
        requests.post(
            ticket, headers=auth, json={"sha256": "0" * 64}, timeout=10
        ).status_code
        == 404
    )
    target = download.url
    partial = requests.get(target, headers={"Range": "bytes=0-127"}, timeout=10)
    assert partial.status_code == 206 and len(partial.content) == 128
    assert "no-store" in partial.headers["Cache-Control"]
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE library_download SET expires=0")
    assert requests.get(target, timeout=10).status_code == 404

    select_action(page, renamed, "Create a copy")
    page.locator("#workTitle").fill("Meeting archive duplicate")
    page.get_by_role("button", name="Create copy", exact=True).click()
    expect(page.get_by_text("Meeting archive duplicate", exact=True)).to_be_visible()
    library = requests.get(base + "/api/library", headers=auth, timeout=10).json()
    duplicate = next(w for w in library if w["title"] == "Meeting archive duplicate")
    assert duplicate["id"] != work["id"] and duplicate["folder"] is None
    copied = requests.get(
        base + "/api/library/work/" + duplicate["id"], headers=auth, timeout=10
    ).json()
    assert (
        copied["files"] == info["files"] and copied["watch"] is copied["embed"] is None
    )
    assert copied["source_privacy"] == "private"
    live_ticket = requests.post(
        ticket, headers=auth, json={"sha256": media_hash}, timeout=10
    )
    live_ticket.raise_for_status()
    return {
        "stable_identity_and_source_title": True,
        "watch_later_persisted_per_operator": True,
        "private_copy_preserved_file_hashes": True,
        "private_share_requires_grant": True,
        "native_download_matches_archive": True,
        "download_authorization_expiry_and_range": True,
        "preview_width": width,
    }, base + live_ticket.json()["download"]


def check_hosted_cards(base, origin, keys):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    jobs_url = base + "/api/delivery"
    jobs = requests.get(jobs_url, headers=auth, timeout=10).json()
    work = next(
        job for job in jobs if job["want_public"] and job["status"] == "playable"
    )
    route = "/" + work["kind"] + "/" + work["work"]
    watch = base + "/watch" + route
    embed = base + "/embed" + route
    host = origin + "/api/v1/videos/" + work["remote"]
    renamed = "Station meeting replay"
    with sync_playwright() as pw:
        browser = chromium(pw)
        page = browser.new_page(permissions=["clipboard-read", "clipboard-write"])
        page.goto(base + "/operator/library")
        page.get_by_label("Operator credential").fill(keys["operator"])
        page.get_by_role("button", name="Load library").click()
        dialog = page.locator("#workDialog")
        select_action(page, "Station meeting", "Copy link")
        expect(dialog.get_by_label("Watch link")).to_have_value(watch)
        expect(dialog.get_by_role("status")).to_have_text("Copied to clipboard.")
        assert page.evaluate("navigator.clipboard.readText()") == watch
        dialog.get_by_role("button", name="Close").click()
        select_action(page, "Station meeting", "Copy embed code")
        expect(dialog.get_by_role("status")).to_have_text("Copied to clipboard.")
        embed_code = page.evaluate("navigator.clipboard.readText()")
        assert embed_code == dialog.get_by_label("Embed code").input_value()
        assert f'src="{embed}"' in embed_code
        dialog.get_by_role("button", name="Close").click()

        select_action(page, "Station meeting", "Rename")
        page.locator("#workTitle").fill(renamed)
        page.get_by_role("button", name="Save title").click()
        expect(page.get_by_text(renamed, exact=True)).to_be_visible()
        deadline = time.monotonic() + 45
        while True:
            remote = requests.get(host, timeout=10)
            remote.raise_for_status()
            if remote.json()["name"] == renamed:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("PeerTube title did not synchronize")
            time.sleep(0.5)
        after = requests.get(jobs_url, headers=auth, timeout=10).json()
        assert {(job["id"], job["remote"]) for job in jobs} == {
            (job["id"], job["remote"]) for job in after
        }
        destination = requests.get(embed, allow_redirects=False, timeout=10).headers[
            "Location"
        ]
        assert work["remote"] in destination
        page.goto(watch)
        expect(page.get_by_role("heading", name=renamed)).to_be_visible()
        expect(page.locator("iframe")).to_have_attribute("src", destination)
        frame = page.frame_locator("iframe")
        frame.get_by_role("button", name="Play Video", exact=True).click(timeout=45000)
        width = frame.locator("video").first.evaluate(
            "async v => {v.muted=true; await v.play(); return v.videoWidth}"
        )
        assert width == 320
        browser.close()
    return {
        "watch_and_embed_copied_to_clipboard": True,
        "same_peertube_video_and_cueporch_route_after_rename": True,
        "renamed_hosted_playback_width": width,
    }
