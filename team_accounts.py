"""Team invitations, member sessions and private Library access."""

import csv
from datetime import datetime, timezone
import hashlib
import hmac
from io import StringIO
import secrets
import time
import uuid

from flask import Response, abort, render_template, request, send_file, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import account_email
import account_email_factor
import team_saml
import team_scim
import team_group_roles
import team_sessions
import account_recovery
import account_totp
import creator_profiles
import library_analytics
import mail_relay
import team_analytics
import team_workspaces
from browser_account_security import (TOKEN, email_key, json_mutation, password, private, same_origin,
                                      secure_site, token_hash, workspace_session, workspace_session_cookie)
from library_privacy import blocked, current_settings, editable, playback, protected_route


SCHEMA = """
CREATE TABLE IF NOT EXISTS team_member(
 id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
 password_hash TEXT NOT NULL, joined REAL NOT NULL, removed REAL);
CREATE UNIQUE INDEX IF NOT EXISTS team_active_email ON team_member(email) WHERE removed IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS team_active_owner ON team_member(role) WHERE removed IS NULL AND role='owner';
CREATE TABLE IF NOT EXISTS team_invitation(
 id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, role TEXT NOT NULL,
 token_hash TEXT UNIQUE NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS team_session(token_hash TEXT PRIMARY KEY, member TEXT NOT NULL, expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS team_session_member ON team_session(member);
CREATE INDEX IF NOT EXISTS team_session_expiry ON team_session(expires);
CREATE TABLE IF NOT EXISTS team_auth_attempt(bucket TEXT PRIMARY KEY, expires REAL NOT NULL, attempts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS team_auth_attempt_expiry ON team_auth_attempt(expires);
CREATE TABLE IF NOT EXISTS team_work_grant(member TEXT NOT NULL, work TEXT NOT NULL, PRIMARY KEY(member,work));
CREATE TABLE IF NOT EXISTS team_folder_grant(
 member TEXT NOT NULL REFERENCES team_member(id) ON DELETE CASCADE,
 folder TEXT NOT NULL REFERENCES library_folder(id) ON DELETE CASCADE,
 PRIMARY KEY(member,folder));
CREATE TABLE IF NOT EXISTS team_group(
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, revision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS team_group_member(
 team_group TEXT NOT NULL REFERENCES team_group(id) ON DELETE CASCADE,
 member TEXT NOT NULL REFERENCES team_member(id) ON DELETE CASCADE, PRIMARY KEY(team_group,member));
CREATE INDEX IF NOT EXISTS team_groups_by_member ON team_group_member(member,team_group);
CREATE TABLE IF NOT EXISTS team_group_folder(
 team_group TEXT NOT NULL REFERENCES team_group(id) ON DELETE CASCADE,
 folder TEXT NOT NULL REFERENCES library_folder(id) ON DELETE CASCADE, PRIMARY KEY(team_group,folder));
CREATE TABLE IF NOT EXISTS team_owned_work(work TEXT PRIMARY KEY, member TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS team_owned_work_member ON team_owned_work(member);
CREATE TABLE IF NOT EXISTS team_membership_event(
 sequence INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
 subject TEXT NOT NULL, detail TEXT, at REAL NOT NULL);
""" + team_analytics.SCHEMA + creator_profiles.SCHEMA
MANAGERS = ("owner", "admin")
CONTRIBUTORS = ("contributor", "contributor_plus")
ROLES = ("admin", *CONTRIBUTORS, "viewer")
INVITE_SECONDS = 7 * 86400
SESSION_MARKER = "team-session"
MUTATION_HEADER = "X-CuePorch-Team"


def email_invitation(config, address, url, note):
    introduction = note + "\n\n" if note else ""
    accepted = mail_relay.deliver(config, address, "Your CuePorch team invitation",
                                 introduction + "Join the CuePorch team at:\n\n" + url +
                                 "\n\nThis private link expires in seven days and can be used once.\n")
    return "accepted" if accepted else "failed"


def initialize_permissions(con):
    for table in ("team_work_grant", "team_folder_grant", "team_group_folder"):
        if "permission" not in {row[1] for row in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"""ALTER TABLE {table} ADD COLUMN permission TEXT NOT NULL
                DEFAULT 'view' CHECK(permission IN ('view','edit'))""")


def permission_from(body):
    permission = body.get("permission", "view")
    if permission not in ("view", "edit"):
        abort(400, "Choose Can view or Can edit.")
    return permission


def current(con, secure):
    digest = workspace_session("team", secure)
    if not digest:
        return None
    now = team_sessions.clock()
    idle = team_sessions.idle_cutoff(con, now)
    row = con.execute(f"""SELECT m.id,m.name,m.email,{team_group_roles.EFFECTIVE_ROLE} AS role
        FROM team_session s JOIN team_member m ON m.id=s.member
        WHERE s.token_hash=? AND s.expires>? AND m.removed IS NULL AND
        (? IS NULL OR s.last_active>?) AND
        (m.role<>'owner' OR NOT EXISTS(SELECT 1 FROM team_saml_session f WHERE f.token_hash=s.token_hash))""",
        (digest, now, idle, idle)).fetchone()
    if row and idle is not None and not con.execute(
        "UPDATE team_session SET last_active=max(last_active,?) WHERE token_hash=? AND expires>? AND last_active>?",
        (now, digest, now, idle),
    ).rowcount:
        return None
    return row


