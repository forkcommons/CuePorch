"""Team invitations, member sessions and private Library access."""

import hashlib
import hmac
import secrets
import time
import uuid

from flask import abort, render_template, request, send_file, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from browser_account_security import (TOKEN, cookie_session, email_key, json_mutation, password, private,
                                      same_origin, secure_site, set_session_cookie, token_hash)
from library_privacy import blocked, playback


SCHEMA = """
CREATE TABLE IF NOT EXISTS team_member(
 id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
 password_hash TEXT NOT NULL, joined REAL NOT NULL, removed REAL);
CREATE UNIQUE INDEX IF NOT EXISTS team_active_email ON team_member(email) WHERE removed IS NULL;
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
CREATE TABLE IF NOT EXISTS team_owned_work(work TEXT PRIMARY KEY, member TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS team_owned_work_member ON team_owned_work(member);
CREATE TABLE IF NOT EXISTS team_membership_event(
 sequence INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
 subject TEXT NOT NULL, detail TEXT, at REAL NOT NULL);
"""
ROLES = ("admin", "contributor", "viewer")
SESSION_SECONDS = 12 * 3600
INVITE_SECONDS = 7 * 86400
SESSION_MARKER = "team-session"
MUTATION_HEADER = "X-CuePorch-Team"


def cookie_name(secure):
    return "__Host-cueporch-team" if secure else "cueporch-team"


def current(con, secure):
    return con.execute("""SELECT m.id,m.name,m.email,m.role FROM team_session s JOIN team_member m ON m.id=s.member
        WHERE s.token_hash=? AND s.expires>? AND m.removed IS NULL""",
        (cookie_session(cookie_name(secure)), time.time())).fetchone()


def actor_id(member):
    return "team:" + member["id"]


def note(con, actor, action, subject, detail=None):
    con.execute("INSERT INTO team_membership_event(actor,action,subject,detail,at) VALUES(?,?,?,?,?)",
                (actor, action, subject, detail, time.time()))


def role_from(body):
    role = body.get("role")
    if role not in ROLES:
        abort(400, "Choose Administrator, Contributor or Viewer.")
    return role


def member_role(con, uid):
    row = con.execute("SELECT role FROM team_member WHERE id=? AND removed IS NULL", (uid,)).fetchone()
    if row is None:
        abort(404, "This member is no longer active.")
    return row[0]


def issue_session(con, uid, secure):
    token = secrets.token_urlsafe(32)
    now = time.time()
    con.execute("DELETE FROM team_session WHERE expires<=? OR token_hash=?",
                (now, cookie_session(cookie_name(secure))))
    con.execute("INSERT INTO team_session VALUES(?,?,?)", (token_hash(token), uid, now + SESSION_SECONDS))
    return token


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


def inherited_works(con, member):
    rows = con.execute("""WITH RECURSIVE granted(id) AS (
        SELECT f.id FROM team_folder_grant g JOIN library_folder f ON f.id=g.folder
        WHERE g.member=? AND NOT EXISTS (
            SELECT 1 FROM library_deleted_item d WHERE d.kind='folder' AND d.item=f.id)
        UNION
        SELECT f.id FROM library_folder f JOIN granted g ON f.parent=g.id
        WHERE NOT EXISTS (SELECT 1 FROM library_deleted_item d WHERE d.kind='folder' AND d.item=f.id)
    ) SELECT p.work FROM library_placement p JOIN granted g ON g.id=p.folder""", (member,))
    return {row[0] for row in rows}


