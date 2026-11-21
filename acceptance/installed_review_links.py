"""A separate, account-free visitor opens a protected review from a package install."""

import time
from base64 import b64decode

import requests
from installed_cards import select_action
from playwright.sync_api import expect


def check_review_links(page, base, auth, work, review_update):
    work_id = work["work"]
    title = work["title"]
    endpoint = base + "/api/library/work/" + work_id + "/review-links"
    assert requests.get(endpoint, timeout=10).status_code == 403
    select_action(page, title, "Review link", work_id)
    dialog = page.locator("#workDialog")
    dialog.get_by_label("Review link name").fill("Curator screening")
    dialog.get_by_label("Review password").fill("fixture-screening-password")
    dialog.get_by_label("Allow download").check()
    dialog.get_by_label("Allow previous versions").check()
    dialog.get_by_label("Allow status changes").check()
    dialog.get_by_label("Allow resolved comments").check()
    dialog.get_by_role("button", name="Create review link", exact=True).click()
    expect(dialog.get_by_role("status")).to_have_text("Review link created.")
    link = dialog.get_by_label("Link for Curator screening").input_value()
    records = requests.get(endpoint, headers=auth, timeout=10)
    records.raise_for_status()
    record = records.json()[0]
    assert abs(record["expires"] - time.time() - (30 * 86400)) < 120
    assert record["password"] and "password_hash" not in record
    preview_api = endpoint + "/" + record["id"] + "/preview"
    assert requests.post(preview_api, timeout=10).status_code == 403
    with page.context.expect_page() as preview:
        dialog.get_by_role("button", name="Preview as reviewer", exact=True).click()
    operator_view = preview.value
    expect(
        operator_view.get_by_text("You are previewing the recipient’s review page.")
    ).to_be_visible()
    expect(operator_view.get_by_label("Review password")).to_have_count(0)
    operator_view.close()
    recent = requests.get(
        base + "/api/library/work/" + work_id + "/versions", headers=auth, timeout=10
    ).json()
    media = link + "/media/" + recent[0]["key"]
    assert requests.get(media, timeout=10).status_code == 403
    assert (
        requests.post(
            link + "/unlock",
            data={"password": "wrong"},
            timeout=10,
        ).status_code
        == 403
    )
    unlocked = requests.Session()
    response = unlocked.post(
        link + "/unlock",
        data={"password": "fixture-screening-password"},
        allow_redirects=False,
        timeout=10,
    )
    assert response.status_code == 303 and "HttpOnly" in response.headers["Set-Cookie"]
    binary = unlocked.get(
        media + "?download=1", headers={"Range": "bytes=0-127"}, timeout=10
    )
    assert binary.status_code == 206 and len(binary.content) == 128
    assert "attachment" in binary.headers["Content-Disposition"]
    feedback = link + "/feedback/" + recent[0]["key"]
    assert requests.get(feedback, timeout=10).status_code == 403
    assert unlocked.get(feedback + "/comments.csv", timeout=10).status_code == 403

    context = page.context.browser.new_context()
    try:
        visitor = context.new_page()
        visitor.goto(link)
        expect(visitor.get_by_role("heading", name="Video review")).to_be_visible()
        expect(visitor.locator("video")).to_have_count(0)
        visitor.get_by_label("Review password").fill("fixture-screening-password")
        visitor.get_by_role("button", name="Open review").click()
        expect(visitor.get_by_role("heading", name=title)).to_be_visible()
        player = visitor.locator("video")
        initial = player.evaluate(
            "async v=>{v.muted=true;await v.play();return v.videoWidth}"
        )
        assert initial == 320
        expect(
            visitor.get_by_text("You are previewing the recipient’s review page.")
        ).to_have_count(0)
        expect(visitor.get_by_role("button", name="Upload new version")).to_have_count(
            0
        )
        assert visitor.request.post(link + "/versions").status == 403
        visitor.get_by_label("Playback speed").select_option("1.5")
        visitor.get_by_label("Loop", exact=True).check()
        assert player.evaluate("v => [v.playbackRate, v.loop]") == [1.5, True]
        player.evaluate("v => {v.pause(); v.currentTime = 1;}")
        visitor.get_by_role("button", name="Next frame", exact=True).click()
        visitor.wait_for_function(
            "target => { const video = document.querySelector('#reviewTourPlayer'); "
            "return !video.seeking && Math.abs(video.currentTime - target) < .008; }",
            arg=25 / 24, timeout=5000,
        )
        visitor.get_by_role("button", name="Previous frame", exact=True).click()
        visitor.wait_for_function(
            "target => { const video = document.querySelector('#reviewTourPlayer'); "
            "return !video.seeking && Math.abs(video.currentTime - target) < .008; }",
            arg=1, timeout=5000,
        )
        player.evaluate("v => {v.pause(); v.currentTime = 1.25;}")
        visitor.get_by_role("button", name="Use current time", exact=True).click()
        visitor.get_by_label("Name", exact=True).fill("A reviewer")
        visitor.get_by_label("Comment", exact=True).fill(
            "Keep this frame in the sequence."
        )
        visitor.get_by_role("button", name="Post comment", exact=True).click()
        expect(
            visitor.get_by_text("Keep this frame in the sequence.", exact=True)
        ).to_be_visible()
        visitor.get_by_role("button", name="Reply", exact=True).click()
        visitor.get_by_label("Comment", exact=True).fill(
            "=1+1. See https://example.invalid/reel. <script>text</script>"
        )
        visitor.get_by_role("button", name="Post reply", exact=True).click()
        reference = visitor.get_by_role(
            "link", name="https://example.invalid/reel", exact=True
        )
        expect(reference).to_have_attribute("rel", "noopener noreferrer")
        expect(reference.locator("..")).to_contain_text("<script>text</script>")
        expect(visitor.locator("#reviewComments script")).to_have_count(0)
        comment = visitor.locator("#reviewComments > li").filter(
            has_text="Keep this frame"
        )
        reaction = comment.get_by_role("button", name="Like", exact=True).first
        reaction.click()
        expect(reaction).to_have_attribute("aria-pressed", "true")
        check_mentions(visitor, page.context.browser, link, feedback)
        expect(reaction).to_contain_text("1")
        visitor.reload()
        expect(reaction).to_have_attribute("aria-pressed", "true")
        assert unlocked.get(feedback, timeout=10).json()["comments"][0][
            "reactions"
        ] == [{"emoji": "👍", "count": 1, "selected": False}]
        reaction.click()
        expect(reaction).to_have_attribute("aria-pressed", "false")
        reaction.click()
        expect(reaction).to_have_attribute("aria-pressed", "true")
        visitor.get_by_label("Search comments").fill("absent phrase")
        expect(visitor.get_by_text("No matching comments.", exact=True)).to_be_visible()
        visitor.get_by_label("Search comments").clear()
        visitor.get_by_label("Review status").select_option("Needs review")
        visitor.get_by_role("button", name="Save status", exact=True).click()
        expect(visitor.locator("#reviewStatus")).to_have_text("Needs review")
        visitor.get_by_label("Video version").select_option(index=1)
        visitor.get_by_role("button", name="View version").click()
        prior_cut = link + "?version=" + recent[1]["key"]
        visitor.wait_for_url(lambda url: url.split("#", 1)[0] == prior_cut)
        previous = player.evaluate(
            "async v=>{v.muted=true;await v.play();return v.videoWidth}"
        )
        assert previous == 256
        player.evaluate("v => {v.pause(); v.currentTime = 0.75;}")
        choices = visitor.locator("#reviewQuality")
        options = choices.get_by_role("option").all_text_contents()
        assert len(options) == 2, f"Expected both archived renditions, got {options!r}"
        original = choices.locator('option[value$="rendition=original"]')
        expect(original).to_have_text("Original · 256 × 144")
        choices.select_option(value=original.get_attribute("value"))
        expect(choices).to_be_enabled()
        assert player.evaluate("v => v.currentSrc").endswith("rendition=original")
        assert abs(player.evaluate("v => v.currentTime") - 0.75) < 0.1
        expect(visitor.get_by_text("No comments yet.", exact=True)).to_be_visible()
        replay = visitor.get_by_role("button", name="Play again", exact=True)
        expect(replay).to_be_hidden()
        player.evaluate(
            "async v => { v.currentTime = v.duration - 0.2; await v.play(); }"
        )
        expect(replay).to_be_visible()
        expect(visitor.locator("#reviewRecommendations")).to_have_count(0)
        replay.click()
        expect(replay).to_be_hidden()
        assert player.evaluate("v => !v.paused && v.currentTime < v.duration / 2")
    finally:
        context.close()

    row = dialog.locator("li").filter(has_text="Curator screening")
    view = unlocked.get(feedback, timeout=10)
    view.raise_for_status()
    contents = view.json()
    assert contents["status"] == "Needs review"
    thread = next(
        comment for comment in contents["comments"] if comment["parent"] is None
    )
    assert abs(thread["seconds"] - 1.25) < 0.1
    assert any(item["parent"] == thread["id"] for item in contents["comments"])
    row.get_by_role("button", name="Comments", exact=True).click()
    expect(
        dialog.get_by_text("Keep this frame in the sequence.", exact=True)
    ).to_be_visible()
    with page.expect_download() as csv:
        dialog.get_by_role("button", name="Download comments CSV").click()
    exported = csv.value.path().read_text(encoding="utf8")
    assert "Keep this frame in the sequence." in exported and "'=1+1" in exported
    dialog.get_by_role("button", name="Resolve thread", exact=True).click()
    expect(
        dialog.get_by_role("button", name="Reopen thread", exact=True)
    ).to_be_visible()
    assert any(
        item["resolved"]
        for item in unlocked.get(feedback, timeout=10).json()["comments"]
    )
    dialog.get_by_role("button", name="Back to review links", exact=True).click()
    with page.context.expect_page() as preview:
        row.get_by_role("button", name="Preview as reviewer", exact=True).click()
    operator_view = preview.value
    try:
        expect(operator_view.get_by_label("New review version")).to_be_visible()
        assert operator_view.request.post(link + "/versions").status == 403
        operator_view.get_by_label("New review version").set_input_files(review_update)
        with operator_view.expect_response(
            lambda response: (
                response.url == link + "/versions" and response.request.method == "POST"
            ),
            timeout=40000,
        ) as uploaded:
            operator_view.get_by_role(
                "button", name="Upload new version", exact=True
            ).click()
        response = uploaded.value
        assert response.ok, f"Review replacement returned HTTP {response.status}"
        selected_cut = operator_view.locator(
            '#uploadReviewVersion input[name="current"]'
        )
        expect(selected_cut).not_to_have_value(recent[0]["key"])
        current = selected_cut.input_value()
    finally:
        operator_view.close()
    retained = requests.get(
        base + "/api/library/work/" + work_id + "/versions", headers=auth, timeout=10
    ).json()
    assert [version["key"] for version in retained[:2]] == [current, recent[0]["key"]]
    assert (
        unlocked.get(feedback, timeout=10).json()["comments"][0]["reactions"][0][
            "count"
        ]
        == 1
    )
    again = page.context.browser.new_context()
    try:
        recipient = again.new_page()
        recipient.goto(link)
        recipient.get_by_label("Review password").fill("fixture-screening-password")
        recipient.get_by_role("button", name="Open review").click()
        new_width = recipient.locator("video").evaluate(
            "async v=>{v.muted=true;await v.play();return v.videoWidth}"
        )
        assert new_width == 192
        expect(recipient.get_by_text("No comments yet.", exact=True)).to_be_visible()
    finally:
        again.close()
    row.get_by_role("button", name="Disable", exact=True).click()
    expect(row.get_by_role("button", name="Enable", exact=True)).to_be_visible()
    assert unlocked.get(media, timeout=10).status_code == 404
    row.get_by_role("button", name="Enable", exact=True).click()
    expect(row.get_by_role("button", name="Disable", exact=True)).to_be_visible()
    assert unlocked.get(media, timeout=10).status_code == 403
    row.get_by_role("button", name="Make a copy", exact=True).click()
    copied = dialog.get_by_label("Link for Curator screening (copy)")
    expect(copied).to_be_visible()
    copy_url = copied.input_value()
    assert copy_url != link
    duplicate = dialog.locator("li").filter(
        has=page.get_by_label("Link for Curator screening (copy)")
    )
    duplicate.get_by_role("button", name="Delete", exact=True).click()
    expect(copied).to_have_count(0)
    assert requests.get(copy_url, timeout=10).status_code == 404
    response = requests.put(
        endpoint + "/" + record["id"],
        headers=auth,
        json={
            "name": record["name"],
            "expires": time.time() + 2,
            "allow_download": False,
            "allow_versions": False,
            "allow_comment": False,
            "password": "",
        },
        timeout=10,
    )
    response.raise_for_status()
    assert (
        requests.get(link + "/media/" + current + "?download=1", timeout=10).status_code
        == 403
    )
    assert (
        requests.post(
            link + "/feedback/" + current,
            json={"author": "Visitor", "body": "Disabled", "seconds": 0},
            timeout=10,
        ).status_code
        == 403
    )
    assert (
        requests.get(link + "/media/" + recent[1]["key"], timeout=10).status_code == 404
    )
    time.sleep(2.1)
    assert requests.get(link, timeout=10).status_code == 404
    dialog.get_by_role("button", name="Close", exact=True).click()
    return {
        "account_free_password_review_playback_width": initial,
        "permitted_prior_cut_playback_width": previous,
        "download_and_prior_version_permissions_enforced": True,
        "disable_revokes_existing_session": True,
        "copy_delete_and_expiration": True,
        "comment_timecode_replies_search_and_status": True,
        "version_specific_feedback": True,
        "staff_csv_and_resolution": True,
        "operator_recipient_preview_and_player_controls": True,
        "review_comment_links_safely_rendered": True,
        "review_reactions_persist_across_browser_reload": True,
        "review_mentions_and_notification_preferences": True,
        "default_review_end_card_replays_without_suggestions": True,
        "new_version_from_operator_review_keeps_same_link_width": new_width,
    }


