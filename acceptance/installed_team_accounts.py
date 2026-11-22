"""Exercise team invitations and private team media from the installed service."""

import csv
import json
from io import StringIO
from pathlib import Path
import uuid
from urllib.parse import urlsplit

import requests
from playwright.sync_api import expect
from installed_authenticator import check_authenticator


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
        authentication = check_authenticator(viewer_page, browser, base, "team", email("viewer"), PASSWORD)
        changes = requests.get(base + "/api/team/audit", headers=auth, timeout=10).json()["events"]
        assert {"authenticator_enabled", "authenticator_disabled"} <= {
            row["action"] for row in changes if row["subject"] == viewer_id
        }
        assert signed_in(contributor)["email"] == email("contributor")
        assert viewer.request.get(base + "/api/viewer/account").json()["name"] == "Public account"
        assert viewer.request.get(media).status == contributor.request.get(media).status == 404
        assert viewer.request.post(base + "/api/team/uploads", headers=TEAM).status == 403
        assert viewer.request.get(base + "/team/profile").status == 403
        assert viewer.request.get(base + "/api/team/profile").status == 403
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
        completion_column = successor.get_by_role("columnheader", name="Completions", exact=True).evaluate("cell => cell.cellIndex")
        expect(named_row.get_by_role("cell").nth(completion_column)).to_have_text("1")
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

        contributor_id = signed_in(contributor)["id"]
        role_path = f"{base}/api/team/members/{contributor_id}"
        presentation_path = f"{base}/api/library/work/{owned[0]['id']}/presentation"
        assert contributor.request.get(presentation_path, headers=TEAM).status == 404
        successor.goto(base + "/team/members")
        contributor_row = successor.locator("#teamMemberList li").filter(has_text=email("contributor"))
        contributor_row.get_by_label("Role for Cameron").select_option("contributor_plus")
        contributor_row.get_by_role("button", name="Save role").click()
        expect(successor.locator("#teamMembersStatus")).to_have_text("Member role saved.")
        assert signed_in(contributor)["role"] == "contributor_plus"
        assert contributor.request.get(f"{base}/api/library/work/{migrated['work']}/presentation",
                                       headers=TEAM).status == 404
        assert contributor.request.get(base + "/api/library", headers=TEAM).status == 403
        contributor_page.goto(base + owned[0]["watch"])
        contributor_page.get_by_role("link", name="Edit video interactivity").click()
        interactive_page = contributor_page.url
        expect(contributor_page.get_by_role("heading", name=f"Interactivity for {draft_name}")).to_be_visible()
        assert contributor_page.get_by_label("Video preview").evaluate(
            "async v => {v.muted = true; await v.play(); v.pause(); return v.videoWidth;}"
        ) > 0
        expect(contributor_page.get_by_label("Operator credential")).not_to_be_visible()
        expect(contributor_page.get_by_role("heading", name="Video rating")).to_have_count(0)
        contributor_page.get_by_role("button", name="Add chapter").click()
        contributor_page.locator("#chapterRows").get_by_label("Title").fill("Artist introduction")
        contributor_page.get_by_role("button", name="Save chapters").click()
        expect(contributor_page.locator("#chapterStatus")).to_have_text("Chapters saved.")
        contributor_page.get_by_role("button", name="Add card").click()
        contributor_page.get_by_label("Card title").fill("Gallery program")
        contributor_page.get_by_label("Card link").fill("https://gallery.example.org/program")
        contributor_page.get_by_label("After playback").select_option("message")
        contributor_page.get_by_label("End message").fill("Thanks for watching.")
        contributor_page.get_by_role("button", name="Save cards and end screen").click()
        expect(contributor_page.locator("#presentationStatus")).to_have_text("Cards and end screen saved.")
        operator_read = requests.get(presentation_path, headers=auth, timeout=10)
        operator_read.raise_for_status()
        saved_presentation = operator_read.json()
        stored_card, = saved_presentation["settings"]["cards"]
        assert stored_card["title"] == "Gallery program"
        logo = requests.post(base + "/api/review-logos", headers=auth, timeout=10,
                             files={"logo": ("staff.png", bytes.fromhex("89504e470d0a1a0a") + b"staff fixture",
                                             "image/png")})
        assert logo.status_code == 201
        settings = saved_presentation["settings"]
        foreign_art = dict(version=saved_presentation["version"], previous=settings,
                           cards=[stored_card | {"image": logo.json()["logo"]}], ending=settings["ending"])
        assert contributor.request.put(presentation_path, headers=TEAM, data=foreign_art).status == 403
        assert viewer.request.put(role_path, headers=TEAM, data={"role": "contributor"}).ok
        assert contributor.request.get(interactive_page).status == 404
        assert contributor.request.get(presentation_path, headers=TEAM).status == 404
        assert contributor.request.get(base + owned[0]["watch"] + "/media").ok

        second_title = "Arts program excerpt"
        second_upload = contributor.request.post(base + "/api/team/uploads", headers=TEAM, multipart={
            "file": {"name": second_title + ".mp4", "mimeType": "video/mp4", "buffer": replacement.read_bytes()},
            "upload_id": uuid.uuid4().hex,
        })
        assert second_upload.status == 201, second_upload.text()
        second_work = second_upload.json()["id"]
        profile_api = base + "/api/team/profile"
        assert requests.get(profile_api, timeout=10).status_code == 401
        contributor_page.goto(base + "/team")
        contributor_page.get_by_role("link", name="Edit creator profile").click()
        contributor_page.get_by_label("Creator name").fill("Cameron · Local arts")
        handle = "local-arts-" + label
        contributor_page.get_by_label("Profile handle").fill(handle)
        contributor_page.get_by_label("Biography").fill("Short films and local exhibitions. <section id=unsafe>")
        contributor_page.get_by_label("Profile links, one per line").fill("https://gallery.example.org/portfolio")
        contributor_page.get_by_label("Cover video").select_option(owned[0]["id"])
        for video_id in (owned[0]["id"], second_work):
            contributor_page.get_by_label("Add a video").select_option(video_id)
            contributor_page.get_by_role("button", name="Add featured video").click()
        contributor_page.get_by_role("button", name=f"Move up {second_title}").click()
        contributor_page.get_by_role("button", name="Save creator profile").click()
        expect(contributor_page.locator("#creatorStatus")).to_have_text("Creator profile saved.")
        profile_url = f"{base}/creators/{handle}"
        assert requests.get(profile_url, timeout=10).status_code == 404
        draft_profile = contributor.request.get(profile_api).json()
        assert draft_profile["featured"] == [second_work, owned[0]["id"]]
        assert contributor.request.put(profile_api, headers=TEAM,
                                       data=draft_profile | {"cover": migrated["work"]}).status == 422
        assert contributor.request.put(profile_api, headers=TEAM,
                                       data=draft_profile | {"links": ["javascript:alert(1)"]}).status == 422
        assert contributor.request.put(profile_api, headers=TEAM, data=draft_profile | {"revision": ""}).status == 409
        contributor_page.get_by_label("Publish profile").check()
        contributor_page.get_by_role("button", name="Save creator profile").click()
        expect(contributor_page.get_by_role("link", name="Open public profile")).to_be_visible()
        public_settings = dict(visibility="public", embed="anywhere", domains=[], download=False)
        private_settings = dict(visibility="private", embed="disabled", domains=[], download=False)

        def privacy(work, settings):
            endpoint = f"{base}/api/library/work/{work}/privacy"
            requests.put(endpoint, headers=auth, timeout=15, json=settings).raise_for_status()

        privacy(second_work, public_settings)
        privacy(owned[0]["id"], public_settings)
        with browser.new_context() as guest:
            profile_visitor = guest.new_page()
            profile_visitor.goto(profile_url)
            expect(profile_visitor.get_by_role("heading", name="Cameron · Local arts")).to_be_visible()
            expect(profile_visitor.get_by_text("Short films and local exhibitions.", exact=False)).to_be_visible()
            expect(profile_visitor.locator("#unsafe")).to_have_count(0)
            assert profile_visitor.get_by_text("https://gallery.example.org/portfolio").get_attribute("href") == (
                "https://gallery.example.org/portfolio"
            )
            cards = profile_visitor.get_by_role("region", name="Featured videos").get_by_role("link")
            expect(cards).to_have_text([second_title, draft_name])
            profile_visitor.get_by_role("link", name="Sign in to follow").click()
            signup = profile_visitor.locator("#viewerSignUp")
            signup.get_by_label("Display name").fill("Arts audience")
            signup.get_by_label("Email").fill(email("arts-audience"))
            signup.get_by_label("Password").fill(PASSWORD)
            signup.get_by_role("button", name="Create account").click()
            expect(profile_visitor).to_have_url(profile_url)
            profile_visitor.get_by_role("button", name="Follow creator", exact=True).click()
            expect(profile_visitor.get_by_role("button", name="Unfollow creator")).to_be_visible()
            expect(profile_visitor.locator("[data-follower-count]")).to_have_text("1 follower")
            follow_path = f"{base}/api/viewer/follow/{handle}"
            assert viewer.request.get(follow_path).json()["following"] is False
            assert viewer.request.put(follow_path, headers={
                "Origin": "https://unrelated.example.org", "X-CuePorch-Viewer": "1",
            }, data={}).status == 403
            assert requests.get(follow_path, timeout=10).status_code == 401
            feed = guest.new_page()
            profile_visitor.get_by_role("link", name="Feed", exact=True).click()
            expect(profile_visitor.locator(".watch-card")).to_have_text([draft_name, second_title])
            feed.goto(base + "/watch/feed")
            expect(feed.locator(".watch-card")).to_have_text([draft_name, second_title])
            maturity = f"{base}/api/library/work/{second_work}/details"
            metadata = requests.get(maturity, headers=auth, timeout=15)
            metadata.raise_for_status()
            requests.put(maturity, headers=auth, timeout=15,
                         json=dict(metadata.json(), rating="mature", content=["language"])).raise_for_status()
            viewing = base + "/api/viewer/preferences"
            marker = {"X-CuePorch-Viewer": "1"}
            assert guest.request.put(viewing, headers=marker, data={"mode": "hide_mature"}).ok
            feed.reload()
            expect(feed.locator(".watch-card")).to_have_text([draft_name])
            assert guest.request.put(viewing, headers=marker, data={"mode": "all"}).ok
            feed.reload()
            expect(feed.locator(".watch-card")).to_have_text([draft_name, second_title])
            requests.put(maturity, headers=auth, timeout=15, json=metadata.json()).raise_for_status()
            independent_feed = viewer.new_page()
            independent_feed.goto(base + "/watch/feed")
            expect(independent_feed.get_by_text("No public videos from creators you follow.")).to_be_visible()

            profile_visitor.goto(profile_url)
            profile_visitor.get_by_role("region", name="Cover video").get_by_role("link", name=draft_name).click()
            assert profile_visitor.locator("video").evaluate(
                "async v => {v.muted = true; await v.play(); return v.videoWidth;}"
            ) > 0
            expect(profile_visitor.get_by_role("button", name="Unfollow creator")).to_be_visible()
            profile_visitor.get_by_role("link", name="Cameron · Local arts").click()
            assert profile_visitor.url == profile_url
            privacy(owned[0]["id"], private_settings)
            profile_visitor.reload()
            expect(profile_visitor.get_by_role("region", name="Cover video")).to_have_count(0)
            expect(cards).to_have_text([second_title])
            feed.reload()
            expect(feed.locator(".watch-card")).to_have_text([second_title])
            profile_visitor.get_by_role("button", name="Unfollow creator").click()
            expect(profile_visitor.locator("[data-follower-count]")).to_have_text("0 followers")
            feed.reload()
            expect(feed.get_by_text("No public videos from creators you follow.")).to_be_visible()
            profile_visitor.get_by_role("button", name="Follow creator", exact=True).click()
            expect(profile_visitor.locator("[data-follower-count]")).to_have_text("1 follower")
            assert guest.request.delete(base + "/api/viewer/account", headers={"X-CuePorch-Viewer": "1"},
                                        data={"password": PASSWORD}).status == 204
            profile_visitor.reload()
            expect(profile_visitor.locator("[data-follower-count]")).to_have_text("0 followers")

            assert viewer.request.put(follow_path, headers={"X-CuePorch-Viewer": "1"}, data={}).ok
            contributor_page.get_by_label("Publish profile").uncheck()
            contributor_page.get_by_role("button", name="Save creator profile").click()
            expect(contributor_page.get_by_role("link", name="Open public profile")).not_to_be_visible()
            assert requests.get(profile_url, timeout=10).status_code == 404
            assert requests.get(f"{base}/watch/work/{second_work}", timeout=10).ok
            independent_feed.reload()
            expect(independent_feed.get_by_text("No public videos from creators you follow.")).to_be_visible()
        privacy(second_work, private_settings)
        sharing_restrictions = check_sharing_restrictions(
            base, auth, admin, contributor_page, successor, second_work, owned[0]["id"], uid, parent, replacement,
        )
        support = check_support_requests(base, auth, contributor_page, successor)
        updates = check_workspace_updates(base, contributor_page, successor)

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
                named_playback_report_scope_opt_out_and_csv=True,
                contributor_plus_scoped_interactivity_and_demotion=True,
                public_creator_profile_ordering_playback_and_withdrawal=True,
                private_feed_follows_publication_order_and_account_removal=True,
                feed_observes_viewer_rating_preference=True, authenticator=authentication,
                sharing_restrictions=sharing_restrictions, support_requests=support, workspace_updates=updates), email("contributor")