def register(app, tx, origin, key, institution, operator_name, read_works, title_for, root):
    rate_key = hmac.digest(key.encode(), b"CuePorch team attempts", "sha256")
    dummy = generate_password_hash(secrets.token_urlsafe(24))

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

    def session_response(body, secure, token=None, status=200):
        return set_session_cookie(private(body, status), cookie_name(secure), secure, SESSION_SECONDS, token)

    def eligible_works(con, ids=None):
        return {work["id"]: work for work in read_works() if (ids is None or work["id"] in ids)
                and playback(work) and grant_available(con, work["id"])}

    def shared_ids(con, uid):
        return {row[0] for row in con.execute("SELECT work FROM team_work_grant WHERE member=?", (uid,))}

    @app.get("/team/join")
    def team_join_page():
        secure_site(origin)
        return private(render_template("team_sign_in.html", institution=institution, joining=True))

    @app.get("/team")
    def team_sign_in_page():
        secure_site(origin)
        return private(render_template("team_sign_in.html", institution=institution, joining=False))

    @app.get("/team/members")
    def team_members_page():
        secure_site(origin)
        return private(render_template("team_members.html", institution=institution))

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
                con.execute("DELETE FROM team_session WHERE token_hash=?", (cookie_session(cookie_name(secure)),))
            return session_response("", secure, status=204)
        email, entered = email_key(body.get("email")), password(body)
        with tx() as con:
            limit(con, "sign-in", email)
            row = con.execute("SELECT id,password_hash FROM team_member WHERE email=? AND removed IS NULL",
                              (email,)).fetchone()
        if not check_password_hash(row["password_hash"] if row else dummy, entered) or row is None:
            abort(401, "The email or password did not match an active team member.")
        with tx() as con:
            logged_in = con.execute("""SELECT id,email,name,role FROM team_member
                WHERE id=? AND password_hash=? AND removed IS NULL""", (row["id"], row["password_hash"])).fetchone()
            if logged_in is None:
                abort(401, "The email or password did not match an active team member.")
            token = issue_session(con, row["id"], secure)
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
            token = issue_session(con, uid, secure)
        return session_response(dict(id=uid, email=pending["email"], name=name, role=pending["role"]), secure, token, 201)

    @app.get("/api/team/members")
    def team_members():
        with tx() as con:
            manager(con)
            members = [dict(row) for row in con.execute("""SELECT id,email,name,role,joined,removed
                FROM team_member ORDER BY removed IS NOT NULL,name,email""")]
            invitations = [dict(row) for row in con.execute("SELECT id,email,role,expires FROM team_invitation ORDER BY email")]
        return private(dict(members=members, invitations=invitations))

    @app.post("/api/team/invitations")
    def team_invite():
        secure = secure_site(origin)
        body = mutation(secure)
        email, role = email_key(body.get("email")), role_from(body)
        uid, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        with tx() as con:
            actor = manager(con)
            if con.execute("SELECT 1 FROM team_member WHERE email=? AND removed IS NULL", (email,)).fetchone():
                abort(409, "This email already has an active team membership.")
            con.execute("""INSERT INTO team_invitation VALUES(?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET
                id=excluded.id,role=excluded.role,token_hash=excluded.token_hash,expires=excluded.expires""",
                (uid, email, role, token_hash(token), time.time() + INVITE_SECONDS))
            note(con, actor, "invited", uid, role)
        return private(dict(id=uid, path=url_for("team_join_page") + "#" + token), 201)

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
            if request.method == "DELETE":
                con.execute("UPDATE team_member SET removed=?,password_hash='' WHERE id=?", (time.time(), uid))
                con.execute("DELETE FROM team_session WHERE member=?", (uid,))
                con.execute("DELETE FROM team_work_grant WHERE member=?", (uid,))
                con.execute("DELETE FROM team_folder_grant WHERE member=?", (uid,))
                note(con, actor, "removed", uid, previous)
            elif (role := role_from(body)) != previous:
                con.execute("UPDATE team_member SET role=? WHERE id=?", (role, uid))
                note(con, actor, "role_changed", uid, previous + " → " + role)
        return private("", 204)

    @app.get("/api/team/audit")
    def team_audit():
        with tx() as con:
            manager(con)
            rows = [dict(row) for row in con.execute("SELECT * FROM team_membership_event ORDER BY sequence DESC LIMIT 200")]
        return private(dict(events=rows))

    @app.get("/api/team/members/<uuid:account>/works")
    def team_share_options(account):
        uid = str(account)
        with tx() as con:
            manager(con)
            member_role(con, uid)
            shared = shared_ids(con, uid)
            rows = [dict(id=work_id, title=title_for(con, work), shared=work_id in shared)
                    for work_id, work in eligible_works(con).items()]
            folders_shared = {row[0] for row in con.execute("SELECT folder FROM team_folder_grant WHERE member=?", (uid,))}
            folders = [dict(row, shared=row["id"] in folders_shared) for row in visible_folders(con)]
        return private(dict(works=sorted(rows, key=lambda row: row["title"].casefold()), folders=folders))

    @app.route("/api/team/members/<uuid:account>/folders/<folder>", methods=("PUT", "DELETE"))
    def team_update_folder_sharing(account, folder):
        mutation(secure_site(origin))
        uid = str(account)
        with tx() as con:
            actor = manager(con)
            member_role(con, uid)
            if request.method == "PUT":
                if not any(row["id"] == folder for row in visible_folders(con)):
                    abort(404, "This folder is unavailable for team viewing.")
                result = con.execute("INSERT OR IGNORE INTO team_folder_grant VALUES(?,?)", (uid, folder))
                event = "folder_shared"
            else:
                result = con.execute("DELETE FROM team_folder_grant WHERE member=? AND folder=?", (uid, folder))
                event = "folder_unshared"
            if result.rowcount:
                note(con, actor, event, uid, folder)
        return private("", 204)

    @app.route("/api/team/members/<uuid:account>/works/<uuid:work>", methods=("PUT", "DELETE"))
    def team_update_sharing(account, work):
        mutation(secure_site(origin))
        uid, work_id = str(account), str(work)
        with tx() as con:
            actor = manager(con)
            member_role(con, uid)
            if request.method == "PUT":
                if work_id not in eligible_works(con, {work_id}):
                    abort(404, "This video is unavailable for team viewing.")
                result = con.execute("INSERT OR IGNORE INTO team_work_grant VALUES(?,?)", (uid, work_id))
                event = "video_shared"
            else:
                result = con.execute("DELETE FROM team_work_grant WHERE member=? AND work=?", (uid, work_id))
                event = "video_unshared"
            if result.rowcount:
                note(con, actor, event, uid, work_id)
        return private("", 204)

    def access(con, row, work_id=None):
        if row["role"] == "admin":
            selected = {work_id} if work_id else None
        else:
            selected = shared_ids(con, row["id"]) | inherited_works(con, row["id"])
            if row["role"] == "contributor":
                selected.update(item[0] for item in con.execute("SELECT work FROM team_owned_work WHERE member=?", (row["id"],)))
            if work_id:
                selected.intersection_update({work_id})
        return eligible_works(con, selected)

    @app.get("/api/team/library")
    def team_library():
        secure = secure_site(origin)
        same_origin(secure)
        with tx() as con:
            row = member(con, secure)
            works = [dict(id=uid, title=title_for(con, work), watch=url_for("team_watch", work=uid))
                     for uid, work in access(con, row).items()]
        return private(dict(works=sorted(works, key=lambda item: item["title"].casefold())))

    @app.get("/team/video/<uuid:work>")
    def team_watch(work):
        secure = secure_site(origin)
        with tx() as con:
            row = member(con, secure)
            item = access(con, row, str(work)).get(str(work))
            if not item:
                abort(404)
            title = title_for(con, item)
        return private(render_template("team_watch.html", title=title, media=url_for("team_video", work=work)))

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
