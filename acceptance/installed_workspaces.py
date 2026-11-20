"""Verify two running workspaces in the same browser and library recovery rehearsal."""

from urllib.parse import urlsplit

import requests
from playwright.sync_api import expect

from installed_team_accounts import PASSWORD, TEAM


def check_workspaces(page, browser, first, first_auth, second, second_auth, email, private_work, group):
    def invite(base, auth, address, role):
        response = requests.post(base + "/api/team/invitations", headers=TEAM | auth, timeout=10,
                                 json={"email": address, "role": role})
        response.raise_for_status()
        return urlsplit(response.json()["path"]).fragment

    def join(context, base, token, name):
        response = context.request.post(base + "/api/team/join", headers=TEAM,
                                        data={"invitation": token, "name": name, "password": PASSWORD})
        assert response.ok, response.status
        return response.json()["id"]

    assert requests.get(second + "/api/library", headers=first_auth, timeout=10).status_code == 403
    assert requests.get(first + "/api/library", headers=second_auth, timeout=10).status_code == 403
    response = requests.get(second + "/api/library", headers=second_auth, timeout=10)
    response.raise_for_status()
    assert response.json() == []
    second_invitation = invite(second, second_auth, email, "viewer")
    visitor_invitation = invite(first, first_auth, "independent-observer@example.org", "viewer")

    page.goto(first + "/team/members")
    page.get_by_role("link", name="Manage workspace links").click()
    page.get_by_label("Workspace name").fill("Arts preview")
    page.get_by_label("Workspace origin").fill(second)
    page.get_by_role("checkbox", name="Archive editors").check()
    page.get_by_role("button", name="Save workspace link").click()
    expect(page.locator("#workspaceLinkStatus")).to_have_text("Workspace link saved.")
    directory = requests.get(first + "/api/team/workspace-links", headers=first_auth, timeout=10).json()
    link, = directory["workspaces"]
    assert link["origin"] == second and link["groups"] == [group]

    with browser.new_context() as contributor, browser.new_context() as observer:
        join(observer, first, visitor_invitation, "Observer")
        assert observer.request.get(first + "/api/team/library").json()["workspaces"] == []
        assert observer.request.get(first + "/api/team/workspace-links", headers=TEAM).status == 403
        response = contributor.request.post(first + "/api/team/session", headers=TEAM,
                                            data={"email": email, "password": PASSWORD})
        assert response.ok and response.json()["role"] == "contributor"
        viewer = contributor.new_page()
        viewer.goto(first + "/team")
        viewer.get_by_role("link", name="Arts preview", exact=True).click()
        assert viewer.url == second + "/team"
        expect(viewer.get_by_role("button", name="Sign in to team")).to_be_visible()
        assert contributor.request.get(second + "/api/team/account").status == 401
        second_member = join(contributor, second, second_invitation, "Arts viewer")
        viewer.reload()
        expect(viewer.locator("#teamIdentity")).to_have_text("Arts viewer · Viewer")
        assert contributor.request.get(first + "/api/team/account").json()["role"] == "contributor"
        assert contributor.request.get(second + "/api/team/account").json()["role"] == "viewer"
        assert contributor.request.get(first + f"/team/video/{private_work}/media").ok
        assert contributor.request.get(second + f"/team/video/{private_work}/media").status == 404

        for address, name in ((first, "Station audience member"), (second, "Arts audience member")):
            response = contributor.request.post(address + "/api/viewer/signup", headers={"X-CuePorch-Viewer": "1"},
                                                data={"email": email, "name": name, "password": PASSWORD})
            assert response.status == 201
        assert contributor.request.get(first + "/api/viewer/account").json()["name"] == "Station audience member"
        assert contributor.request.get(second + "/api/viewer/account").json()["name"] == "Arts audience member"

        requests.delete(second + f"/api/team/members/{second_member}", headers=TEAM | second_auth, json={},
                        timeout=10).raise_for_status()
        assert contributor.request.get(second + "/api/team/account").status == 401
        assert contributor.request.get(first + "/api/team/account").json()["role"] == "contributor"
        page.get_by_role("button", name="Remove workspace link").click()
        expect(page.locator("#workspaceLinkStatus")).to_have_text("Workspace link removed.")
        viewer.goto(first + "/team")
        expect(viewer.get_by_role("heading", name="Available videos")).to_be_visible()
        assert contributor.request.get(first + "/api/team/library").json()["workspaces"] == []

    return dict(isolated_workspace_data_and_operator_keys=True, group_scoped_switch_links=True,
                concurrent_team_and_audience_sessions=True, independent_roles_and_revocation=True)