def actor_id(member):
    return "team:" + member["id"]


def note(con, actor, action, subject, detail=None):
    con.execute("INSERT INTO team_membership_event(actor,action,subject,detail,at) VALUES(?,?,?,?,?)",
                (actor, action, subject, detail, time.time()))


def role_from(body):
    role = body.get("role")
    if role not in ROLES:
        abort(400, "Choose Administrator, Contributor Plus, Contributor or Viewer.")
    return role


def member_role(con, uid):
    row = con.execute("SELECT role FROM team_member WHERE id=? AND removed IS NULL", (uid,)).fetchone()
    if row is None:
        abort(404, "This member is no longer active.")
    return row[0]


def ownership(con, actor):
    row = con.execute("SELECT id FROM team_member WHERE role='owner' AND removed IS NULL").fetchone()
    uid = row["id"] if row else None
    return dict(owner=uid, can_transfer=actor == "operator" or (uid is not None and actor == "team:" + uid))


def roster(con):
    members = [dict(row, role_override=bool(row["role_override"])) for row in con.execute(f"""
        SELECT m.id,m.email,m.name,m.role,m.joined,m.removed,{team_group_roles.EFFECTIVE_ROLE} AS effective_role,
            EXISTS(SELECT 1 FROM team_member_role_override direct WHERE direct.member=m.id) AS role_override
        FROM team_member m ORDER BY m.removed IS NOT NULL,m.name,m.email""")]
    invitations = [dict(row) for row in con.execute("SELECT id,email,role,expires FROM team_invitation ORDER BY email")]
    return members, invitations


def spreadsheet_cell(value):
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def grant_available(con, uid):
    return not blocked(con, uid) and not con.execute("""SELECT 1 FROM library_peer_handoff
        WHERE work=? AND state='pending'""", (uid,)).fetchone()


def visible_folders(con):
    return con.execute("""WITH RECURSIVE paths(id,title) AS (
        SELECT id,name FROM library_folder WHERE parent IS NULL AND NOT EXISTS (
            SELECT 1 FROM library_deleted_item d WHERE d.kind='folder' AND d.item=library_folder.id)
        UNION ALL
        SELECT f.id,paths.title || ' / ' || f.name FROM library_folder f JOIN paths ON f.parent=paths.id
        WHERE NOT EXISTS (SELECT 1 FROM library_deleted_item d WHERE d.kind='folder' AND d.item=f.id)
    ) SELECT id,title FROM paths ORDER BY title COLLATE NOCASE,id""").fetchall()


def inherited_works(con, member, required="view"):
    rows = con.execute("""WITH RECURSIVE roots(id) AS (
        SELECT folder FROM team_folder_grant WHERE member=? AND (?='view' OR permission='edit')
        UNION
        SELECT f.folder FROM team_group_folder f JOIN team_group_member m ON m.team_group=f.team_group
        JOIN team_group g ON g.id=m.team_group WHERE m.member=? AND (?='view' OR f.permission='edit')
    ), granted(id) AS (
        SELECT f.id FROM roots r JOIN library_folder f ON f.id=r.id
        WHERE NOT EXISTS (
            SELECT 1 FROM library_deleted_item d WHERE d.kind='folder' AND d.item=f.id)
        UNION
        SELECT f.id FROM library_folder f JOIN granted g ON f.parent=g.id
        WHERE NOT EXISTS (SELECT 1 FROM library_deleted_item d WHERE d.kind='folder' AND d.item=f.id)
    ) SELECT p.work FROM library_placement p JOIN granted g ON g.id=p.folder""",
        (member, required, member, required))
    return {row[0] for row in rows}


def group_selection(values, available, label):
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        abort(400, f"Choose {label} from this workspace.")
    selection = set(values)
    if not selection <= available:
        abort(409, f"Reload the group. Some {label} are unavailable.")
    return selection


