"""A separate, account-free visitor opens a protected review from a package install."""

import time

import requests
from installed_cards import select_action
from playwright.sync_api import expect


def check_review_links(page, base, auth, work):
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
        visitor.get_by_label("Comment", exact=True).fill("=1+1")
        visitor.get_by_role("button", name="Post reply", exact=True).click()
        expect(visitor.get_by_text("=1+1", exact=True)).to_be_visible()
        visitor.get_by_label("Search comments").fill("absent phrase")
        expect(visitor.get_by_text("No matching comments.", exact=True)).to_be_visible()
        visitor.get_by_label("Search comments").clear()
        visitor.get_by_label("Review status").select_option("Needs review")
        visitor.get_by_role("button", name="Save status", exact=True).click()
        expect(visitor.locator("#reviewStatus")).to_have_text("Needs review")
        visitor.get_by_label("Video version").select_option(index=1)
        visitor.get_by_role("button", name="View version").click()
        expect(visitor).to_have_url(link + "?version=" + recent[1]["key"])
        previous = player.evaluate(
            "async v=>{v.muted=true;await v.play();return v.videoWidth}"
        )
        assert previous == 256
        expect(visitor.get_by_text("No comments yet.", exact=True)).to_be_visible()
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
    assert requests.get(media + "?download=1", timeout=10).status_code == 403
    assert (
        requests.post(
            feedback,
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
    }
