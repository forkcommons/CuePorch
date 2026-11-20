"""Exercise team invitations and private team media from the installed service."""

import uuid
from urllib.parse import urlsplit

import requests
from playwright.sync_api import expect


TEAM = {"X-CuePorch-Team": "1", "Authorization": "Bearer team-session"}
PASSWORD = "CuePorch team rehearsal password!"


def check_team_accounts(page, browser, base, auth, replacement):
    label = uuid.uuid4().hex[:8]
    email = lambda role: f"cueporch-{role}-{label}@example.org"
    protected_title = "Team private rehearsal footage"
    with replacement.open("rb") as stream:
        uploaded = requests.post(base + "/api/library/uploads", headers=auth, timeout=30,
                                 files={"file": (protected_title + ".mp4", stream, "video/mp4")},
                                 data={"upload_id": uuid.uuid4().hex})
    uploaded.raise_for_status()
    assert uploaded.json()["visibility"] == "private"
    uid = uploaded.json()["id"]
    media = f"{base}/team/video/{uid}/media"
    assert requests.get(media, timeout=10).status_code == 401
    assert requests.get(f"{base}/watch/work/{uid}", timeout=10).status_code == 404
    assert requests.get(base + "/api/library", headers=TEAM, timeout=10).status_code == 403

    def invite(manager, address, role):
        manager.get_by_label("Invite email").fill(address)
        manager.get_by_label("Invite role").select_option(role)
        manager.get_by_role("button", name="Create invitation").click()
        expect(manager.locator("#teamMembersStatus")).to_have_text("Invitation link created.")
        link = manager.get_by_label("Private invitation link").input_value()
        assert len(urlsplit(link).fragment) == 43 and urlsplit(link).path == "/team/join"
        return link

    def join(context, link, name, role):
        member = context.new_page()
        member.goto(link)
        expect(member.get_by_role("heading", name="Accept team invitation")).to_be_visible()
        assert not urlsplit(member.url).fragment
        member.get_by_label("Display name").fill(name)
        member.get_by_label("Choose a password").fill(PASSWORD)
        member.get_by_role("button", name="Join team").click()
        expect(member.locator("#teamIdentity")).to_have_text(f"{name} · {role}")
        return member

    def accept_status(context, link):
        return context.request.post(base + "/api/team/join", headers=TEAM,
                                    data={"invitation": urlsplit(link).fragment, "name": "Old link", "password": PASSWORD}).status

    def signed_in(context):
        response = context.request.get(base + "/api/team/account")
        assert response.ok, response.status
        return response.json()

    def playback(member, name):
        member.get_by_role("link", name=name, exact=True).click()
        player = member.locator("video")
        width = player.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth;}")
        assert width > 0
        player.evaluate("v => v.pause()")
        return width

    page.goto(base + "/team/members")
    expect(page.get_by_role("heading", name="Pending invitations")).to_be_visible()
    admin_invite = invite(page, email("admin"), "admin")
    pending = page.locator("#teamInvitations li").filter(has_text=email("admin"))
    pending.get_by_role("button", name="Replace link").click()
    expect(page.locator("#teamMembersStatus")).to_have_text("New invitation link created.")
    replacement_link = page.get_by_label("Private invitation link").input_value()
    assert replacement_link != admin_invite
    canceled = invite(page, email("canceled"), "viewer")
    page.locator("#teamInvitations li").filter(has_text=email("canceled")).get_by_role("button", name="Cancel invitation").click()
    expect(page.locator("#teamMembersStatus")).to_have_text("Invitation canceled.")

    with browser.new_context() as admin, browser.new_context() as contributor, browser.new_context() as viewer:
        assert accept_status(admin, admin_invite) == accept_status(admin, canceled) == 404
        admin_page = join(admin, replacement_link, "Avery", "Administrator")
        assert accept_status(contributor, replacement_link) == 404
        admin_id = signed_in(admin)["id"]
        cookies = {cookie["name"]: cookie for cookie in admin.cookies()}
        assert cookies["cueporch-team"]["httpOnly"] and cookies["cueporch-team"]["sameSite"] == "Lax"
        assert admin.request.get(base + "/api/library", headers=TEAM).ok
        assert admin.request.get(base + "/api/library").status == 403
        admin_page.get_by_role("link", name="Manage team members").click()
        expect(admin_page.get_by_role("heading", name="Pending invitations")).to_be_visible()
        contributor_link = invite(admin_page, email("contributor"), "contributor")
        viewer_link = invite(admin_page, email("viewer"), "viewer")

        # A public viewer can choose the same email without joining the team.
        public_account = viewer.request.post(base + "/api/viewer/signup", headers={"X-CuePorch-Viewer": "1"},
            data={"name": "Public account", "email": email("viewer"), "password": "Separate viewer password!"})
        assert public_account.status == 201
        assert viewer.request.get(base + "/api/team/account").status == 401
        assert viewer.request.get(base + "/api/library", headers=TEAM).status == 403

        viewer_page = join(viewer, viewer_link, "Val", "Viewer")
        contributor_page = join(contributor, contributor_link, "Cameron", "Contributor")
        viewer_id = signed_in(viewer)["id"]
        assert signed_in(contributor)["email"] == email("contributor")
        assert viewer.request.get(base + "/api/viewer/account").json()["name"] == "Public account"
        assert viewer.request.get(media).status == contributor.request.get(media).status == 404
        assert viewer.request.post(base + "/api/team/uploads", headers=TEAM).status == 403
        assert contributor.request.get(base + "/api/library", headers=TEAM).status == 403
        assert contributor.request.get(base + "/api/team/members", headers=TEAM).status == 403
        assert contributor.request.post(base + "/api/team/invitations", headers=TEAM,
                                        data={"email": email("uninvited"), "role": "admin"}).status == 403

        draft_name = "Contributor private draft"
        contributor_page.get_by_label("Private video").set_input_files({
            "name": draft_name + ".mp4", "mimeType": "video/mp4", "buffer": replacement.read_bytes(),
        })
        contributor_page.get_by_role("button", name="Upload to My Library").click()
        expect(contributor_page.locator("#teamStatus")).to_have_text("Private video added to My Library.", timeout=45000)
        owned = contributor.request.get(base + "/api/team/library").json()["works"]
        assert [item["title"] for item in owned] == [draft_name], owned
        assert viewer.request.get(base + owned[0]["watch"] + "/media").status == 404
        assert requests.get(f"{base}/watch/work/{owned[0]['id']}", timeout=10).status_code == 404
        draft_width = playback(contributor_page, draft_name)

        member_row = admin_page.locator("#teamMemberList li").filter(has_text=email("viewer"))
        member_row.get_by_role("button", name="Manage video access").click()
        expect(admin_page.get_by_role("heading", name="Videos shared with Val")).to_be_visible()
        admin_page.get_by_label("Video", exact=True).select_option(label=protected_title)
        admin_page.get_by_role("button", name="Share video", exact=True).click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Video shared.")

        viewer_page.reload()
        viewer_width = playback(viewer_page, protected_title)
        ranged = viewer.request.get(media, headers={"Range": "bytes=0-63"})
        assert ranged.status == 206 and len(ranged.body()) == 64
        assert contributor.request.get(media).status == 404
        forbidden_origin = admin.request.delete(f"{base}/api/team/members/{viewer_id}", headers=TEAM | {
            "Origin": "https://unrelated.example.org",
        }, data={})
        assert forbidden_origin.status == 403 and viewer.request.get(media).ok

        admin_page.get_by_role("button", name=f"Revoke {protected_title}").click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Video access revoked.")
        assert viewer.request.get(media).status == 404
        admin_page.get_by_label("Video", exact=True).select_option(label=protected_title)
        admin_page.get_by_role("button", name="Share video", exact=True).click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Video shared.")
        member_row.get_by_role("button", name="Remove access").click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Team access removed.")
        assert viewer.request.get(media).status == 401
        assert viewer.request.get(base + "/api/team/account").status == 401
        assert viewer.request.post(base + "/api/team/session", headers=TEAM,
                                    data={"email": email("viewer"), "password": PASSWORD}).status == 401
        assert viewer.request.get(base + "/api/viewer/account").ok

        contributor_page.goto(base + "/team")
        contributor_page.get_by_role("button", name="Sign out").click()
        expect(contributor_page.get_by_role("button", name="Sign in to team")).to_be_visible()
        assert contributor.request.get(base + "/api/team/account").status == 401
        contributor_page.get_by_label("Team email").fill(email("contributor"))
        contributor_page.get_by_label("Password").fill(PASSWORD)
        contributor_page.get_by_role("button", name="Sign in to team").click()
        expect(contributor_page.get_by_role("link", name=draft_name)).to_be_visible()

        page.reload()
        admin_row = page.locator("#teamMemberList li").filter(has_text=email("admin"))
        admin_row.get_by_label("Role for Avery").select_option("viewer")
        admin_row.get_by_role("button", name="Save role").click()
        expect(page.locator("#teamMembersStatus")).to_have_text("Member role saved.")
        assert admin.request.get(base + "/api/library", headers=TEAM).status == 403
        assert signed_in(admin)["role"] == "viewer"
        assert admin.request.get(base + "/api/team/members", headers=TEAM).status == 403
        owner_list = requests.get(base + "/api/library", headers=auth, timeout=10)
        owner_list.raise_for_status()
        assert {uid, owned[0]["id"]} <= {item["id"] for item in owner_list.json()}
        audit = requests.get(base + "/api/team/audit", headers=auth, timeout=10).json()["events"]
        assert any(event["action"] == "removed" and event["actor"] == f"team:{admin_id}"
                   and event["subject"] == viewer_id for event in audit)
        assert any(event["action"] == "role_changed" and event["subject"] == admin_id for event in audit)
        assert all("token" not in record for record in audit)

    return dict(single_use_and_rotated_invitations=True, canceled_link_rejected=True, admin_role_applied=True,
                scoped_viewer_video_width=viewer_width, contributor_private_video_width=draft_width,
                existing_audience_identity_separate=True, range_playback=True, forged_origin_denied=True,
                removed_session_denied=True, revoked_grant_denied=True, demoted_admin_denied=True,
                persistent_contributor_sign_in=True, bootstrap_operator_preserved=True, membership_audit=True)
