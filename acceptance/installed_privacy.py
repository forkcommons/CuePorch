"""Exercise viewer access and link changes in the installed player."""

from urllib.parse import urlsplit

import requests
from playwright.sync_api import expect

from installed_cards import select_action


def check_privacy(page, browser, base, auth, museum, venue):
    def get(path, **options):
        return requests.get(base + path, timeout=10, **options)

    def put(path, body):
        response = requests.put(base + path, headers=auth, json=body, timeout=10)
        response.raise_for_status()
        return response.json()

    title = "Library rehearsal"
    uid = next(work["id"] for work in get("/api/library", headers=auth).json() if work["title"] == title)
    privacy = f"/api/library/work/{uid}/privacy"
    watch = f"/watch/work/{uid}"
    embed = f"/embed/work/{uid}"
    assert get(privacy).status_code == 403
    assert get(watch).status_code == 404
    assert get(privacy, headers=auth).json()["visibility"] == "private"
    links = get(f"/api/library/work/{uid}/review-links", headers=auth).json()
    assert len(links) == 1 and links[0]["dark"]
    page.get_by_role("button", name="Change privacy for " + title).click()
    editor = page.get_by_role("dialog", name="Video privacy")
    editor.locator("select[name=visibility]").select_option("unlisted")
    editor.get_by_role("button", name="Save privacy").click()
    expect(editor.get_by_role("status")).to_have_text("Privacy saved.")
    unlisted = editor.get_by_label("Watch link").input_value()
    editor.get_by_role("button", name="Close").click()
    assert urlsplit(unlisted).query and get(watch).status_code == 404
    assert title not in get("/").text
    response = requests.get(unlisted, timeout=10)
    assert response.status_code == 200 and "noindex" in response.headers["X-Robots-Tag"]

    guest = browser.new_context()
    viewer = guest.new_page()
    prior_embed = museum.embed

    def play(video):
        width = video.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth;}")
        assert width == 256
        return video.get_attribute("src")

    try:
        viewer.goto(unlisted)
        old_media = play(viewer.locator("video"))
        assert get(old_media + "?download=1").status_code == 403

        domain = urlsplit(venue).netloc
        password = "fixture-share-password"
        paths = put(privacy, dict(visibility="password", embed="domains", domains=[domain], password=password, download=True))
        assert paths["watch"] == watch and paths["embed_url"] == embed
        assert get(old_media).status_code == 404
        viewer.goto(base + watch)
        expect(viewer.locator("video")).to_have_count(0)
        viewer.get_by_label("Video password").fill("incorrect-password")
        viewer.get_by_role("button", name="Watch video").click()
        expect(viewer.get_by_role("alert")).to_contain_text("did not match")
        viewer.get_by_label("Video password").fill(password)
        viewer.get_by_role("button", name="Watch video").click()
        play(viewer.locator("video"))
        with viewer.expect_download() as received:
            viewer.get_by_role("link", name="Download video").click()
        assert received.value.path().stat().st_size > 1024

        museum.embed = base + embed
        viewer.goto(venue)
        frame = viewer.frame_locator("iframe")
        frame.get_by_label("Video password").fill(password)
        frame.get_by_role("button", name="Watch video").click()
        protected_media = play(frame.locator("video"))
        assert get(embed, headers={"Sec-Fetch-Dest": "iframe", "Referer": "http://localhost:3/"}).status_code == 403
        assert get(embed, headers={"Sec-Fetch-Dest": "iframe", "Referer": venue + "/"}).headers["Content-Security-Policy"].startswith("frame-ancestors ")

        paths = put(privacy, dict(visibility="embed_only", embed="domains", domains=[domain]))
        assert paths["watch"] is None and paths["embed_url"] == embed
        assert get(watch).status_code == get(embed).status_code == 404
        assert get(protected_media).status_code == 404
        viewer.goto(venue)
        play(viewer.frame_locator("iframe").locator("video"))
        select_action(page, title, "Share")
        share = page.locator("#workDialog")
        assert 'referrerpolicy="origin"' in share.get_by_label("Embed code").input_value()
        expect(share.get_by_label("Watch link")).to_have_count(0)
        share.get_by_role("button", name="Close").click()

        put(privacy, dict(visibility="private", embed="disabled"))
        before = get("/api/policy", headers=auth).json()["publication"]
        assert before is False
        put("/api/policy/publication", dict(required=True))
        pending = put(privacy, dict(visibility="public", embed="anywhere"))
        assert pending["pending"]["visibility"] == "public" and pending["visibility"] == "private"
        assert get(watch).status_code == 404
        assert title not in get("/").text
        put("/api/policy/publication", dict(required=False))
        assert get(privacy, headers=auth).json()["pending"] is None
        assert title in get("/").text
        viewer.goto(base + watch)
        public_media = play(viewer.locator("video"))
        put(privacy, dict(visibility="private", embed="disabled"))
        assert get(public_media).status_code == get(watch).status_code == 404
        assert title not in get("/").text
    finally:
        museum.embed = prior_embed
        guest.close()

    return dict(modes=["private", "unlisted", "password", "embed_only", "public"],
                browser_playback_width=256, wrong_password_denied=True, password_in_domain_embed=True,
                outside_domain_denied=True, key_required_for_unlisted=True, download_control=True,
                previous_media_revoked=True, publication_gate_optional=True,
                private_and_unlisted_excluded_from_catalog=True)