def check_sharing_restrictions(base, auth, admin, contributor, owner, own, pending, foreign, folder, sample):
    policy_path = base + "/api/team/sharing-policy"
    owner_headers = auth | {"X-CuePorch-Team": "1"}
    initial = requests.get(policy_path, headers=auth, timeout=10)
    initial.raise_for_status()
    assert admin.request.get(policy_path, headers=TEAM).status == 403
    assert contributor.request.get(policy_path, headers=TEAM).status == 403
    own_privacy = f"{base}/api/library/work/{own}/privacy"
    foreign_privacy = f"{base}/api/library/work/{foreign}/privacy"
    assert contributor.request.get(own_privacy, headers=TEAM).status == 403
    assert contributor.request.get(foreign_privacy, headers=TEAM).status == 404
    owner.goto(base + "/team/sharing-policy")
    editor = owner.locator("#sharingPolicy")
    expect(editor).to_be_visible()
    admin_options = editor.locator("[data-role=admin]")
    contributor_options = editor.locator("[data-role=contributor]")
    admin_options.get_by_label("Update embed settings").uncheck()
    admin_options.get_by_label("Publish to third-party destinations").uncheck()
    admin_options.get_by_label("Manage direct video file links").uncheck()
    contributor_options.get_by_label("Unlisted", exact=True).check()
    owner.get_by_role("button", name="Save sharing restrictions").click()
    expect(owner.locator("#sharingPolicyStatus")).to_have_text("Sharing restrictions saved.")
    assert requests.put(policy_path, headers=owner_headers, json=initial.json(), timeout=10).status_code == 409
    assert admin.request.put(policy_path, headers=TEAM, data=initial.json()).status == 403

    public = dict(visibility="public", embed="anywhere", domains=[], download=False)
    unlisted = dict(visibility="unlisted", embed="disabled", domains=[], download=False)
    assert contributor.request.put(own_privacy, headers=TEAM, data=public).status == 403
    assert contributor.request.put(foreign_privacy, headers=TEAM, data=unlisted).status == 404
    assert admin.request.put(foreign_privacy, headers=TEAM, data=public).status == 403
    assert admin.request.put(base + "/api/library/upload-defaults", headers=TEAM, data=public).status == 403
    assert admin.request.put(f"{base}/api/library/folders/{folder}/privacy", headers=TEAM,
                             data=public | {"enabled": True}).status == 403
    assert admin.request.get(f"{base}/api/library/work/{foreign}/file-links", headers=TEAM).status == 403
    assert admin.request.post(f"{base}/api/delivery/{foreign}", headers=TEAM, data={"public": True}).status == 403
    assert admin.request.post(base + "/api/migration/publish-eligible", headers=TEAM, data={}).status == 403
    assert admin.request.put(base + "/api/routes/channel/workspace-program", headers=TEAM, data={
        "revision": "one", "destination": "https://program.example.test/videos/embed/clip",
        "public_authorized": True,
    }).status == 403

    contributor.goto(f"{base}/team/video/{own}/details")
    privacy = contributor.get_by_role("region", name="Video sharing")
    select = privacy.get_by_label("Link privacy")
    expect(select.locator("option[value=public]")).to_be_disabled()
    select.select_option("unlisted")
    expect(privacy.get_by_label("Embedding")).to_be_disabled()
    privacy.get_by_role("button", name="Save privacy").click()
    expect(privacy.get_by_role("status")).to_have_text("Video sharing saved.")
    watch = privacy.get_by_label("Viewer link").input_value()
    assert requests.get(watch, timeout=10).ok
    assert requests.get(f"{base}/watch/work/{own}", timeout=10).status_code == 404
    assert contributor.request.put(own_privacy, headers=TEAM | {"Origin": "https://outside.example.test"},
                                   data=unlisted).status == 403

    def put(path, settings):
        response = requests.put(path, headers=auth, json=settings, timeout=10)
        response.raise_for_status()
        return response.json() if response.content else None

    put(foreign_privacy, public)
    assert requests.get(f"{base}/watch/work/{foreign}", timeout=10).ok
    put(base + "/api/policy/publication", {"required": True})
    pending_path = f"{base}/api/library/work/{pending}/privacy"
    assert put(pending_path, public)["pending"]["visibility"] == "public"
    admin_options.get_by_label("Public", exact=True).uncheck()
    owner.get_by_role("button", name="Save sharing restrictions").click()
    expect(owner.locator("#sharingPolicyStatus")).to_have_text("Sharing restrictions saved. Videos made private: 1.")
    put(base + "/api/policy/publication", {"required": False})
    for uid in (foreign, pending):
        assert requests.get(f"{base}/watch/work/{uid}", timeout=10).status_code == 404
    assert requests.get(pending_path, headers=auth, timeout=10).json()["pending"] is None
    assert admin.request.put(foreign_privacy, headers=TEAM,
                             data=public | {"embed": "disabled"}).status == 403
    assert admin.request.put(base + "/api/library/privacy", headers=TEAM,
                             data=public | {"works": [foreign], "embed": "disabled"}).status == 403
    assert admin.request.post(base + "/api/library/uploads", headers=TEAM, multipart={
        "file": {"name": "Policy blocked upload.mp4", "mimeType": "video/mp4", "buffer": sample.read_bytes()},
        "upload_id": uuid.uuid4().hex,
        "privacy": json.dumps(public),
    }).status == 403
    assert requests.get(watch, timeout=10).ok

    # Reset only the fixture workspace's rule so the independent restore checks retain their setup.
    current = requests.get(policy_path, headers=auth, timeout=10).json()
    restored = requests.put(policy_path, headers=owner_headers,
                            json={"revision": current["revision"], "rules": initial.json()["rules"]}, timeout=10)
    restored.raise_for_status()
    assert admin.request.get(f"{base}/api/library/work/{foreign}/file-links", headers=TEAM).ok
    return dict(owner_policy_ui_and_stale_write=True, scoped_contributor_link_and_embed_rules=True,
                single_bulk_folder_and_upload_denials=True, third_party_routes_denied=True,
                restricted_current_and_pending_sharing_withdrawn=True, direct_file_management_restricted=True)


