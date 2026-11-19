"""Cover operator metadata and decoded media facts on the installed application."""

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