def check_mentions(recipient, browser, link, feedback):
    other = browser.new_context()
    try:
        page = other.new_page()
        page.goto(link)
        page.get_by_label("Review password").fill("fixture-screening-password")
        page.get_by_role("button", name="Open review").click()
        page.get_by_role("button", name="Reply", exact=True).click()
        page.get_by_label("Name", exact=True).fill("A second reviewer")
        comment = page.get_by_label("Comment", exact=True)
        comment.fill("@A")
        page.get_by_role("button", name="Mention A reviewer", exact=True).click()
        expect(comment).to_have_value("@A reviewer ")
        comment.press("End")
        comment.press_sequentially("Please check the revised title.")
        page.get_by_role("button", name="Post reply", exact=True).click()
        posted = page.get_by_text(
            "@A reviewer Please check the revised title.", exact=True
        )
        expect(posted).to_be_visible()
        bell = recipient.get_by_text("Notifications (1)", exact=True)
        expect(bell).to_be_visible(timeout=15000)
        bell.click()
        notice = recipient.get_by_role(
            "link", name="A second reviewer mentioned you", exact=True
        )
        target = notice.get_attribute("href")
        assert target and target.startswith("#comment-")
        notice.click()
        expect(recipient.locator(target)).to_be_focused()
        expect(recipient.locator(target)).to_contain_text(
            "Please check the revised title."
        )
        expect(notice).to_have_count(0)
        assert recipient.url.endswith(target)

        mode = recipient.get_by_label("Comment notifications")
        mode.select_option("off")
        expect(mode).to_be_enabled()
        assert recipient.request.get(feedback + "/notices").json() == {
            "mode": "off",
            "items": [],
        }
        page.get_by_role("button", name="Reply", exact=True).click()
        comment.fill("@A")
        page.get_by_role("button", name="Mention A reviewer", exact=True).click()
        comment.press("End")
        comment.press_sequentially("Another notification would be disabled.")
        page.get_by_role("button", name="Post reply", exact=True).click()
        expect(
            page.get_by_text(
                "@A reviewer Another notification would be disabled.", exact=True
            )
        ).to_be_visible()
        assert recipient.request.get(feedback + "/notices").json()["items"] == []
    finally:
        other.close()


