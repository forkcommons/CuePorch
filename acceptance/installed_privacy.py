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
