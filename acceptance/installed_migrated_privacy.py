"""Check that mapped Vimeo source rules still govern installed guest playback."""

import csv
import time
from urllib.parse import urlsplit

import requests
from playwright.sync_api import expect

from installed_cards import select_action
from browser import chromium
from playwright.sync_api import sync_playwright


def check_migrated_privacy(page, browser, base, auth, source, museum, venue):
    previous_embed = museum.embed
    legacy_embed = f"http://127.0.0.1:{source.server_port}/fixture/legacy-player"
    guest = browser.new_context()
    viewer = guest.new_page()
    source.privacy_domain = urlsplit(venue).netloc

    def transfer(stage):
        source.privacy_fixture = stage
        page.goto(base + "/operator/migration")
        page.wait_for_function("typeof document.querySelector('#transfer')?.onclick === 'function'")
        page.get_by_role("button", name="Preview transfer").click()
        expect(page.locator("#status")).to_have_text("inventoried", use_inner_text=True, timeout=30000)
        page.reload()
        expect(page.locator("#status")).to_have_text("inventoried", use_inner_text=True)
        page.get_by_role("button", name="Transfer privately").click()
        page.wait_for_function("document.querySelector('#status').textContent.startsWith('transferred')")

    def work_path(uid, resource=""):
        return f"/api/library/work/{uid}{resource}"

    def restore(uid, password=None):
        response = requests.put(base + work_path(uid, "/source-privacy"), headers=auth, timeout=10,
                                json={"password": password} if password else {})
        response.raise_for_status()
        return response.json()

    def play(locator):
        width = locator.evaluate("async v => {v.muted=true;await v.play();return v.videoWidth;}")
        assert width == 320
        return locator.get_attribute("src")

    def download_map():
        page.goto(base + "/operator/migration")
        with page.expect_download() as result:
            page.get_by_role("button", name="Download embed map").click()
        download = result.value
        assert download.suggested_filename == "cueporch-embed-map.csv"
        with download.path().open(encoding="utf8", newline="") as stream:
            return list(csv.DictReader(stream))

    try:
        museum.embed = legacy_embed
        viewer.goto(venue)
        play(viewer.frame_locator("iframe").locator("video"))
        transfer("initial")
        page.goto(base + "/operator/library")
        page.wait_for_function("typeof document.querySelector('#load')?.onclick === 'function'")
        page.get_by_role("button", name="Load library").click()
        expect(page.locator("#result")).to_have_text("Library loaded.")
        catalog = requests.get(base + "/api/library", headers=auth, timeout=10).json()
        ids = {work["title"]: work["id"] for work in catalog}
        unlisted = ids["Migrated unlisted"]
        no_embeds = ids["Missing embed allowlist"]
        assert requests.get(base + work_path(unlisted, "/source-privacy"), timeout=10).status_code == 403
        assert requests.get(base + "/watch/work/" + unlisted, timeout=10).status_code == 404

        select_action(page, "Migrated unlisted", "Restore Vimeo privacy")
        dialog = page.get_by_role("dialog", name="Restore Vimeo privacy")
        expect(dialog.get_by_text("Recorded visibility:", exact=False)).to_contain_text("unlisted")
        dialog.get_by_role("button", name="Apply recorded privacy").click()
        expect(dialog.get_by_role("status")).to_contain_text("Source privacy applied.")
        dialog.get_by_role("button", name="Close").click()
        info = requests.get(base + work_path(unlisted), headers=auth, timeout=10).json()
        assert info["embed"] is None and "?key=" in info["watch"]
        viewer.goto(base + info["watch"])
        previously_unlisted_media = play(viewer.locator("video"))
        expect(viewer.get_by_role("link", name="Download video")).to_be_visible()
        assert requests.get(base + "/api/embed-map.csv", timeout=10).status_code == 403
        entry = next(row for row in download_map() if row["source_id"] == "74")
        assert entry["old_url"] == "https://vimeo.com/74/abcdef1234"
        assert entry["watch_url"] == base + info["watch"] and not entry["embed_url"]

        password_id = ids["Migrated password"]
        password = "migration-replacement-password"
        paths = restore(password_id, password)["paths"]
        assert paths["watch"] and paths["embed_url"]
        replacement = next(row for row in download_map() if row["work_id"] == password_id)
        assert replacement["embed_url"] == base + paths["embed_url"]
        museum.embed = replacement["embed_url"]
        viewer.goto(venue)
        frame = viewer.frame_locator("iframe")
        frame.get_by_label("Video password").fill(password)
        frame.get_by_role("button", name="Watch video").click()
        play(frame.locator("video"))
        expect(frame.get_by_role("link", name="Download video")).to_have_count(0)
        response = requests.get(base + paths["embed_url"], timeout=10,
                                headers={"Referer": "http://localhost:3/", "Sec-Fetch-Dest": "iframe"})
        assert response.status_code == 403
        museum.embed = legacy_embed
        viewer.goto(venue)
        play(viewer.frame_locator("iframe").locator("video"))

        embed_id = ids["Migrated embed"]
        paths = restore(embed_id)["paths"]
        assert paths["watch"] is None and paths["embed_url"]
        museum.embed = base + paths["embed_url"]
        viewer.goto(venue)
        play(viewer.frame_locator("iframe").locator("video"))
        assert requests.get(base + "/watch/work/" + embed_id, timeout=10).status_code == 404
        missing = restore(no_embeds)
        assert missing["settings"]["visibility"] == "private" and missing["paths"] is None
        assert "allowlist could not be recovered" in missing["issue"]

        transfer("tightened")
        assert requests.get(base + info["watch"], timeout=10).status_code == 404
        assert requests.get(base + previously_unlisted_media, timeout=10).status_code == 404
        response = requests.get(base + work_path(unlisted), headers=auth, timeout=10)
        assert response.json()["watch"] is None
    finally:
        source.privacy_fixture = None
        museum.embed = previous_embed
        guest.close()

    return dict(unlisted_key_and_source_map=True, browser_playback_width=320,
                preview_restored_on_reload=True, protected_map_download=True, controlled_embed_rollback=True,
                replacement_password_and_embed_allowlist=True, embed_only_preserved=True,
                missing_source_allowlist_stays_private=True, repeat_import_revokes_old_access=True)