def check_review_defaults(page, base, auth, work, creator, clip):
    assert requests.get(base + "/api/review-defaults", timeout=10).status_code == 403
    select_action(page, work["title"], "Review link", work["work"])
    dialog = page.locator("#workDialog")
    canvas = page.evaluate("""() => {
        const logo = document.createElement("canvas");
        logo.width = 40;
        logo.height = 20;
        const brush = logo.getContext("2d");
        brush.fillStyle = "#134e57";
        brush.fillRect(0,0,40,20);
        return logo.toDataURL().split(",")[1];
    }""")
    dialog.get_by_label("Review logo").set_input_files(
        {
            "name": "station-logo.png",
            "mimeType": "image/png",
            "buffer": b64decode(canvas),
        }
    )
    dialog.get_by_label("Dark mode").check()
    dialog.get_by_label("Show other videos after playback").check()
    dialog.get_by_label("Automatically create review links for new uploads").check()
    dialog.get_by_role("button", name="Save as default settings").click()
    expect(dialog.get_by_role("status")).to_have_text("Default review settings saved.")
    expect(dialog.get_by_label("Dark mode")).to_be_checked()
    expect(dialog.get_by_label("Remove saved logo")).to_be_visible()
    defaults = requests.get(
        base + "/api/review-defaults", headers=auth, timeout=10
    ).json()
    assert defaults["dark"] and defaults["auto_create"] and defaults["logo"]
    dialog.get_by_role("button", name="Close", exact=True).click()

    route = requests.put(
        base + "/api/routes/work/public-program",
        headers=auth,
        json={
            "revision": "1",
            "public_authorized": True,
            "description": "Another public program",
            "destination": "https://video.example.invalid/videos/embed/public-program",
        },
        timeout=10,
    )
    route.raise_for_status()
    with clip.open("rb") as source:
        upload = requests.post(
            base + "/api/submissions",
            headers={"Authorization": "Bearer " + creator},
            data={"title": "New artwork rehearsal", "terms": "private"},
            files={
                "master": (clip.name, source, "video/mp4"),
                "caption": (
                    "screening.vtt",
                    b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nReview caption rehearsal\n",
                    "text/vtt",
                ),
            },
            timeout=25,
        )
    upload.raise_for_status()
    new_work = upload.json()["work"]
    links = requests.get(
        base + "/api/library/work/" + new_work + "/review-links",
        headers=auth,
        timeout=10,
    )
    links.raise_for_status()
    (link,) = links.json()
    assert (
        link["dark"] and link["show_other_videos"] and link["logo"] == defaults["logo"]
    )
    context = page.context.browser.new_context()
    try:
        visitor = context.new_page()
        visitor.goto(base + link["path"])
        expect(visitor.locator("body")).to_have_class("review-dark")
        image_width = visitor.get_by_role("img", name="Review logo").evaluate(
            "async img=>{await img.decode();return img.naturalWidth}"
        )
        assert image_width == 40
        recommendation = visitor.get_by_role("link", name="Another public program")
        expect(recommendation).to_be_hidden()
        video = visitor.locator("video")
        video.evaluate("v => { v.textTracks[0].mode = 'showing'; }")
        visitor.wait_for_function("document.querySelector('video track').readyState === 2")
        assert video.evaluate("v => v.textTracks[0].cues[0].text") == "Review caption rehearsal"
        assert (
            video.evaluate(
                "async v=>{v.muted=true;await v.play();v.currentTime=v.duration-0.15;return v.videoWidth;}"
            )
            == 256
        )
        expect(recommendation).to_be_visible()
    finally:
        context.close()
    return {
        "saved_defaults_used_on_private_upload": True,
        "review_dark_mode_and_local_logo_rendered": True,
        "review_player_decodes_caption_track": True,
        "authorized_suggestion_appears_after_playback": True,
    }
