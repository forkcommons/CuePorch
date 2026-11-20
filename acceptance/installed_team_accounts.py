"""Exercise team invitations and private team media from the installed service."""

import csv
from io import StringIO
from pathlib import Path
import uuid
from urllib.parse import urlsplit

import requests
from playwright.sync_api import expect


TEAM = {"X-CuePorch-Team": "1", "Authorization": "Bearer team-session"}
PASSWORD = "CuePorch team rehearsal password!"


def check_team_accounts(page, browser, base, auth, replacement, migrated):
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
    migrated_watch = f"{base}/watch/work/{migrated['work']}"
    migrated_media = f"{base}/team/video/{migrated['work']}/media"
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
        session = next(cookie for name, cookie in cookies.items() if name.startswith("cueporch-team-"))
        assert session["httpOnly"] and session["sameSite"] == "Lax"
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
        draft_analytics = f"{base}/api/team/video/{owned[0]['id']}/analytics"
        contributor_page.get_by_role("link", name="Video analytics").click()
        report_page = contributor_page.url
        expect(contributor_page.get_by_role("heading", name=f"Analytics for {draft_name}")).to_be_visible()
        expect(contributor_page.get_by_role("link", name="Advanced report")).to_have_count(0)
        expect(contributor_page.get_by_label("Operator credential")).not_to_be_visible()
        expect(contributor_page.get_by_label("Video preview")).to_be_visible()
        expect(contributor_page.locator("#views")).to_have_text("0")
        contributor_page.get_by_label("Playback surface").select_option("embed")
        with contributor_page.expect_response(lambda result: "analytics?" in result.url) as filtered:
            contributor_page.get_by_role("button", name="Apply filters").click()
        assert filtered.value.ok and filtered.value.json()["surface"] == "embed"
        assert "no-store" in filtered.value.headers["cache-control"]
        with contributor_page.expect_download() as analytics_download:
            contributor_page.get_by_role("button", name="Download CSV").click()
        analytics_csv = analytics_download.value.path().read_text(encoding="utf-8")
        assert analytics_csv.splitlines() == ["day,surface,impressions,views,view_rate,finishes,watched_seconds"]
        assert admin.request.get(draft_analytics, headers=TEAM).ok
        assert contributor.request.get(draft_analytics).status == 403
        assert requests.get(draft_analytics, headers=TEAM, timeout=10).status_code == 401
        assert contributor.request.get(draft_analytics, headers=TEAM | {
            "Origin": "https://unrelated.example.org",
        }).status == 403

        admin_page.reload()
        member_row = admin_page.locator("#teamMemberList li").filter(has_text=email("viewer"))
        member_row.get_by_role("button", name="Manage media access").click()
        expect(admin_page.get_by_role("heading", name="Videos shared with Val")).to_be_visible()
        admin_page.get_by_label("Video", exact=True).select_option(label=protected_title)
        admin_page.get_by_role("button", name="Share video", exact=True).click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Video shared.")

        viewer_page.reload()
        viewer_width = playback(viewer_page, protected_title)
        expect(viewer_page.get_by_role("link", name="Video analytics")).to_have_count(0)
        assert viewer.request.get(f"{base}/api/team/video/{uid}/analytics", headers=TEAM).status == 404
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

        # Share the private Vimeo copy without creating a public watch route.
        assert requests.get(migrated_watch, timeout=10).status_code == 404
        assert viewer.request.get(migrated_media).status == 404
        admin_page.get_by_label("Video", exact=True).select_option(value=migrated["work"])
        admin_page.get_by_role("button", name="Share video", exact=True).click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Video shared.")
        viewer_page.goto(base + "/team")
        migrated_width = playback(viewer_page, migrated["title"])
        assert migrated_width == 320
        assert contributor.request.get(migrated_media).status == 404
        assert requests.get(migrated_watch, timeout=10).status_code == 404
        admin_page.get_by_role("button", name=f"Revoke {migrated['title']}").click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Video access revoked.")
        assert viewer.request.get(migrated_media).status == 404

        folders = requests.get(base + "/api/library/folders", headers=auth, timeout=10).json()
        parent = next(folder["id"] for folder in folders if folder["name"] == "Station programs")
        child = next(folder["id"] for folder in folders if folder["parent"] == parent)

        def move_migrated(folder):
            requests.put(base + "/api/library/placement", headers=auth, timeout=10,
                         json={"works": [migrated["work"]], "folder": folder}).raise_for_status()

        move_migrated(child)
        folder_api = f"{base}/api/team/members/{viewer_id}/folders/{parent}"
        assert viewer.request.put(folder_api, headers=TEAM, data={}).status == 403
        admin_page.get_by_label("Folder", exact=True).select_option(value=parent)
        admin_page.get_by_role("button", name="Share folder", exact=True).click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Folder shared.")
        viewer_page.goto(base + "/team")
        inherited_width = playback(viewer_page, migrated["title"])
        assert inherited_width == 320
        assert contributor.request.get(migrated_media).status == 404
        move_migrated(None)
        assert viewer.request.get(migrated_media).status == 404
        move_migrated(child)
        assert viewer.request.get(migrated_media).ok
        admin_page.get_by_role("button", name="Revoke folder Station programs", exact=True).click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Folder access revoked.")
        assert viewer.request.get(migrated_media).status == 404

        groups_api = base + "/api/team/groups"
        assert viewer.request.get(groups_api, headers=TEAM).status == 403
        group_form = admin_page.locator("#teamGroupForm")
        group_form.get_by_label("Group name").fill("Screening reviewers")
        group_member = group_form.get_by_role("checkbox", name=f"Val · {email('viewer')}")
        group_member.check()
        group_folder = group_form.get_by_label("Station programs", exact=True)
        group_folder.select_option("view")
        group_form.get_by_role("button", name="Save group").click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Group saved.")
        group, = admin.request.get(groups_api, headers=TEAM).json()["groups"]
        viewer_page.goto(base + "/team")
        group_width = playback(viewer_page, migrated["title"])
        assert group_width == 320
        assert contributor.request.get(migrated_media).status == 404
        assert requests.get(migrated_watch, timeout=10).status_code == 404
        group_member.uncheck()
        group_form.get_by_role("button", name="Save group").click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Group saved.")
        assert viewer.request.get(migrated_media).status == 404
        group_api = f"{groups_api}/{group['id']}"
        assert admin.request.put(group_api, headers=TEAM, data=group).status == 409
        assert contributor.request.delete(group_api, headers=TEAM, data=group).status == 403

        group_member.check()
        group_form.get_by_role("checkbox", name=f"Cameron · {email('contributor')}").check()
        group_folder.select_option("edit")
        group_form.get_by_role("button", name="Save group").click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Group saved.")
        assert viewer.request.get(migrated_media).ok
        details_path = f"{base}/api/library/work/{migrated['work']}"
        assert viewer.request.get(details_path + "/details", headers=TEAM).status == 404
        contributor_page.goto(base + "/team")
        contributor_page.get_by_role("link", name=migrated["title"], exact=True).click()
        expect(contributor_page.get_by_role("link", name="Video analytics")).to_have_count(0)
        assert contributor.request.get(f"{base}/api/team/video/{migrated['work']}/analytics?format=csv",
                                       headers=TEAM).status == 404
        contributor_page.get_by_role("link", name="Edit video details").click()
        edit_page = contributor_page.url
        contributor_page.get_by_label("Description", exact=True).fill("Screening notes from the assigned contributor.")
        contributor_page.get_by_role("button", name="Save video details").click()
        expect(contributor_page.locator("#detailsStatus")).to_have_text("Video details saved.")
        contributor_page.get_by_label("Editorial status").select_option("Needs review")
        expect(contributor_page.locator("#detailsStatus")).to_have_text("Editorial status saved.")
        assert contributor.request.put(details_path + "/share-link", headers=TEAM, data={"slug": "forged"}).status == 403
        stored = requests.get(details_path + "/details", headers=auth, timeout=10).json()
        assert stored["description"] == "Screening notes from the assigned contributor."
        group_folder.select_option("view")
        group_form.get_by_role("button", name="Save group").click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Group saved.")
        assert contributor.request.get(edit_page).status == 404
        assert contributor.request.put(details_path + "/details", headers=TEAM, data=stored).status == 404
        assert contributor.request.get(migrated_media).ok

        group_form.get_by_role("button", name="Delete group").click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Group deleted.")
        assert viewer.request.get(migrated_media).status == 404

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
        assert contributor.request.get(report_page).status == 401
        assert contributor.request.get(draft_analytics + "?format=csv", headers=TEAM).status == 401
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
        assert {event["action"] for event in audit if event["subject"] == viewer_id
                and event["detail"] == migrated["work"]} == {"video_shared", "video_unshared"}
        assert {event["action"] for event in audit if event["subject"] == viewer_id
                and event["detail"] == parent} == {"folder_shared", "folder_unshared"}
        assert {event["action"] for event in audit if event["subject"] == group["id"]} == {
            "group_created", "group_updated", "group_removed",
        }
        assert all("token" not in record for record in audit)

        successor = join(viewer, invite(page, email("owner-successor"), "admin"), "=North Arts", "Administrator")
        successor_id = signed_in(viewer)["id"]
        pending = invite(page, email("roster-pending"), "viewer")
        ownership_path = base + "/api/team/owner/"
        assert viewer.request.put(ownership_path + successor_id, headers=TEAM, data={"previous": None}).status == 403

        page.get_by_role("button", name="Make Avery owner").click()
        expect(page.locator("#teamMembersStatus")).to_have_text("Ownership updated.")
        assert signed_in(admin)["role"] == "owner"
        assert admin.request.get(base + "/api/library", headers=TEAM).ok
        assert viewer.request.delete(base + "/api/team/members/" + admin_id, headers=TEAM, data={}).status == 409
        assert viewer.request.put(base + "/api/team/members/" + admin_id, headers=TEAM, data={"role": "viewer"}).status == 409
        assert requests.put(ownership_path + successor_id, headers=TEAM | auth, json={"previous": None},
                            timeout=10).status_code == 409

        admin_page.goto(base + "/team")
        expect(admin_page.locator("#teamIdentity")).to_have_text("Avery · Owner")
        admin_page.get_by_role("link", name="Team members").click()
        admin_page.get_by_role("button", name="Make =North Arts owner").click()
        expect(admin_page.locator("#teamMembersStatus")).to_have_text("Ownership updated.")
        assert signed_in(admin)["role"] == "admin" and signed_in(viewer)["role"] == "owner"
        assert admin.request.put(ownership_path + admin_id, headers=TEAM, data={"previous": successor_id}).status == 403
        successor.goto(base + "/team")
        expect(successor.locator("#teamIdentity")).to_have_text("=North Arts · Owner")
        successor.get_by_role("link", name="Team members").click()
        with successor.expect_download() as downloaded:
            successor.get_by_role("button", name="Download team roster").click()
        receipt = downloaded.value
        assert receipt.suggested_filename == "cueporch-team-roster.csv"
        content = Path(receipt.path()).read_text(encoding="utf8")
        rows = {row["Email"]: row for row in csv.DictReader(StringIO(content))}
        assert rows[email("roster-pending")]["Status"] == "Pending"
        assert rows[email("viewer")]["Status"] == "Removed"
        assert rows[email("owner-successor")]["Role"] == "owner" and rows[email("owner-successor")]["Name"] == "'=North Arts"
        assert rows[email("admin")]["Role"] == "admin" and all(row["Date added (UTC)"] for row in rows.values())
        assert urlsplit(pending).fragment not in content and PASSWORD not in content
        assert contributor.request.get(base + "/api/team/roster.csv", headers=TEAM).status == 403
        audit = requests.get(base + "/api/team/audit", headers=auth, timeout=10).json()["events"]
        assert any(event["action"] == "owner_changed" and event["subject"] == successor_id for event in audit)
        assert any(event["action"] == "roster_exported" and event["actor"] == f"team:{successor_id}" for event in audit)

        named_api = base + "/api/team/analytics"
        successor.get_by_role("link", name="Team analytics").click()
        named_tracking = successor.get_by_label("Record named viewing activity")
        expect(named_tracking).to_be_visible()
        expect(named_tracking).not_to_be_checked()
        assert viewer.request.get(named_api, headers=TEAM).status == 403  # This context now belongs to the Owner.
        assert contributor.request.put(named_api + "/settings", headers=TEAM, data={"enabled": True}).status == 403
        contributor_page.goto(base + owned[0]["watch"])
        assert contributor_page.locator("video").get_attribute("data-analytics") is None

        def save_named_tracking(enable):
            named_tracking.set_checked(enable)
            successor.get_by_role("button", name="Save viewing privacy").click()
            expect(successor.locator("#teamAnalyticsStatus")).to_have_text("Viewing privacy saved.")

        save_named_tracking(True)
        contributor_page.reload()
        expect(contributor_page.get_by_text("This workspace records viewing activity for signed-in team members.")).to_be_visible()
        player = contributor_page.locator("video")
        observation = base + player.get_attribute("data-analytics")
        with contributor_page.expect_response(lambda response: response.url == observation and
                                               response.request.post_data_json.get("completed")) as ended:
            player.evaluate("v => { v.muted = true; return v.play(); }")
        assert ended.value.status == 204
        assert admin.request.post(observation, data=ended.value.request.post_data_json).status == 404
        assert contributor.request.get(named_api + "?format=csv", headers=TEAM).status == 403
        assert requests.get(named_api, timeout=10).status_code == 403

        opt_out = browser.new_context(extra_http_headers={"DNT": "1", "Sec-GPC": "1"})
        try:
            opt_out.request.post(base + "/api/team/session", headers=TEAM,
                                 data={"email": email("contributor"), "password": PASSWORD})
            private_view = opt_out.new_page()
            private_view.goto(base + owned[0]["watch"])
            private_player = private_view.locator("video")
            assert private_player.get_attribute("data-analytics") is None
            private_player.evaluate("v => { v.muted = true; return v.play(); }")
            private_view.wait_for_function("document.querySelector('video').ended")
            assert opt_out.request.post(observation, data=ended.value.request.post_data_json).status == 204
        finally:
            opt_out.close()

        successor.get_by_role("button", name="Apply filters").click()
        expect(successor.locator("#teamAnalyticsTotals")).to_have_text("Members 1 · Videos 1 · Views 1")
        named_row = successor.locator("#teamAnalyticsRows tr")
        expect(named_row).to_have_count(1)
        expect(named_row.get_by_role("cell").nth(3)).to_have_text("1")
        named_row.get_by_role("button", name=draft_name).click()
        expect(successor.get_by_role("button", name="Clear member or video filter")).to_be_visible()
        with successor.expect_download() as named_download:
            successor.get_by_role("button", name="Download CSV").click()
        named_rows = list(csv.DictReader(StringIO(named_download.value.path().read_text(encoding="utf-8"))))
        assert len(named_rows) == 1 and named_rows[0]["Email"] == email("contributor")
        assert named_rows[0]["Video"] == draft_name and float(named_rows[0]["Time watched (seconds)"]) > 0

        save_named_tracking(False)
        assert viewer.request.get(named_api, headers=TEAM).status == 403
        assert contributor.request.post(observation, data=ended.value.request.post_data_json).status == 404
        save_named_tracking(True)
        assert contributor.request.post(observation, data=ended.value.request.post_data_json).status == 404
        expect(successor.locator("#teamAnalyticsTotals")).to_have_text("Members 1 · Videos 1 · Views 1")
        save_named_tracking(False)

    return dict(single_use_and_rotated_invitations=True, canceled_link_rejected=True, admin_role_applied=True,
                scoped_viewer_video_width=viewer_width, contributor_private_video_width=draft_width,
                migrated_private_video_width=migrated_width, migrated_grant_revoked=True,
                inherited_folder_playback_width=inherited_width, folder_move_and_revocation_enforced=True,
                group_folder_playback_width=group_width, group_revocation_and_stale_edit_enforced=True,
                scoped_contributor_details_and_editorial_edit=True, edit_downgrade_preserved_playback=True,
                existing_audience_identity_separate=True, range_playback=True, forged_origin_denied=True,
                removed_session_denied=True, revoked_grant_denied=True, demoted_admin_denied=True,
                persistent_contributor_sign_in=True, bootstrap_operator_preserved=True, membership_audit=True,
                owner_assignment_and_transfer_with_stale_check=True, admin_cannot_take_ownership=True,
                private_audited_roster_csv=True, private_contributor_video_report_and_export=True,
                report_requires_current_ownership_and_session=True,
                named_playback_report_scope_opt_out_and_csv=True), email("contributor")
