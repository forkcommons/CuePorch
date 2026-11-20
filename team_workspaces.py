"""Workspace directory for CuePorch services with independently owned data."""

import uuid
from urllib.parse import urlsplit

from flask import abort, render_template, request

from browser_account_security import is_loopback, json_mutation, private, secure_site
from library_analytics import source_origin


SCHEMA = """
CREATE TABLE IF NOT EXISTS team_workspace_link(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL UNIQUE, revision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS team_workspace_link_group(
 workspace TEXT NOT NULL REFERENCES team_workspace_link(id) ON DELETE CASCADE,
 team_group TEXT NOT NULL REFERENCES team_group(id) ON DELETE CASCADE,
 PRIMARY KEY(workspace,team_group));
CREATE INDEX IF NOT EXISTS team_workspace_link_group_by_group ON team_workspace_link_group(team_group,workspace);
"""


def linked_workspaces(con, member):
    return [dict(row) for row in con.execute("""SELECT name,origin FROM team_workspace_link link
        WHERE ?='admin' OR EXISTS (
            SELECT 1 FROM team_workspace_link_group scope
            JOIN team_group_member membership ON membership.team_group=scope.team_group
            WHERE scope.workspace=link.id AND membership.member=?
        ) ORDER BY name COLLATE NOCASE,origin""", (member["role"], member["id"]))]


def canonical_origin(value, secure):
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or any(
        char.isspace() or ord(char) < 32 or char in "\\%@" for char in value
    ):
        abort(400, "Enter a complete CuePorch workspace origin.")
    address = source_origin(value)
    if address == "Unknown":
        abort(400, "Enter a complete CuePorch workspace origin.")
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment or parsed.path not in ("", "/") or parsed.port == 0:
        abort(400, "Use the workspace origin without a page path, query or fragment.")
    if parsed.scheme != "https" and (secure or not is_loopback(parsed.hostname)):
        abort(400, "Use HTTPS for a hosted workspace.")
    if address == source_origin(("https://" if secure else "http://") + request.host):
        abort(400, "Choose a different workspace.")
    return address


def register(app, tx, origin, manager, note):
    @app.get("/team/workspaces")
    def workspace_directory_page():
        secure_site(origin)
        return private(render_template("team_workspaces.html"))

    @app.get("/api/team/workspace-links")
    def workspace_directory():
        with tx() as con:
            manager(con)
            links = {row["id"]: dict(row, groups=[]) for row in con.execute(
                "SELECT id,name,origin,revision FROM team_workspace_link ORDER BY name COLLATE NOCASE,origin")}
            groups = [dict(row) for row in con.execute("SELECT id,name FROM team_group ORDER BY name COLLATE NOCASE")]
            for workspace, group in con.execute("SELECT workspace,team_group FROM team_workspace_link_group"):
                links[workspace]["groups"].append(group)
        return private(dict(workspaces=list(links.values()), groups=groups))

    @app.post("/api/team/workspace-links")
    @app.route("/api/team/workspace-links/<uuid:link>", methods=("PUT", "DELETE"))
    def save_workspace_link(link=None):
        secure = secure_site(origin)
        body = json_mutation(secure, "X-CuePorch-Team")
        uid = str(link or uuid.uuid4())
        with tx() as con:
            actor = manager(con)
            if link:
                current = con.execute("SELECT name,revision FROM team_workspace_link WHERE id=?", (uid,)).fetchone()
                if current is None:
                    abort(404)
                if body.get("revision") != current["revision"]:
                    abort(409, "This workspace link changed. Reload it before saving.")
            if request.method == "DELETE":
                con.execute("DELETE FROM team_workspace_link_group WHERE workspace=?", (uid,))
                con.execute("DELETE FROM team_workspace_link WHERE id=?", (uid,))
                note(con, actor, "workspace_link_removed", uid, current["name"])
                return private("", 204)
            name = body.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 32:
                abort(400, "Enter a workspace name of up to 32 characters.")
            address = canonical_origin(body.get("origin"), secure)
            if con.execute("SELECT 1 FROM team_workspace_link WHERE origin=? AND id<>?", (address, uid)).fetchone():
                abort(409, "This workspace origin is already listed.")
            groups = body.get("groups")
            if not isinstance(groups, list) or any(not isinstance(group, str) for group in groups):
                abort(400, "Choose groups for this workspace.")
            selection = set(groups)
            if not selection <= {row[0] for row in con.execute("SELECT id FROM team_group")}:
                abort(409, "Reload the workspace links. Some groups are unavailable.")
            con.execute("""INSERT INTO team_workspace_link VALUES(?,?,?,?) ON CONFLICT(id)
                DO UPDATE SET name=excluded.name,origin=excluded.origin,revision=excluded.revision""",
                (uid, name.strip(), address, uuid.uuid4().hex))
            con.execute("DELETE FROM team_workspace_link_group WHERE workspace=?", (uid,))
            con.executemany("INSERT INTO team_workspace_link_group VALUES(?,?)", [(uid, group) for group in selection])
            note(con, actor, "workspace_link_saved", uid, name.strip())
        return private(dict(id=uid), 200 if link else 201)