def check_support_requests(base, auth, contributor, owner):
    endpoint = base + "/api/team/support/requests"
    hidden_ticket = requests.post(endpoint, headers=TEAM | auth, timeout=10,
                                  json={"subject": "Second account request", "message": "Private operator only."})
    assert hidden_ticket.status_code == 201
    hidden_path = endpoint + "/" + hidden_ticket.json()["id"]
    assert contributor.request.get(hidden_path, headers=TEAM).status == 404
    assert contributor.request.get(endpoint + "?view=inbox", headers=TEAM).status == 403
    assert contributor.request.get(endpoint, headers=TEAM).json()["requests"] == []
    assert contributor.request.post(endpoint, headers=TEAM | {"Origin": "https://foreign.example.test"},
                                    data={"subject": "Incorrect origin", "message": "Denied."}).status == 403

    contributor.goto(base + "/team")
    contributor.get_by_role("link", name="Help center").click()
    expect(contributor.get_by_role("heading", name="Popular searches")).to_be_visible()
    contributor.get_by_role("link", name="captions", exact=True).click()
    expect(contributor.get_by_role("link", name="Captions and subtitles")).to_be_visible()
    contributor.get_by_role("searchbox", name="Search help").fill("no-such-topic")
    contributor.get_by_role("button", name="Search", exact=True).click()
    expect(contributor.get_by_text("No help topics match your search.")).to_be_visible()
    contributor.get_by_role("link", name="Requests", exact=True).click()
    expect(contributor.get_by_text("No requests yet.")).to_be_visible()
    contributor.get_by_text("New request", exact=True).click()
    topic = "Caption questions for tonight's screening"
    message = "Is <em>this wording</em> stored as text?"
    contributor.get_by_label("Subject", exact=True).fill(topic)
    contributor.get_by_label("Describe the issue").fill(message)
    contributor.get_by_role("button", name="Send request").click()
    expect(contributor.locator("#helpStatus")).to_have_text("Request sent.")
    expect(contributor.get_by_role("heading", name=topic)).to_be_visible()
    expect(contributor.get_by_text(message, exact=True)).to_be_visible()
    assert contributor.locator("#helpHistory em").count() == 0
    path = contributor.url
    uid = urlsplit(path).query.split("request=")[1]
    ticket = endpoint + "/" + uid
    cache = contributor.request.get(ticket, headers=TEAM).headers["cache-control"]
    assert "no-store" in cache and "private" in cache
    assert requests.get(ticket, timeout=10).status_code == 403
    initial = contributor.request.get(ticket, headers=TEAM).json()
    assert contributor.request.put(ticket, headers=TEAM,
                                   data={"state": "resolved", "revision": initial["revision"]}).status == 403

    owner.goto(base + "/help/requests?team=1")
    assert owner.request.get(endpoint, headers=TEAM).json()["can_manage"]
    expect(owner.locator("#helpWorkspace")).to_be_visible()
    owner.get_by_role("combobox", name="Request view").select_option("inbox")
    owner.get_by_role("button", name=topic, exact=True).click()
    expect(owner.get_by_text(message, exact=True)).to_be_visible()
    owner.get_by_label("Reply", exact=True).fill("Use the reviewed subtitles before the show.")
    owner.get_by_role("button", name="Send reply").click()
    expect(owner.locator("#helpStatus")).to_have_text("Reply sent.")
    assert owner.request.put(ticket, headers=TEAM,
                             data={"state": "resolved", "revision": initial["revision"]}).status == 409
    owner.get_by_role("button", name="Resolve request").click()
    expect(owner.locator("#helpStatus")).to_have_text("Request resolved.")

    contributor.goto(path)
    expect(contributor.locator("#helpState")).to_have_text("Resolved")
    expect(contributor.get_by_text("Use the reviewed subtitles before the show.", exact=True)).to_be_visible()
    expect(contributor.get_by_role("button", name="Reopen request")).to_have_count(0)
    contributor.get_by_label("Reply", exact=True).fill("One more detail about the translated track.")
    contributor.get_by_role("button", name="Send reply").click()
    expect(contributor.locator("#helpStatus")).to_have_text("Reply sent.")
    expect(contributor.locator("#helpState")).to_have_text("Open")
    expect(contributor.get_by_text("You · Request reopened", exact=True)).to_be_visible()
    return dict(searched_help=True, member_scope_and_private_cache=True, owner_reply_and_resolve=True,
                stale_update_denied=True, durable_thread_and_reopen=True)