def check_privacy_defaults(page, browser, base, auth, source):
    endpoint = base + "/api/library/upload-defaults"
    second = {"Authorization": "Bearer fixture-distinct-operator-key"}
    assert requests.get(endpoint, timeout=10).status_code == 403
    page.get_by_role("button", name="Upload defaults").click()
    options = page.get_by_role("dialog", name="Your upload defaults")
    options.locator("select[name=visibility]").select_option("unlisted")
    options.get_by_role("button", name="Save privacy").click()
    expect(options.get_by_role("status")).to_have_text("Upload defaults saved.")
    options.get_by_role("button", name="Close").click()
    assert requests.get(endpoint, headers=second, timeout=10).json()["visibility"] == "private"

    for name, override in [("Defaulted program", False), ("Explicitly private", True)]:
        page.get_by_role("button", name="Upload", exact=True).click()
        upload = page.get_by_role("dialog", name="Upload files")
        expect(upload.locator("#uploadVisibility")).to_contain_text("Video default: unlisted")
        if override:
            upload.get_by_text("Customize privacy for these videos", exact=True).click()
            upload.locator("select[name=visibility]").select_option("private")
        upload.get_by_label("Choose files").set_input_files({
            "name": name + ".mp4", "mimeType": "video/mp4", "buffer": source.read_bytes(),
        })
        upload.get_by_role("button", name="Upload selected files").click()
        expect(upload.get_by_role("status")).to_have_text("All files added.", timeout=30000)
        expect(upload.locator("#uploadFiles")).to_contain_text("Added privately" if override else "Added · unlisted")
        upload.get_by_role("button", name="Close").click()
    assert requests.get(endpoint, headers=auth, timeout=10).json()["visibility"] == "unlisted"

    library = requests.get(base + "/api/library", headers=auth, timeout=10).json()
    ids = {row["title"]: row["id"] for row in library}
    default_watch = f"/watch/work/{ids['Defaulted program']}"
    overridden_watch = f"/watch/work/{ids['Explicitly private']}"
    assert requests.get(base + default_watch, timeout=10).status_code == 404
    assert requests.get(base + overridden_watch, timeout=10).status_code == 404
    info = requests.get(base + f"/api/library/work/{ids['Defaulted program']}", headers=auth, timeout=10).json()
    assert info["watch"].startswith(default_watch + "?key=")
    assert requests.get(base + info["watch"], timeout=10).status_code == 200

    document = ids["Library notes"]
    reject = requests.put(
        base + "/api/library/privacy", headers=auth, timeout=10,
        json=dict(works=[document, ids["Defaulted program"]], visibility="private", embed="disabled"),
    )
    assert reject.status_code == 409
    assert requests.get(base + info["watch"], timeout=10).status_code == 200

    for name in ("Defaulted program", "Explicitly private"):
        page.get_by_role("checkbox", name="Select " + name, exact=True).check()
    page.get_by_role("button", name="Privacy for selected").click()
    bulk = page.get_by_role("dialog", name="Apply privacy to selected videos")
    bulk.locator("select[name=visibility]").select_option("password")
    bulk.get_by_label("Viewer password").fill("fixture-bulk-password")
    bulk.locator("select[name=embed]").select_option("anywhere")
    bulk.get_by_label("Allow viewers to download").check()
    bulk.get_by_role("button", name="Save privacy").click()
    expect(bulk.get_by_role("status")).to_have_text("Privacy saved.")
    bulk.get_by_role("button", name="Close").click()
    browser_context = browser.new_context()
    try:
        viewer = browser_context.new_page()
        for path in [default_watch, overridden_watch]:
            viewer.goto(base + path)
            viewer.get_by_label("Video password").fill("fixture-bulk-password")
            viewer.get_by_role("button", name="Watch video").click()
            assert viewer.locator("video").evaluate("async v => {v.muted=true;await v.play();return v.videoWidth}") == 256
            expect(viewer.get_by_role("link", name="Download video")).to_be_visible()
    finally:
        browser_context.close()
    return dict(operator_default_isolation=True, default_applied_on_upload=True,
                per_upload_override=True, bulk_access_and_download=True, atomic_mixed_selection=True)


