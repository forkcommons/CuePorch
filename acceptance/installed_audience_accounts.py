"""Exercise two isolated viewer accounts and access changes in the installed app."""

import json
import uuid
from contextlib import ExitStack

from playwright.sync_api import expect
import requests
from installed_authenticator import check_authenticator


MUTATION = {"X-CuePorch-Viewer": "1", "Content-Type": "application/json"}


def check_audience_accounts(browser, base, staff, sample):
    title = "Independent Gallery Session"
    public = dict(visibility="public", embed="anywhere")
    with sample.open("rb") as media:
        response = requests.post(base + "/api/library/uploads", headers=staff, timeout=30,
                                 files={"file": (title + ".mp4", media, "video/mp4")},
                                 data={"upload_id": str(uuid.uuid4()), "privacy": json.dumps(public)})
    response.raise_for_status()
    video = response.json()["id"]
    path = "/watch/work/" + video
    saved_path = "/api/viewer/watch-later/work/" + video
    account_path = "/api/viewer/account"
    privacy_path = f"{base}/api/library/work/{video}/privacy"
    original = "Gallery witness fixture 2026!"
    updated = "Gallery recovery fixture 2026!"
    email = "maya.gallery@example.invalid"
    new_email = "maya.screening@example.invalid"

    def signup(page, name, email_address):
        form = page.locator("#viewerSignUp")
        form.get_by_label("Display name").fill(name)
        form.get_by_label("Email").fill(email_address)
        form.get_by_label("Password").fill(original)
        form.get_by_role("button", name="Create account").click()

    def signed_in(page, name):
        expect(page.locator("#viewerDetails").get_by_label("Display name")).to_have_value(name)

    def sign_in(page, email_address, password):
        form = page.locator("#viewerSignIn")
        form.get_by_label("Email").fill(email_address)
        form.get_by_label("Password").fill(password)
        form.get_by_role("button", name="Sign in").click()

    with ExitStack() as contexts:
        def guest():
            context = browser.new_context()
            contexts.callback(context.close)
            return context, context.new_page()

        a, first = guest()
        b, second = guest()
        returning, other_session = guest()
        first.goto(base + path)
        assert first.locator("video").evaluate("async v => { v.muted = true; await v.play(); return v.videoWidth; }") == 256
        first.get_by_role("button", name="Save to Watch Later").click()
        expect(first).to_have_url(base + "/account?next=%2Fwatch%2Fwork%2F" + video)
        signup(first, "Maya", email)
        expect(first).to_have_url(base + path)
        first.get_by_role("button", name="Save to Watch Later").click()
        expect(first.get_by_role("button", name="Remove from Watch Later")).to_be_visible()
        cookies = a.cookies()
        session = next(cookie for cookie in cookies if cookie["name"].startswith("cueporch-viewer-"))
        assert session["httpOnly"] and session["sameSite"] == "Lax" and session["path"] == "/"
        assert "cueporch-viewer" not in first.evaluate("document.cookie")
        assert a.request.get(base + "/api/watch/staff-picks").status == 403
        assert b.request.put(base + saved_path, headers=MUTATION, data={}).status == 401
        assert a.request.delete(base + saved_path, data={}).status == 403
        assert a.request.delete(base + saved_path, headers=MUTATION | {"Origin": "https://different.invalid"}, data={}).status == 403

        second.goto(base + "/watch")
        second.get_by_role("link", name="Viewer account").click()
        signup(second, "Jo", "jo.gallery@example.invalid")
        signed_in(second, "Jo")
        second.get_by_role("link", name="Watch Later").click()
        expect(second.get_by_text("Your Watch Later list is empty.")).to_be_visible()
        assert b.request.get(base + saved_path).json() == {"saved": False}

        first.goto(base + "/watch/later")
        expect(first.get_by_role("link", name=title)).to_have_attribute("href", path)
        queue = a.request.get(base + "/watch/later")
        assert "private" in queue.headers["cache-control"] and "no-store" in queue.headers["cache-control"]
        requests.put(privacy_path, headers=staff, json=dict(visibility="private", embed="disabled"), timeout=15).raise_for_status()
        first.reload()
        expect(first.get_by_role("link", name=title)).to_have_count(0)
        assert a.request.get(base + path).status == a.request.put(base + saved_path, headers=MUTATION, data={}).status == 404
        requests.put(privacy_path, headers=staff, json=public, timeout=15).raise_for_status()
        first.reload()
        expect(first.get_by_role("link", name=title)).to_be_visible()

        first.get_by_role("link", name="Viewer account").click()
        details = first.locator("#viewerDetails")
        expect(details.get_by_label("Display name")).to_have_value("Maya")
        details.get_by_label("Display name").fill("Maya Gallery")
        details.get_by_label("Pronouns").fill("she/her")
        details.get_by_label("Email", exact=True).fill(new_email)
        details.get_by_label("Current password to change email").fill(original)
        details.get_by_role("button", name="Save account details").click()
        expect(first.locator("#viewerAccountStatus")).to_have_text("Account details saved.")
        assert a.request.get(base + account_path).json() == dict(name="Maya Gallery", email=new_email, pronouns="she/her")
        authentication = check_authenticator(first, browser, base, "viewer", new_email, original)

        other_session.goto(base + "/account")
        sign_in(other_session, email, original)
        expect(other_session.locator("#viewerAccountStatus")).to_have_text("The email or password did not match.")
        sign_in(other_session, new_email, original)
        signed_in(other_session, "Maya Gallery")
        other_session.get_by_role("link", name="Watch Later").click()
        expect(other_session.get_by_role("link", name=title)).to_be_visible()

        details_url = f"{base}/api/library/work/{video}/details"
        metadata = requests.get(details_url, headers=staff, timeout=15)
        metadata.raise_for_status()
        requests.put(details_url, headers=staff, timeout=15,
                     json=dict(metadata.json(), rating="mature", content=["violence"])).raise_for_status()
        preferences = first.locator("#viewerPreferences")
        mode = preferences.get_by_role("combobox", name="Mature content", exact=True)
        save = preferences.get_by_role("button", name="Save viewing preferences")

        def saved_mode(value):
            mode.select_option(value)
            save.click()
            expect(first.locator("#viewerAccountStatus")).to_have_text("Viewing preferences saved.")

        preferences_url = base + "/api/viewer/preferences"
        autoplay = preferences.get_by_label("Autoplay")

        def save_autoplay(value):
            autoplay.select_option(value)
            with first.expect_response(lambda response: response.url == preferences_url
                                       and response.request.method == "PUT") as saved:
                save.click()
            assert saved.value.ok
            expect(first.locator("#viewerAccountStatus")).to_have_text("Viewing preferences saved.")

        save_autoplay("on")
        other_session.goto(base + "/account")
        expect(other_session.locator("#viewerPreferences").get_by_label("Autoplay")).to_have_value("on")
        assert returning.request.get(preferences_url).json()["autoplay"] is True
        other_session.goto(base + path)
        other_session.wait_for_function(
            """() => {
                const v = document.querySelector('video');
                return v && v.muted && v.videoWidth === 256 && v.currentTime > 0.1;
            }"""
        )
        other_session.evaluate(
            """src => {
                const frame = document.createElement('iframe');
                frame.id = 'autoplay-embed';
                frame.src = src;
                document.body.appendChild(frame);
            }""",
            "/embed/work/" + video,
        )
        player = other_session.frame_locator("#autoplay-embed").locator("video")
        expect(player).to_be_visible()
        assert player.get_attribute("autoplay") is None
        save_autoplay("off")
        legacy = returning.request.put(preferences_url, headers=MUTATION, data={"mode": "all"})
        assert legacy.ok and legacy.json()["autoplay"] is False
        invalid = returning.request.put(preferences_url, headers=MUTATION, data={"mode": "all", "autoplay": "yes"})
        assert invalid.status == 400
        other_session.goto(base + path)
        assert other_session.locator("video").evaluate("v => !v.autoplay && v.paused && v.currentTime === 0")
        save_autoplay("default")
        assert "autoplay" not in returning.request.get(preferences_url).json()

        saved_mode("hide_mature")
        search_url = base + "/watch?q=Independent+Gallery+Session"
        assert returning.request.get(preferences_url).json() == {"mode": "hide_mature", "excluded": []}
        assert b.request.get(preferences_url).json()["mode"] == "all"
        assert returning.request.put(preferences_url, headers=MUTATION | {"Origin": "https://different.invalid"},
                                     data={"mode": "all"}).status == 403
        other_session.goto(search_url)
        expect(other_session.get_by_role("link", name=title)).to_have_count(0)
        second.goto(search_url)
        expect(second.get_by_role("link", name=title)).to_be_visible()
        assert title in requests.get(search_url, timeout=10).text
        warning = other_session.goto(base + path + "?start=0.1")
        assert "private" in warning.headers["cache-control"] and "no-store" in warning.headers["cache-control"]
        expect(other_session.locator("video")).to_have_count(0)
        other_session.get_by_role("link", name="Show this video").click()
        assert other_session.url.endswith("?start=0.1&show_content=1")
        assert other_session.locator("video").evaluate(
            "async v => { v.muted = true; await v.play(); return v.videoWidth; }"
        ) == 256

        mode.select_option("custom")
        preferences.get_by_role("checkbox", name="Unrated videos").check()
        preferences.get_by_role("checkbox", name="Nudity").check()
        saved_mode("custom")
        other_session.goto(search_url)
        expect(other_session.get_by_role("link", name=title)).to_be_visible()
        preferences.get_by_role("checkbox", name="Violence").check()
        saved_mode("custom")
        other_session.reload()
        expect(other_session.get_by_role("link", name=title)).to_have_count(0)
        requests.put(details_url, headers=staff, timeout=15,
                     json=dict(metadata.json(), rating="unrated", content=[])).raise_for_status()
        other_session.reload()
        expect(other_session.get_by_role("link", name=title)).to_have_count(0)
        saved_mode("all")
        other_session.reload()
        expect(other_session.get_by_role("link", name=title)).to_be_visible()
        change = first.locator("#changeViewerPassword")
        change.get_by_label("Current password").fill(original)
        change.get_by_label("New password").fill(updated)
        change.get_by_role("button", name="Change password").click()
        expect(first.locator("#viewerAccountStatus")).to_have_text("Password changed. Your other sessions were signed out.")
        assert returning.request.get(base + account_path).status == 401
        assert next(cookie["value"] for cookie in a.cookies() if cookie["name"] == session["name"]) != session["value"]
        assert a.request.get(base + account_path).ok

        first.get_by_role("link", name="Watch Later").click()
        first.get_by_role("button", name=f"Remove {title} from Watch Later").click()
        expect(first.get_by_text("Your Watch Later list is empty.")).to_be_visible()
        first.goto(base + path)
        first.get_by_role("button", name="Save to Watch Later").click()
        expect(first.get_by_role("button", name="Remove from Watch Later")).to_be_visible()
        first.goto(base + "/account")
        first.get_by_role("button", name="Sign out").click()
        expect(first.locator("#viewerAccountStatus")).to_have_text("Signed out.")
        assert a.request.get(base + account_path).status == 401
        sign_in(first, new_email, original)
        expect(first.locator("#viewerAccountStatus")).to_have_text("The email or password did not match.")
        sign_in(first, new_email, updated)
        signed_in(first, "Maya Gallery")
        saved_mode("hide_mature")
        first.get_by_text("Delete account", exact=True).click()
        removal = first.locator("#deleteViewerAccount")
        removal.get_by_label("Current password").fill(updated)
        removal.get_by_role("button", name="Delete my account").click()
        expect(first.locator("#viewerAccountStatus")).to_have_text("Account deleted.")
        signup(first, "Maya Returned", new_email)
        signed_in(first, "Maya Returned")
        assert a.request.get(base + saved_path).json() == {"saved": False}
        assert a.request.get(preferences_url).json() == {"mode": "all", "excluded": []}
        assert b.request.get(base + account_path).json()["name"] == "Jo"
        return dict(account_free_public_playback_width=256, separate_operator_access=True, cross_origin_changes_denied=True,
                    own_watch_later_isolated=True, save_resumes_after_signup=True, status_persisted_across_browsers=True,
                    saved_private_video_hidden_and_denied=True, account_details_updated=True, login_cookie_rotated=True,
                    revoke_other_sessions_on_password_change=True, remove_and_sign_out=True, deletion_discards_list=True,
                    mature_and_custom_account_filters=True, independent_anonymous_catalog=True,
                    direct_link_warning_then_playback_width=256, deletion_resets_viewing_preferences=True,
                    authenticator=authentication)
