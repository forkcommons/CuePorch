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
        visitor.get_by_label("Video version").select_option(index=1)
        visitor.get_by_role("button", name="View version").click()
        expect(visitor).to_have_url(link + "?version=" + recent[1]["key"])
        previous = player.evaluate(
            "async v=>{v.muted=true;await v.play();return v.videoWidth}"
        )
        assert previous == 256
    finally:
        context.close()

    row = dialog.locator("li").filter(has_text="Curator screening")
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
            "password": "",
        },
        timeout=10,
    )
    response.raise_for_status()
    assert requests.get(media + "?download=1", timeout=10).status_code == 403
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
    }
