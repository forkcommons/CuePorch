"""Cover operator metadata and decoded media facts on the installed application."""

import sqlite3
import time
from urllib.parse import urlsplit

import requests
from playwright.sync_api import expect

from installed_cards import select_action


def check_video_details(page, browser, base, auth):
    name = "Inherited upload"
    tree = page.get_by_role("navigation", name="Folders")
    tree.get_by_role("button", name="Station programs", exact=True).click()
    library = requests.get(base + "/api/library", headers=auth, timeout=10).json()
    uid = next(item["id"] for item in library if item["title"] == name)
    api = f"{base}/api/library/work/{uid}/details"
    assert requests.get(api, timeout=10).status_code == 403
    select_action(page, name, "Edit video details")
    expect(page.get_by_role("heading", level=1)).to_have_text(name)
    expect(page.locator("#videoFacts")).to_contain_text("256 × 144")
    expect(page.locator("#videoFacts")).to_contain_text("24 fps")
    title = "Institutional video details"
    description = "A recording with <script>window.disclosureTampered = true</script> in its text."
    form = page.locator("#videoMeta")
    form.get_by_label("Title").fill(title)
    form.get_by_label("Description").fill(description)
    form.get_by_label("Tags").fill("archive\nmedia arts")
    form.get_by_label("Categories").fill("City government\nExperimental")
    form.get_by_label("Credits").fill("A producer | Producer\nA voice")
    form.get_by_label("Copyright license").select_option("by-sa")
    form.get_by_label("Content rating").select_option("mature")
    form.get_by_label("Profanity").check()
    form.get_by_label("Contains advertisement").check()
    form.get_by_label("Includes realistic AI-generated content").check()
    form.get_by_role("button", name="Save video details").click()
    expect(page.get_by_role("status")).to_have_text("Video details saved.")
    page.reload()
    expect(form.get_by_label("Description")).to_have_value(description)
    expect(form.get_by_label("Copyright license")).to_have_value("by-sa")
    saved = requests.get(api, headers=auth, timeout=10).json()
    assert saved["title"] == title and saved["rating"] == "mature" and saved["content"] == ["language"]
    assert saved["advertisement"] and saved["realistic_ai"]
    assert saved["tags"] == ["archive", "media arts"] and saved["categories"] == ["City government", "Experimental"]
    assert saved["credits"] == [dict(name="A producer", role="Producer"), dict(name="A voice", role="")]
    invalid = requests.put(api, headers=auth, timeout=10, json=dict(saved, description="a" * 5001, title="Invalid attempt"))
    assert invalid.status_code == 400
    assert requests.get(api, headers=auth, timeout=10).json() == saved

    guest = browser.new_context()
    try:
        viewer = guest.new_page()
        viewer.goto(f"{base}/watch/work/{uid}")
        expect(viewer.get_by_role("heading", level=1)).to_have_text(title)
        expect(viewer.get_by_text(description, exact=True)).to_be_visible()
        expect(viewer.get_by_text("Mature audiences · Profanity")).to_be_visible()
        expect(viewer.get_by_text("Contains advertisement")).to_be_visible()
        expect(viewer.get_by_text("Includes realistic AI-generated content")).to_be_visible()
        assert viewer.evaluate("window.disclosureTampered === undefined")
        assert viewer.locator("video").evaluate("async v => {v.muted=true;await v.play();return v.videoWidth}") == 256
    finally:
        guest.close()
    return dict(version_independent_details_saved=True, saved_details_on_public_watch=True,
                description_escaped=True, invalid_metadata_does_not_rename=True,
                resolution_and_native_fps=True)


def check_imported_video_details(base, auth):
    library = requests.get(base + "/api/library", headers=auth, timeout=10).json()
    uid = next(item["id"] for item in library if item["title"] == "Migrated unlisted")
    response = requests.get(f"{base}/api/library/work/{uid}/details", headers=auth, timeout=10)
    response.raise_for_status()
    data = response.json()
    assert data["license"] == "by" and data["rating"] == "mature" and data["advertisement"]
    assert data["tags"] == ["city council"] and data["categories"] == ["Documentary"]
    assert data["content"] == ["language"] and data["projection"] == "equirectangular"
    assert data["media"]["width"] == 320 and data["media"]["audio"].startswith("aac")
    return dict(migrated_editorial_fields=True, source_projection_and_audio_format=True)


