"""Verify authenticator enrollment, login and recovery through CuePorch."""

from base64 import b32decode
from hashlib import sha1
import hmac
from time import time

from playwright.sync_api import expect


def app_code(seed, offset=0):
    step = int(time()) // 30 + offset
    digest = hmac.new(b32decode(seed), step.to_bytes(8, "big"), sha1).digest()
    start = digest[-1] & 15
    value = int.from_bytes(digest[start:start + 4], "big") & 0x7FFFFFFF
    return f"{value % 1000000:06d}"


def check_authenticator(page, browser, base, scope, email, password):
    start_url = page.url
    api = base + "/api/" + scope
    settings_url, session_url, account_url = (api + path for path in ("/authenticator", "/session", "/account"))
    headers = {"X-CuePorch-" + scope.capitalize(): "1"}
    login = {"email": email, "password": password}
    team = scope == "team"
    page.get_by_role("link", name="Account security").click()
    state = page.locator("#authenticatorStatus")
    expect(state).to_have_text("Authenticator sign-in is off.")
    start = page.locator("#authenticatorStart")
    confirmation = page.locator("#authenticatorConfirm")
    turn_off = page.locator("#authenticatorDisable")
    own = page.context.request

    with browser.new_context() as stale, browser.new_context() as returning:
        assert stale.request.post(session_url, headers=headers, data=login).ok
        assert own.post(settings_url, headers=headers | {"Origin": "https://unrelated.invalid"},
                        data={"password": password}).status == 403
        start.get_by_label("Current password").fill("incorrect fixture password")
        start.get_by_role("button", name="Set up authenticator").click()
        expect(state).to_have_text("The current password did not match.")
        start.get_by_label("Current password").fill(password)
        with page.expect_response(lambda response: response.url == settings_url and response.request.method == "POST") as opened:
            start.get_by_role("button", name="Set up authenticator").click()
        assert opened.value.ok and "no-store" in opened.value.headers["cache-control"]
        seed = confirmation.get_by_label("Setup key").input_value()
        assert seed and seed in confirmation.get_by_role("link", name="Open in authenticator app").get_attribute("href")
        old_ticket = opened.value.json()["setup_ticket"]
        used_at_setup = app_code(seed)
        confirmation.get_by_label("Six-digit code").fill(used_at_setup)
        confirmation.get_by_role("button", name="Enable authenticator").click()
        expect(state).to_have_text("Authenticator sign-in is on.")
        recovery = page.locator("#authenticatorRecovery")
        expect(recovery.get_by_role("listitem")).to_have_count(10)
        codes = recovery.get_by_role("listitem").all_text_contents()
        assert len(set(codes)) == 10
        assert stale.request.get(account_url).status == 401
        assert own.get(account_url).ok and own.get(settings_url).json() == {"enabled": True}
        assert seed not in own.get(settings_url).text()

        challenge = stale.request.post(session_url, headers=headers, data=login)
        assert challenge.status == 428 and challenge.json() == {"authenticator_required": True}
        assert stale.request.get(account_url).status == 401
        assert stale.request.post(session_url, headers=headers, data=login | {"code": used_at_setup}).status == 422

        sign_in = returning.new_page()
        sign_in.goto(base + ("/team" if team else "/account"))
        form = sign_in.locator("#teamSignIn" if team else "#viewerSignIn")
        form.get_by_label("Team email" if team else "Email").fill(email)
        form.get_by_label("Password", exact=True).fill(password)
        submit = form.get_by_role("button", name="Sign in to team" if team else "Sign in")
        submit.click()
        factor = form.get_by_label("Authenticator or recovery code")
        expect(factor).to_be_visible()
        factor.fill(codes[0])
        submit.click()
        expect(sign_in.get_by_role("link", name="Account security")).to_be_visible()
        assert returning.request.get(account_url).ok
        assert stale.request.post(session_url, headers=headers, data=login | {"code": codes[0]}).status == 422

        next_code = app_code(seed, offset=1)
        assert stale.request.post(session_url, headers=headers, data=login | {"code": next_code}).ok
        assert returning.request.post(session_url, headers=headers, data=login | {"code": next_code}).status == 422
        turn_off.get_by_label("Current password").fill(password)
        turn_off.get_by_label("Authenticator or recovery code").fill(codes[1])
        turn_off.get_by_role("button", name="Turn off authenticator").click()
        expect(state).to_have_text("Authenticator sign-in is off.")
        expect(recovery).to_be_hidden()
        assert not recovery.get_by_role("listitem").count()
        assert stale.request.get(account_url).status == returning.request.get(account_url).status == 401
        assert own.get(account_url).ok and own.get(settings_url).json() == {"enabled": False}
        assert own.put(settings_url, headers=headers, data={"setup_ticket": old_ticket, "code": app_code(seed)}).status == 409
        assert stale.request.post(session_url, headers=headers, data=login).ok
    page.goto(start_url)
    return dict(optional_default_and_manual_enrollment=True, no_seed_in_settings=True,
                password_only_creates_no_mfa_session=True, authenticator_and_recovery_login=True,
                replay_denied=True, disable_revokes_other_sessions=True, stale_enrollment_denied=True)