def check_hosted_handoff(base, origin, keys):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    jobs = requests.get(base + "/api/delivery", headers=auth, timeout=10).json()
    public = next(job for job in jobs if job["want_public"] and job["status"] == "playable" and job["kind"] == "work")
    uid, video_id = public["work"], public["remote"]
    endpoint = f"{base}/api/library/work/{uid}/source-privacy"
    protected = next(job for job in jobs if not job["want_public"] and job["status"] == "playable" and job["work"] != uid)
    protected_watch = f"{base}/watch/meeting/{protected['work']}"
    protected_before = requests.get(protected_watch, timeout=10)
    protected_before.raise_for_status()
    protected_update = requests.put(f"{base}/api/library/work/{protected['work']}/source-privacy", headers=auth, timeout=10, json={})
    assert protected_update.status_code == 409
    assert requests.get(protected_watch, timeout=10).text == protected_before.text

    watch = f"/watch/work/{uid}"
    embed = f"/embed/work/{uid}"
    response = requests.get(base + embed, timeout=10, allow_redirects=False)
    assert response.status_code == 302 and response.headers["Location"].endswith("/" + video_id)
    remote = origin.rstrip("/") + "/api/v1/videos/" + video_id
    previous = requests.get(remote, timeout=10)
    previous.raise_for_status()
    playlist = previous.json()["streamingPlaylists"][0]["playlistUrl"]
    assert requests.get(playlist, timeout=10).status_code == 200
    requested = requests.put(endpoint, headers=auth, json={}, timeout=10)
    requested.raise_for_status()
    assert requested.json()["handoff"] == "pending"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        status = requests.get(endpoint, headers=auth, timeout=10).json()
        if status["handoff"] == "private" and not status["pending"]:
            break
        time.sleep(0.4)
    else:
        raise AssertionError("PeerTube handoff did not finish: " + str(status.get("handoff_error")))

    assert status["paths"] == {"watch": watch, "embed_url": embed}
    assert requests.get(remote, timeout=10).status_code in (401, 403, 404)
    assert requests.get(playlist, timeout=10).status_code in (401, 403, 404)
    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            page = browser.new_page()
            page.goto(base + watch)
            player = page.locator("video")
            width = player.evaluate("async v => {v.muted=true;await v.play();return v.videoWidth;}")
            assert width == 320
            expect(player.locator("track[kind=captions]")).to_have_count(1)
        finally:
            browser.close()
    reject = requests.post(base + "/api/delivery/" + uid, headers=auth, timeout=10, json={"public": True})
    assert reject.status_code == 400
    assert requests.get(remote, timeout=10).status_code in (401, 403, 404)
    return dict(managed_peertube_public_copy_made_private=True, old_peertube_playlist_denied=True,
                same_cueporch_work_links=True, routed_video_width=width, caption_track_present=True,
                protects_designated_public_records=True, independent_republish_blocked=True)