def check_workspace(page, browser, base, auth, database):
    title = "Institutional video details"
    workspace = page.url
    uid = urlsplit(workspace).path.rsplit("/", 1)[-1]
    api = f"{base}/api/library/work/{uid}"
    preview = page.locator("#workspacePreview")
    expect(preview).to_be_visible()
    assert preview.evaluate("async v => {v.muted=true;await v.play();v.pause();return v.videoWidth}") == 256
    preview.evaluate("v => {v.currentTime=0.45;}")
    expect(page.get_by_label("Editorial status")).to_have_value("In progress")
    page.get_by_label("Editorial status").select_option("Needs review")
    expect(page.get_by_role("status")).to_have_text("Editorial status saved.")
    original = requests.get(api + "/editorial-status", headers=auth, timeout=10).json()
    assert original["status"] == "Needs review"

    assert requests.get(api + "/share-link", timeout=10).status_code == 403
    assert requests.put(api + "/share-link", timeout=10, json={"slug": "public-reel", "days": 1}).status_code == 403
    page.get_by_label("Custom link name").fill("city-arts-reel")
    page.get_by_label("Expiration").select_option("1")
    page.get_by_role("button", name="Save custom link").click()
    expect(page.locator("#sharingMessage")).to_have_text("Custom link saved.")
    page.get_by_role("button", name="Use preview time").click()
    alias = page.get_by_label("Viewer link").input_value()
    assert alias == base + "/v/city-arts-reel?start=0.450"
    row = requests.get(api + "/share-link", headers=auth, timeout=10).json()
    assert abs(row["alias"]["expires"] - time.time() - 86400) < 60

    response = requests.post(api + "/review-links", headers=auth, timeout=10,
                             json=dict(name="Workspace reviewer", password="", allow_status=True))
    response.raise_for_status()
    review = response.json()
    guest = browser.new_context()
    try:
        view = guest.new_page()
        view.goto(alias)
        expect(view.get_by_role("heading", name=title, exact=True)).to_be_visible()
        player = view.locator("video")
        width = player.evaluate("async v => {v.muted=true;await v.play();v.pause();return v.videoWidth}")
        assert width == 256 and player.evaluate("v => v.currentTime") >= 0.4
        media = base + player.get_attribute("src")
        assert requests.head(media, timeout=10).status_code == 200
        with sqlite3.connect(database) as con:
            con.execute("UPDATE library_share_alias SET expires=? WHERE work=?", (time.time() - 1, uid))
        assert requests.get(alias, timeout=10).status_code == 404
        assert requests.head(media, timeout=10).status_code == 404
        assert requests.get(base + "/watch/work/" + uid, timeout=10).status_code == 200
        view.goto(base + review["path"])
        expect(view.locator("#reviewStatus")).to_have_text("Needs review")
        view.get_by_label("Review status").select_option("Approved")
        view.get_by_role("button", name="Save status", exact=True).click()
        expect(view.locator("#reviewStatus")).to_have_text("Approved")
    finally:
        guest.close()
    page.reload()
    expect(page.get_by_label("Editorial status")).to_have_value("Approved")
    expect(page.get_by_label("Active review link")).to_have_value(base + review["path"])
    page.get_by_role("link", name="Versions", exact=True).click()
    expect(page.get_by_role("dialog", name="Replace video")).to_be_visible()
    page.goto(workspace)
    page.get_by_text("File actions", exact=True).click()
    page.get_by_role("link", name="Download files").click()
    expect(page.get_by_role("dialog", name="Download " + title)).to_be_visible()
    return dict(private_operator_preview_width=256, editorial_status_scoped_to_current_cut=True,
                authorized_reviewer_updates_workspace_status=True, custom_alias_timestamp_playback=True,
                expired_alias_revoked_existing_media=True, canonical_public_link_unchanged=True,
                version_and_file_actions_reachable=True)