def register(app, tx, origin, key, workspace_brand, operator_name, read_works, title_for, root, public_listing,
             entry_payload, sharing_access):
    rate_key = hmac.digest(key.encode(), b"CuePorch team attempts", "sha256")
    dummy = generate_password_hash(secrets.token_urlsafe(24))
    invitation_mail = mail_relay.configured(origin)

    def limit(con, operation, subject=""):
        now = time.time()
        peer = request.remote_addr or ""
        con.execute("DELETE FROM team_auth_attempt WHERE expires<=?", (now,))
        checks = ((peer, 120), (peer + "|" + subject, 12)) if subject else ((peer, 60),)
        for value, maximum in checks:
            bucket = hmac.new(rate_key, (operation + "|" + value).encode(), hashlib.sha256).hexdigest()
            row = con.execute("SELECT attempts FROM team_auth_attempt WHERE bucket=?", (bucket,)).fetchone()
            if row and row[0] >= maximum:
                abort(429, "Too many attempts. Try again later.")
            con.execute("""INSERT INTO team_auth_attempt VALUES(?,?,1) ON CONFLICT(bucket)
                DO UPDATE SET attempts=attempts+1""", (bucket, now + 600))

    def mutation(secure):
        return json_mutation(secure, MUTATION_HEADER)

    def manager(con):
        actor = operator_name(con)
        if not actor:
            abort(403, "Team administration requires Administrator access.")
        return actor

    def member(con, secure):
        row = current(con, secure)
        if not row:
            abort(401, "Sign in to your team account.")
        return row

    def set_session_cookie(response, secure, token):
        lifetime = None
        if token:
            with tx() as con:
                lifetime = team_sessions.cookie_age(con)
        return workspace_session_cookie(response, "team", secure, lifetime, token)

    def session_response(body, secure, token=None, status=200):
        return set_session_cookie(private(body, status), secure, token)

    saml_available = team_saml.register(
        app, tx, origin, operator_name, ownership, mutation, team_sessions.issue, set_session_cookie, note, limit,
    )
    team_sessions.register(app, tx, origin, manager, note)

    def security_account(con, secure):
        uid = member(con, secure)["id"]
        return con.execute("SELECT id,email,password_hash FROM team_member WHERE id=?", (uid,)).fetchone()

    def rotate_security_session(con, uid, secure):
        federated = con.execute("""SELECT s.expires FROM team_session s JOIN team_saml_session f USING(token_hash)
                                 WHERE s.member=? AND s.token_hash=?""", (uid, workspace_session("team", secure))).fetchone()
        con.execute("DELETE FROM team_session WHERE member=?", (uid,))
        token = team_sessions.issue(con, uid, secure)
        if federated:
            team_saml.mark_session(con, token, federated["expires"])
        return token

    def note_authenticator(con, actor, enabled):
        note(con, actor_id(actor), "authenticator_enabled" if enabled else "authenticator_disabled", actor["id"])

    authenticator = account_totp.register(
        app, tx, "team", key, lambda: secure_site(origin), mutation, security_account, limit, rotate_security_session,
        set_session_cookie,
        note_authenticator,
    )
    email_security = account_email.register(
        app, tx, "team", key, origin, lambda: secure_site(origin), mutation, security_account, limit,
        lambda con, actor: note(con, actor_id(actor), "email_verified", actor["id"]),
    )
    email_factor = account_email_factor.register(
        app, tx, email_security, authenticator, lambda: secure_site(origin), mutation, security_account, limit,
        rotate_security_session,
        set_session_cookie,
        lambda con, actor, enabled: note(con, actor_id(actor),
                                         "email_factor_enabled" if enabled else "email_factor_disabled", actor["id"]),
    )

    def reset_password(con, uid, encoded):
        con.execute("UPDATE team_member SET password_hash=? WHERE id=? AND removed IS NULL", (encoded, uid))
        con.execute("DELETE FROM team_session WHERE member=?", (uid,))
        note(con, "team:" + uid, "password_recovered", uid)

    def password_account(con, address):
        actor = con.execute("SELECT id,email,role,password_hash FROM team_member WHERE email=? AND removed IS NULL",
                            (address,)).fetchone()
        return actor if team_saml.allows_password(con, actor) else None

    account_recovery.register(
        app, tx, "team", origin, email_security, lambda: secure_site(origin), mutation,
        password_account,
        limit, authenticator, reset_password,
    )

    def clear_local_credentials(con, uid):
        con.execute("DELETE FROM team_session WHERE member=?", (uid,))
        authenticator.forget(con, uid)
        email_factor.forget(con, uid)
        email_security.forget(con, uid)
        con.execute("DELETE FROM account_password_reset WHERE scope='team' AND account=?", (uid,))

    def revoke_member_access(con, uid):
        clear_local_credentials(con, uid)
        con.execute("DELETE FROM team_work_grant WHERE member=?", (uid,))
        con.execute("DELETE FROM team_folder_grant WHERE member=?", (uid,))
        con.execute("""UPDATE team_group SET revision=lower(hex(randomblob(16)))
            WHERE id IN (SELECT team_group FROM team_group_member WHERE member=?)""", (uid,))
        con.execute("DELETE FROM team_group_member WHERE member=?", (uid,))
        team_group_roles.set_direct_role(con, uid, False)

    def delete_group(con, uid):
        con.execute("DELETE FROM team_group_member WHERE team_group=?", (uid,))
        con.execute("DELETE FROM team_group_folder WHERE team_group=?", (uid,))
        con.execute("""UPDATE team_workspace_link SET revision=lower(hex(randomblob(16)))
            WHERE id IN (SELECT workspace FROM team_workspace_link_group WHERE team_group=?)""", (uid,))
        con.execute("DELETE FROM team_workspace_link_group WHERE team_group=?", (uid,))
        con.execute("DELETE FROM team_scim_group WHERE team_group=?", (uid,))
        con.execute("DELETE FROM team_saml_group WHERE team_group=?", (uid,))
        con.execute("DELETE FROM team_group_role WHERE team_group=?", (uid,))
        con.execute("DELETE FROM team_group WHERE id=?", (uid,))

    team_scim.register(app, tx, origin, operator_name, ownership, mutation, note, revoke_member_access,
                       clear_local_credentials, delete_group)

    def eligible_works(con, ids=None):
        return {work["id"]: work for work in read_works() if (ids is None or work["id"] in ids)
                and playback(work) and grant_available(con, work["id"])}

    def shared_ids(con, uid):
        return {row[0] for row in con.execute("SELECT work FROM team_work_grant WHERE member=?", (uid,))}

    def can_edit(con, row, uid):
        if not row or not grant_available(con, uid):
            return False
        if row["role"] in MANAGERS:
            return True
        if row["role"] not in CONTRIBUTORS:
            return False
        direct = con.execute("""SELECT 1 FROM team_owned_work WHERE member=? AND work=?
            UNION ALL SELECT 1 FROM team_work_grant WHERE member=? AND work=? AND permission='edit'""",
            (row["id"], uid, row["id"], uid)).fetchone()
        return direct is not None or uid in inherited_works(con, row["id"], "edit")

    def authorize_editor(con, uid, *, advanced=False):
        if operator_name(con):
            return False
        secure = secure_site(origin)
        same_origin(secure)
        if request.method != "GET":
            mutation(secure)
        if request.headers.get("Authorization") != "Bearer " + SESSION_MARKER:
            abort(403)
        row = current(con, secure)
        if not can_edit(con, row, uid) or (advanced and row["role"] != "contributor_plus"):
            abort(404)
        return True

    @app.get("/team/join")
    def team_join_page():
        secure_site(origin)
        return private(render_template("team_sign_in.html", brand=workspace_brand(), joining=True))

    @app.get("/team")
    def team_sign_in_page():
        secure_site(origin)
        return private(render_template("team_sign_in.html", brand=workspace_brand(), joining=False,
                                       can_recover=bool(email_security.config), saml_available=saml_available()))

    @app.get("/team/members")
    def team_members_page():
        secure_site(origin)
        return private(render_template("team_members.html", brand=workspace_brand(),
                                       invitation_mail=bool(invitation_mail)))

    @app.get("/api/team/account")
    def team_account():
        secure = secure_site(origin)
        same_origin(secure)
        with tx() as con:
            return private(dict(member(con, secure)))

    @app.route("/api/team/session", methods=("POST", "DELETE"))
    def team_session():
        secure = secure_site(origin)
        body = mutation(secure)
        if request.method == "DELETE":
            with tx() as con:
                con.execute("DELETE FROM team_session WHERE token_hash=?", (workspace_session("team", secure),))
            return session_response("", secure, status=204)
        email, entered = email_key(body.get("email")), password(body)
        with tx() as con:
            limit(con, "sign-in", email)
            row = con.execute("SELECT id,password_hash FROM team_member WHERE email=? AND removed IS NULL",
                              (email,)).fetchone()
        if not check_password_hash(row["password_hash"] if row and row["password_hash"] else dummy, entered) or row is None:
            abort(401, "The email or password did not match an active team member.")
        with tx() as con:
            logged_in = con.execute(f"""SELECT m.id,m.email,m.name,{team_group_roles.EFFECTIVE_ROLE} AS role FROM team_member m
                WHERE m.id=? AND m.email=? AND m.password_hash=? AND m.removed IS NULL""",
                (row["id"], email, row["password_hash"])).fetchone()
            if logged_in is None:
                abort(401, "The email or password did not match an active team member.")
            factor_actor = dict(logged_in, password_hash=row["password_hash"])
            if not team_saml.allows_password(con, factor_actor):
                abort(403, "Use your organization's single sign-on for this account.")
            needs_mail, prepared = email_factor.check(con, factor_actor, body)
            if not needs_mail:
                token = team_sessions.issue(con, row["id"], secure)
        if needs_mail:
            return email_factor.deliver(tx, factor_actor, prepared)
        return session_response(dict(logged_in), secure, token)

    @app.post("/api/team/join")
    def team_join():
        secure = secure_site(origin)
        body = mutation(secure)
        invite = body.get("invitation")
        name = body.get("name")
        if not isinstance(invite, str) or not TOKEN.fullmatch(invite):
            abort(400, "Open the team invitation again.")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name):
            abort(400, "Enter a display name.")
        name, entered = name.strip(), password(body, new=True)
        invite_hash = token_hash(invite)
        with tx() as con:
            limit(con, "join")
            pending = con.execute("SELECT 1 FROM team_invitation WHERE token_hash=? AND expires>?",
                                  (invite_hash, time.time())).fetchone()
        if not pending:
            abort(404, "This invitation has expired or is no longer available.")
        encoded = generate_password_hash(entered)
        uid = str(uuid.uuid4())
        with tx() as con:
            pending = con.execute("SELECT id,email,role FROM team_invitation WHERE token_hash=? AND expires>?",
                                  (invite_hash, time.time())).fetchone()
            if not pending:
                abort(404, "This invitation has expired or is no longer available.")
            if con.execute("SELECT 1 FROM team_member WHERE email=? AND removed IS NULL", (pending["email"],)).fetchone():
                abort(409, "This email already has an active team membership.")
            con.execute("INSERT INTO team_member VALUES(?,?,?,?,?,?,NULL)",
                        (uid, pending["email"], name, pending["role"], encoded, time.time()))
            con.execute("DELETE FROM team_invitation WHERE id=?", (pending["id"],))
            note(con, "team:" + uid, "joined", uid, pending["role"])
            token = None if team_saml.forces_saml(team_saml.configured(con)[1], pending) else team_sessions.issue(con, uid, secure)
        return session_response(dict(id=uid, email=pending["email"], name=name, role=pending["role"]), secure, token, 201)

    @app.get("/api/team/members")
    def team_members():
        with tx() as con:
            actor = manager(con)
            members, invitations = roster(con)
            owner = ownership(con, actor)
        return private(dict(members=members, invitations=invitations, ownership=owner))

    @app.get("/api/team/roster.csv")
    def roster_csv():
        with tx() as con:
            actor = manager(con)
            members, invitations = roster(con)
            note(con, actor, "roster_exported", "roster", f"{len(members)} members, {len(invitations)} invitations")
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(("Email", "Name", "Role", "Date added (UTC)", "Status"))
        for row in members:
            joined = datetime.fromtimestamp(row["joined"], timezone.utc).isoformat()
            writer.writerow((spreadsheet_cell(row["email"]), spreadsheet_cell(row["name"]), row["effective_role"],
                             joined, "Removed" if row["removed"] is not None else "Accepted"))
        now = time.time()
        for row in invitations:
            invited = datetime.fromtimestamp(row["expires"] - INVITE_SECONDS, timezone.utc).isoformat()
            writer.writerow((spreadsheet_cell(row["email"]), "", row["role"], invited,
                             "Pending" if row["expires"] > now else "Expired"))
        return private(Response(output.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": "attachment; filename=cueporch-team-roster.csv",
        }))

    @app.put("/api/team/owner/<uuid:account>")
    def set_team_owner(account):
        body = mutation(secure_site(origin))
        uid = str(account)
        with tx() as con:
            actor = manager(con)
            owner = ownership(con, actor)
            if not owner["can_transfer"]:
                abort(403, "Ownership can be changed by the Owner or the installation credential.")
            current_owner = owner["owner"]
            if "previous" not in body or body["previous"] != current_owner:
                abort(409, "The owner changed. Reload the roster before assigning ownership.")
            if member_role(con, uid) == "owner":
                return private("", 204)
            if not con.execute("SELECT password_hash FROM team_member WHERE id=?", (uid,)).fetchone()["password_hash"]:
                abort(409, "Choose a member who can recover the workspace with a local account.")
            if current_owner:
                con.execute("UPDATE team_member SET role='admin' WHERE id=?", (current_owner,))
                team_group_roles.set_direct_role(con, current_owner, True)
            con.execute("UPDATE team_member SET role='owner' WHERE id=?", (uid,))
            note(con, actor, "owner_changed", uid, current_owner)
        return private("", 204)

    @app.post("/api/team/invitations")
    def team_invite():
        secure = secure_site(origin)
        body = mutation(secure)
        email, role = email_key(body.get("email")), role_from(body)
        send_email, message = body.get("send_email", False), body.get("message", "")
        if not isinstance(send_email, bool):
            abort(400, "Choose whether to email the invitation.")
        if not isinstance(message, str) or len(message) > 1000:
            abort(400, "Use at most 1000 characters for the invitation message.")
        uid, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        with tx() as con:
            actor = manager(con)
            if send_email and not invitation_mail:
                abort(409, "Invitation email needs a configured relay and a public HTTPS address.")
            if con.execute("SELECT 1 FROM team_member WHERE email=? AND removed IS NULL", (email,)).fetchone():
                abort(409, "This email already has an active team membership.")
            con.execute("""INSERT INTO team_invitation VALUES(?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET
                id=excluded.id,role=excluded.role,token_hash=excluded.token_hash,expires=excluded.expires""",
                (uid, email, role, token_hash(token), time.time() + INVITE_SECONDS))
            note(con, actor, "invited", uid, role)
        path = url_for("team_join_page") + "#" + token
        url = origin + path if origin.startswith("https://") else None
        delivery = email_invitation(invitation_mail, email, url, message.strip()) if send_email else None
        return private(dict(id=uid, path=path, url=url, delivery=delivery), 201)

    @app.delete("/api/team/invitations/<uuid:invitation>")
    def team_cancel_invitation(invitation):
        mutation(secure_site(origin))
        uid = str(invitation)
        with tx() as con:
            actor = manager(con)
            if not con.execute("DELETE FROM team_invitation WHERE id=?", (uid,)).rowcount:
                abort(404, "This invitation is no longer pending.")
            note(con, actor, "invitation_canceled", uid)
        return private("", 204)

    @app.route("/api/team/members/<uuid:account>", methods=("PUT", "DELETE"))
    def team_manage_member(account):
        body = mutation(secure_site(origin))
        uid = str(account)
        with tx() as con:
            actor = manager(con)
            previous = member_role(con, uid)
            if previous == "owner":
                abort(409, "Transfer ownership before changing this member's role or removing access.")
            if request.method == "DELETE":
                con.execute("UPDATE team_member SET removed=?,password_hash='' WHERE id=?", (time.time(), uid))
                revoke_member_access(con, uid)
                note(con, actor, "removed", uid, previous)
            else:
                role = role_from(body)
                direct = body.get("role_override")
                if "role_override" in body and not isinstance(direct, bool):
                    abort(400, "Choose whether this member's direct role overrides group defaults.")
                if role != previous:
                    con.execute("UPDATE team_member SET role=? WHERE id=?", (role, uid))
                    note(con, actor, "role_changed", uid, previous + " → " + role)
                if direct is not None or role != previous:
                    team_group_roles.set_direct_role(con, uid, direct is not False)
                    note(con, actor, "role_source_changed", uid, "direct" if direct is not False else "group")
        return private("", 204)

    @app.get("/api/team/audit")
    def team_audit():
        with tx() as con:
            manager(con)
            rows = [dict(row) for row in con.execute("SELECT * FROM team_membership_event ORDER BY sequence DESC LIMIT 200")]
        return private(dict(events=rows))

    @app.get("/api/team/groups")
    def team_groups():
        with tx() as con:
            manager(con)
            members = [dict(row) for row in con.execute("""SELECT id,name,email FROM team_member
                WHERE removed IS NULL ORDER BY name COLLATE NOCASE,email""")]
            folders = [dict(row) for row in visible_folders(con)]
            groups = {row["id"]: dict(row, managed=bool(row["managed"]), members=[], folders=[], edit_folders=[]) for row in con.execute(
                """SELECT g.id,g.name,g.revision,defaults.role AS default_role,
                           (scim.team_group IS NOT NULL OR saml.team_group IS NOT NULL) AS managed
                    FROM team_group g LEFT JOIN team_scim_group scim ON scim.team_group=g.id
                    LEFT JOIN team_saml_group saml ON saml.team_group=g.id
                    LEFT JOIN team_group_role defaults ON defaults.team_group=g.id ORDER BY g.name COLLATE NOCASE""")}
            for group_id, member_id in con.execute("SELECT team_group,member FROM team_group_member"):
                if group_id in groups:
                    groups[group_id]["members"].append(member_id)
            available = {folder["id"] for folder in folders}
            for group_id, folder_id, name, permission in con.execute("""SELECT g.team_group,g.folder,f.name,g.permission
                FROM team_group_folder g JOIN library_folder f ON f.id=g.folder"""):
                if group_id in groups:
                    groups[group_id]["folders"].append(folder_id)
                    if permission == "edit":
                        groups[group_id]["edit_folders"].append(folder_id)
                    if folder_id not in available:
                        groups[group_id].setdefault("unavailable_folders", []).append(
                            dict(id=folder_id, title=f"{name} (unavailable)"))
        return private(dict(groups=list(groups.values()), members=members, folders=folders))

    @app.post("/api/team/groups")
    @app.route("/api/team/groups/<uuid:group>", methods=("PUT", "DELETE"))
    def save_team_group(group=None):
        body = mutation(secure_site(origin))
        uid = str(group or uuid.uuid4())
        with tx() as con:
            actor = manager(con)
            managed = False
            if group:
                current = con.execute("SELECT name,revision FROM team_group WHERE id=?", (uid,)).fetchone()
                if current is None:
                    abort(404)
                if body.get("revision") != current["revision"]:
                    abort(409, "This group changed. Reload it before saving.")
                managed = con.execute("""SELECT 1 FROM team_scim_group WHERE team_group=?
                    UNION ALL SELECT 1 FROM team_saml_group WHERE team_group=?""", (uid, uid)).fetchone() is not None
            if request.method == "DELETE":
                if managed:
                    abort(409, "Remove this group from the identity provider.")
                delete_group(con, uid)
                note(con, actor, "group_removed", uid, current["name"])
                return private("", 204)
            name = body.get("name")
            if managed:
                name = current["name"]
            elif not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
                abort(400, "Enter a group name of up to 64 characters.")
            name = name.strip()
            if con.execute("SELECT 1 FROM team_group WHERE name=? COLLATE NOCASE AND id<>?", (name, uid)).fetchone():
                abort(409, "A group already has this name.")
            members = [] if managed else group_selection(body.get("members"), {row[0] for row in con.execute(
                "SELECT id FROM team_member WHERE removed IS NULL")}, "members")
            available = {row["id"] for row in visible_folders(con)}
            existing = {row[0] for row in con.execute("""SELECT g.folder FROM team_group_folder g
                JOIN library_folder f ON f.id=g.folder WHERE g.team_group=?""", (uid,))}
            folders = group_selection(body.get("folders"), available | existing, "folders")
            editors = group_selection(body.get("edit_folders", []), folders, "folders with edit access")
            default_role = body.get("default_role")
            if default_role is not None and default_role not in team_group_roles.ROLES:
                abort(400, "Choose a valid group role.")
            revision = uuid.uuid4().hex
            con.execute("""INSERT INTO team_group VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,revision=excluded.revision""", (uid, name, revision))
            if not managed:
                con.execute("DELETE FROM team_group_member WHERE team_group=?", (uid,))
                con.executemany("INSERT INTO team_group_member VALUES(?,?)", [(uid, member) for member in members])
            con.execute("DELETE FROM team_group_folder WHERE team_group=?", (uid,))
            con.executemany("INSERT INTO team_group_folder VALUES(?,?,?)", [
                (uid, folder, "edit" if folder in editors else "view") for folder in folders
            ])
            if "default_role" in body:
                previous_role = con.execute("SELECT role FROM team_group_role WHERE team_group=?", (uid,)).fetchone()
                if default_role:
                    con.execute("INSERT OR REPLACE INTO team_group_role VALUES(?,?)", (uid, default_role))
                else:
                    con.execute("DELETE FROM team_group_role WHERE team_group=?", (uid,))
                if default_role != (previous_role["role"] if previous_role else None):
                    note(con, actor, "group_role_changed", uid, default_role or "member roles")
            note(con, actor, "group_updated" if group else "group_created", uid, name)
        return private(dict(id=uid, revision=revision), 200 if group else 201)

    @app.get("/api/team/members/<uuid:account>/works")
    def team_share_options(account):
        uid = str(account)
        with tx() as con:
            manager(con)
            member_role(con, uid)
            shared = dict(con.execute("SELECT work,permission FROM team_work_grant WHERE member=?", (uid,)))
            rows = [dict(id=work_id, title=title_for(con, work), shared=work_id in shared,
                         permission=shared.get(work_id))
                    for work_id, work in eligible_works(con).items()]
            folders_shared = dict(con.execute("SELECT folder,permission FROM team_folder_grant WHERE member=?", (uid,)))
            folders = [dict(row, shared=row["id"] in folders_shared, permission=folders_shared.get(row["id"]))
                       for row in visible_folders(con)]
        return private(dict(works=sorted(rows, key=lambda row: row["title"].casefold()), folders=folders))

    @app.route("/api/team/members/<uuid:account>/folders/<folder>", methods=("PUT", "DELETE"))
    def team_update_folder_sharing(account, folder):
        body = mutation(secure_site(origin))
        uid = str(account)
        with tx() as con:
            actor = manager(con)
            member_role(con, uid)
            if request.method == "PUT":
                if not any(row["id"] == folder for row in visible_folders(con)):
                    abort(404, "This folder is unavailable for team viewing.")
                result = con.execute("""INSERT INTO team_folder_grant VALUES(?,?,?)
                    ON CONFLICT(member,folder) DO UPDATE SET permission=excluded.permission
                    WHERE permission<>excluded.permission""", (uid, folder, permission_from(body)))
                event = "folder_shared"
            else:
                result = con.execute("DELETE FROM team_folder_grant WHERE member=? AND folder=?", (uid, folder))
                event = "folder_unshared"
            if result.rowcount:
                note(con, actor, event, uid, folder)
        return private("", 204)

    @app.route("/api/team/members/<uuid:account>/works/<uuid:work>", methods=("PUT", "DELETE"))
    def team_update_sharing(account, work):
        body = mutation(secure_site(origin))
        uid, work_id = str(account), str(work)
        with tx() as con:
            actor = manager(con)
            member_role(con, uid)
            if request.method == "PUT":
                if work_id not in eligible_works(con, {work_id}):
                    abort(404, "This video is unavailable for team viewing.")
                result = con.execute("""INSERT INTO team_work_grant VALUES(?,?,?)
                    ON CONFLICT(member,work) DO UPDATE SET permission=excluded.permission
                    WHERE permission<>excluded.permission""", (uid, work_id, permission_from(body)))
                event = "video_shared"
            else:
                result = con.execute("DELETE FROM team_work_grant WHERE member=? AND work=?", (uid, work_id))
                event = "video_unshared"
            if result.rowcount:
                note(con, actor, event, uid, work_id)
        return private("", 204)

    def access(con, row, work_id=None):
        if row["role"] in MANAGERS:
            selected = {work_id} if work_id else None
        else:
            selected = shared_ids(con, row["id"]) | inherited_works(con, row["id"])
            if row["role"] in CONTRIBUTORS:
                selected.update(item[0] for item in con.execute("SELECT work FROM team_owned_work WHERE member=?", (row["id"],)))
            if work_id:
                selected.intersection_update({work_id})
        return eligible_works(con, selected)

    def can_read_analytics(con, row, uid):
        return row["role"] in MANAGERS or (row["role"] in CONTRIBUTORS and con.execute(
            "SELECT 1 FROM team_owned_work WHERE member=? AND work=?", (row["id"], uid),
        ).fetchone() is not None)

    def analytics_work(con, uid, secure):
        row = member(con, secure)
        if can_read_analytics(con, row, uid):
            work = access(con, row, uid).get(uid)
            if work:
                return work
        abort(404)

    named_playback = team_analytics.register(
        app, tx, origin, key, manager, member, access, mutation, note, read_works, title_for, spreadsheet_cell,
    )

    @app.get("/api/team/video/<uuid:work>/analytics")
    def team_video_analytics_report(work):
        secure = secure_site(origin)
        same_origin(secure)
        if request.headers.get("Authorization") != "Bearer " + SESSION_MARKER:
            abort(403)
        with tx() as con:
            item = analytics_work(con, str(work), secure)
            return private(library_analytics.report(con, item, title_for(con, item)))

    @app.get("/api/team/library")
    def team_library():
        secure = secure_site(origin)
        same_origin(secure)
        with tx() as con:
            row = member(con, secure)
            works = [dict(id=uid, title=title_for(con, work), watch=url_for("team_watch", work=uid))
                     for uid, work in access(con, row).items()]
            workspaces = team_workspaces.linked_workspaces(con, row)
        return private(dict(works=sorted(works, key=lambda item: item["title"].casefold()), workspaces=workspaces))

    @app.get("/team/video/<uuid:work>")
    def team_watch(work):
        secure = secure_site(origin)
        with tx() as con:
            row = member(con, secure)
            item = access(con, row, str(work)).get(str(work))
            if not item:
                abort(404)
            title = title_for(con, item)
            editable = can_edit(con, row, str(work))
            interactive = editable and row["role"] == "contributor_plus"
            analytics = can_read_analytics(con, row, str(work))
            telemetry = named_playback(con, item, row)
        return private(render_template("team_watch.html", title=title, media=url_for("team_video", work=work),
                                       details=url_for("team_edit_video", work=work) if editable else None,
                                       interactivity=url_for("team_edit_interactivity", work=work) if interactive else None,
                                       analytics=url_for("team_video_analytics", work=work) if analytics else None,
                                       telemetry=telemetry))

    @app.get("/team/video/<uuid:work>/analytics")
    def team_video_analytics(work):
        secure = secure_site(origin)
        with tx() as con:
            analytics_work(con, str(work), secure)
        return private(render_template("video_analytics.html", work=str(work),
                                       team_preview=url_for("team_video", work=work)))

    @app.get("/team/video/<uuid:work>/details")
    def team_edit_video(work):
        secure = secure_site(origin)
        uid = str(work)
        with tx() as con:
            actor = member(con, secure)
            available = eligible_works(con, {uid}).get(uid)
            if not available or not can_edit(con, actor, uid):
                abort(404)
            allowed = sharing_access(con, actor)
            mode = current_settings(con, uid)["visibility"]
            privacy_editor = (editable(available) and not protected_route(con, uid) and len(allowed["modes"]) > 1
                              and mode in allowed["modes"])
        return private(render_template("video_details.html", work=uid, public_url="", team_editor=True,
                                       team_preview=url_for("team_video", work=work), team_privacy=privacy_editor,
                                       team_file_links=allowed["file_links"]))

    @app.get("/team/video/<uuid:work>/interactivity")
    def team_edit_interactivity(work):
        secure = secure_site(origin)
        uid = str(work)
        with tx() as con:
            row = member(con, secure)
            if (row["role"] != "contributor_plus" or not can_edit(con, row, uid)
                    or uid not in eligible_works(con, {uid})):
                abort(404)
        return private(render_template("video_interactivity.html", work=uid,
                                       team_preview=url_for("team_video", work=work)))

    @app.get("/team/video/<uuid:work>/media")
    def team_video(work):
        secure = secure_site(origin)
        same_origin(secure)
        with tx() as con:
            row = member(con, secure)
            item = access(con, row, str(work)).get(str(work))
            if not item:
                abort(404)
            path = root / "objects" / playback(item)["sha256"]
            if not path.is_file():
                abort(404)
        return private(send_file(path, mimetype="video/mp4", conditional=True))

    team_workspaces.register(app, tx, origin, manager, note)
    creator_profiles.register(app, tx, origin, member, mutation, public_listing, entry_payload, read_works,
                              title_for, note)
    return authorize_editor