def check_folder_privacy(page, browser, base, auth, source):
    tree = page.get_by_role("navigation", name="Folders")
    parent = tree.get_by_role("button", name="Station programs", exact=True)
    child = tree.get_by_role("button", name="November meetings", exact=True)
    selected = page.get_by_role("button", name="Folder privacy", exact=True)
    dialog = page.get_by_role("dialog", name="Folder privacy")

    def work_id(name):
        response = requests.get(base + "/api/library", headers=auth, timeout=10)
        response.raise_for_status()
        return next(row["id"] for row in response.json() if row["title"] == name)

    def link(uid):
        return base + "/watch/work/" + uid

    program = work_id("Defaulted program")
    assert requests.get(link(program), timeout=10).status_code == 200
    parent.click()
    selected.click()
    dialog.locator("select[name=visibility]").select_option("private")
    dialog.get_by_role("button", name="Apply folder override").click()
    expect(dialog.get_by_role("status")).to_have_text("Folder override saved for 3 video(s).")
    dialog.get_by_role("button", name="Close").click()
    assert requests.get(link(program), timeout=10).status_code == 404

    child.click()
    page.get_by_role("button", name="Upload", exact=True).click()
    upload = page.get_by_role("dialog", name="Upload files")
    expect(upload.locator("#uploadVisibility")).to_contain_text("Folder privacy: private")
    expect(upload.get_by_text("Customize privacy for these videos", exact=True)).not_to_be_visible()
    upload.get_by_label("Choose files").set_input_files({
        "name": "Inherited upload.mp4", "mimeType": "video/mp4", "buffer": source.read_bytes(),
    })
    upload.get_by_role("button", name="Upload selected files").click()
    expect(upload.get_by_role("status")).to_have_text("All files added.", timeout=30000)
    expect(upload.locator("#uploadFiles")).to_contain_text("Added privately")
    upload.get_by_role("button", name="Close").click()
    inherited = work_id("Inherited upload")
    assert requests.get(link(inherited), timeout=10).status_code == 404

    selected.click()
    expect(dialog.get_by_text("This folder currently inherits its parent’s privacy settings.")).to_be_visible()
    dialog.locator("select[name=visibility]").select_option("password")
    dialog.get_by_label("Viewer password").fill("fixture-folder-specific")
    dialog.get_by_role("button", name="Apply folder override").click()
    expect(dialog.get_by_role("status")).to_have_text("Folder override saved for 4 video(s).")
    dialog.get_by_role("button", name="Close").click()
    parent.click()
    selected.click()
    dialog.locator("select[name=visibility]").select_option("public")
    dialog.get_by_role("button", name="Apply folder override").click()
    expect(dialog.get_by_role("status")).to_have_text("Folder override saved for 0 video(s).")
    dialog.get_by_role("button", name="Close").click()
    assert "Inherited upload" not in requests.get(base + "/", timeout=10).text

    viewer_context = browser.new_context()
    try:
        viewer = viewer_context.new_page()
        viewer.goto(link(inherited))
        viewer.get_by_label("Video password").fill("fixture-folder-specific")
        viewer.get_by_role("button", name="Watch video").click()
        assert viewer.locator("video").evaluate("async v => {v.muted=true;await v.play();return v.videoWidth}") == 256
        child.click()
        selected.click()
        dialog.get_by_role("button", name="Remove folder override").click()
        expect(dialog.get_by_role("status")).to_contain_text("Existing video settings were kept")
        dialog.get_by_role("button", name="Close").click()
        assert "Protected video" in requests.get(link(inherited), timeout=10).text

        page.get_by_role("checkbox", name="Select Inherited upload", exact=True).check()
        page.get_by_label("Destination").select_option(label="Station programs")
        page.get_by_role("button", name="Move selected").click()
        expect(page.locator("#result")).to_have_text("Moved one work.")
        viewer.goto(link(inherited))
        assert viewer.locator("video").evaluate("async v => {v.muted=true;await v.play();return v.videoWidth}") == 256
        assert "Inherited upload" in requests.get(base + "/", timeout=10).text
    finally:
        viewer_context.close()
    return dict(parent_override_applied=True, overrides_operator_default=True,
                nested_override_kept=True, remove_preserves_current=True,
                destination_rule_on_move=True)