def check_workspace_updates(base, contributor, owner):
    hub = base + "/whats-new"
    endpoint = base + "/api/team/updates"
    assert contributor.request.get(endpoint, headers=TEAM).status == 403
    contributor.goto(base + "/team")
    contributor.get_by_role("link", name="What's New", exact=True).click()
    expect(contributor.get_by_text("No released announcements match.")).to_be_visible()
    expect(contributor.get_by_role("link", name="Manage announcements")).to_be_hidden()
    contributor.get_by_role("link", name="Up next").click()
    expect(contributor.get_by_role("link", name="Vimeo account move")).to_be_visible()
    contributor.get_by_role("searchbox", name="Search announcements").fill("equirectangular")
    contributor.get_by_role("combobox", name="Product area").select_option("Playback")
    contributor.get_by_role("combobox", name="Category").select_option("New feature")
    contributor.get_by_role("button", name="Filter").click()
    expect(contributor.get_by_role("link", name="Vimeo account move")).to_have_count(0)
    expect(contributor.get_by_role("link", name="Panoramic playback")).to_be_visible()

    owner.goto(hub + "?team=1")
    owner.get_by_role("link", name="Manage announcements").click()
    owner.get_by_role("button", name="New announcement").click()
    slug = "screening-rehearsal-note"
    title = "Screening rehearsal update"
    entry = endpoint + "/" + slug
    public = hub + "/" + slug
    owner.get_by_label("Link name").fill(slug)
    form = owner.locator("#editAnnouncement")
    form.get_by_label("Title", exact=True).fill(title)
    form.get_by_label("Announcement", exact=True).fill("The new community program has a dated rehearsal note.")
    form.get_by_label("Announcement date").fill("2026-11-20")
    form.get_by_label("Product area").select_option("Community")
    form.get_by_label("Category").select_option("Improvement")
    form.get_by_role("button", name="Save announcement").click()
    expect(owner.locator("#updateSaveStatus")).to_have_text("Announcement saved.")
    assert requests.get(public, timeout=10).status_code == 404
    original = owner.request.get(entry, headers=TEAM).json()
    assert original["state"] == "draft"
    form.get_by_label("Publication state").select_option("up-next")
    form.get_by_role("button", name="Save announcement").click()
    expect(owner.locator("#updateSaveStatus")).to_have_text("Announcement saved.")
    assert requests.get(public, timeout=10).ok
    assert requests.get(hub + "?view=up-next&area=Community&category=Improvement", timeout=10).text.count(title) == 1
    assert owner.request.put(entry, headers=TEAM, data=original).status == 409
    form.get_by_label("Publication state").select_option("released")
    form.get_by_role("button", name="Save announcement").click()
    expect(owner.locator("#updateSaveStatus")).to_have_text("Announcement saved.")
    assert requests.get(public, timeout=10).ok
    assert title in requests.get(hub + "?view=released", timeout=10).text
    assert title not in requests.get(hub + "?view=up-next", timeout=10).text
    assert contributor.request.put(entry, headers=TEAM, data=original).status == 403

    contributor.goto(public + "?team=1")
    expect(contributor.get_by_role("heading", name=title)).to_be_visible()
    expect(contributor.locator("article time")).to_have_attribute("datetime", "2026-11-20")
    up = contributor.get_by_role("button", name="Thumbs up")
    up.click()
    expect(up).to_have_attribute("aria-pressed", "true")
    assert contributor.request.put(entry + "/feedback", headers=TEAM, data={"vote": "up"}).ok
    assert owner.request.get(entry, headers=TEAM).json()["feedback"] == {"up": 1}
    assert owner.request.put(entry + "/feedback", headers=TEAM, data={"vote": "down"}).ok
    contributor.reload()
    expect(up).to_have_attribute("aria-pressed", "true")
    up.click()
    expect(up).to_have_attribute("aria-pressed", "false")
    assert owner.request.get(entry, headers=TEAM).json()["feedback"] == {"down": 1}
    assert requests.put(entry + "/feedback", headers={"X-CuePorch-Team": "1"},
                        json={"vote": "up"}, timeout=10).status_code == 403
    contributor.get_by_role("link", name="Send feedback").click()
    expect(contributor.get_by_label("Subject", exact=True)).to_have_value("What's New feedback")
    form.get_by_label("Publication state").select_option("draft")
    form.get_by_role("button", name="Save announcement").click()
    expect(owner.locator("#updateSaveStatus")).to_have_text("Announcement saved.")
    assert requests.get(public, timeout=10).status_code == 404
    assert contributor.request.get(entry + "/feedback", headers=TEAM).status == 404
    return dict(filtered_and_dated_entries=True, authorized_draft_and_release=True,
                stable_link_and_draft_withdrawal=True, duplicate_vote_and_own_removal=True, support_feedback_link=True)
