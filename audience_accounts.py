"""Private viewer accounts, browser sessions and Watch Later."""

import hashlib
import hmac
import re
import secrets
import time
import uuid

from flask import abort, render_template, request
from werkzeug.security import check_password_hash, generate_password_hash

import audience_comments
import audience_library
from browser_account_security import (cookie_session, email_key, json_mutation, password, private, secure_site,
                                      set_session_cookie, token_hash)
import community_analytics
from library_analytics import source_origin
from library_discovery import entry_payload

SCHEMA = """
CREATE TABLE IF NOT EXISTS audience_account(
 id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 pronouns TEXT NOT NULL, password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audience_session(
 token_hash TEXT PRIMARY KEY, account TEXT NOT NULL, expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS audience_session_account ON audience_session(account);
CREATE INDEX IF NOT EXISTS audience_session_expiry ON audience_session(expires);
CREATE TABLE IF NOT EXISTS audience_auth_attempt(
 bucket TEXT PRIMARY KEY, expires REAL NOT NULL, attempts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS audience_attempt_expiry ON audience_auth_attempt(expires);
CREATE TABLE IF NOT EXISTS audience_watch_later(
 account TEXT NOT NULL, kind TEXT NOT NULL, slug TEXT NOT NULL, saved REAL NOT NULL,
 PRIMARY KEY(account,kind,slug));
""" + audience_library.SCHEMA + audience_comments.SCHEMA + community_analytics.SCHEMA
SESSION_SECONDS = 30 * 24 * 3600
LOCAL_COOKIE = "cueporch-viewer"
HTTPS_COOKIE = "__Host-cueporch-viewer"
VIDEO_PATH = re.compile(r"/watch/(?:work|meeting)/[A-Za-z0-9_-]{1,128}")
KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")


def fields(body):
    name, email, pronouns = body.get("name"), body.get("email"), body.get("pronouns", "")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        abort(400, "Enter a display name.")
    if not isinstance(pronouns, str) or len(pronouns.strip()) > 60:
        abort(400, "Use up to 60 characters for pronouns.")
    return name.strip(), email_key(email), pronouns.strip()


def account_view(row):
    return {key: row[key] for key in ("name", "email", "pronouns")}


def register(app, tx, origin_url, operator, listing, action, staff):
    public_origin = source_origin(origin_url)
    dummy = generate_password_hash(secrets.token_urlsafe(24))
    limit_key = hmac.digest(operator.encode(), b"CuePorch viewer attempts", "sha256")

    def scheme():
        return secure_site(public_origin)

    def cookie_name(secure):
        return HTTPS_COOKIE if secure else LOCAL_COOKIE

    def browser_session(secure):
        return cookie_session(cookie_name(secure))

    def mutation(secure):
        return json_mutation(secure, "X-CuePorch-Viewer")

    def current(con, session, required=True):
        row = con.execute(
            """SELECT a.* FROM audience_session s JOIN audience_account a ON a.id=s.account
               WHERE s.token_hash=? AND s.expires>?""", (session, time.time()),
        ).fetchone()
        if row is None and required:
            abort(401, "Sign in to your viewer account.")
        return row

    def limit(con, action, subject=None):
        now = time.time()
        source = request.remote_addr or ""
        con.execute("DELETE FROM audience_auth_attempt WHERE expires<=?", (now,))
        checks = [(source, 150, 600)] if action != "signup" else [(source, 30, 3600)]
        if subject:
            checks.append((source + "|" + subject, 12, 600))
        for subject_key, maximum, window in checks:
            bucket = hmac.new(limit_key, (action + "|" + subject_key).encode(), hashlib.sha256).hexdigest()
            row = con.execute("SELECT attempts FROM audience_auth_attempt WHERE bucket=?", (bucket,)).fetchone()
            if row and row["attempts"] >= maximum:
                abort(429, "Too many attempts. Try again later.")
            con.execute(
                """INSERT INTO audience_auth_attempt VALUES(?,?,1) ON CONFLICT(bucket)
                   DO UPDATE SET attempts=attempts+1""", (bucket, now + window),
            )

    def issue(con, uid, old_session):
        now = time.time()
        token = secrets.token_urlsafe(32)
        con.execute("DELETE FROM audience_session WHERE expires<=? OR token_hash=?", (now, old_session))
        con.execute("INSERT INTO audience_session VALUES(?,?,?)", (token_hash(token), uid, now + SESSION_SECONDS))
        return token

    def cookie(response, secure, token=None):
        return set_session_cookie(response, cookie_name(secure), secure, SESSION_SECONDS, token)

    def reauthenticate(session, entered):
        with tx() as con:
            row = current(con, session)
            limit(con, "reauthenticate", row["id"])
        if not check_password_hash(row["password_hash"], entered):
            abort(401, "The current password did not match.")
        return row["password_hash"]

    def same_password(row, previous_hash):
        if previous_hash is not None and not hmac.compare_digest(row["password_hash"], previous_hash):
            abort(409, "Your account changed. Try again.")

    @app.get("/account")
    def viewer_account_page():
        scheme()
        destination = request.args.get("next", "")
        return private(render_template("viewer_account.html", destination=destination if VIDEO_PATH.fullmatch(destination) else ""))

    @app.route("/api/viewer/account", methods=("GET", "PUT", "DELETE"))
    def viewer_account():
        secure = scheme()
        session = browser_session(secure)
        if request.method in ("GET", "HEAD"):
            with tx() as con:
                return private(account_view(current(con, session)))
        body = mutation(secure)
        if request.method == "DELETE":
            previous_hash = reauthenticate(session, password(body))
            with tx() as con:
                row = current(con, session)
                same_password(row, previous_hash)
                audience_comments.remove_account(con, row["id"])
                audience_library.remove_account(con, row["id"])
                con.execute("DELETE FROM audience_watch_later WHERE account=?", (row["id"],))
                con.execute("DELETE FROM audience_session WHERE account=?", (row["id"],))
                con.execute("DELETE FROM audience_account WHERE id=?", (row["id"],))
            return cookie(private("", 204), secure)
        name, email, pronouns = fields(body)
        with tx() as con:
            previous_email = current(con, session)["email"]
        previous_hash = reauthenticate(session, password(body, "current_password")) if email != previous_email else None
        with tx() as con:
            row = current(con, session)
            same_password(row, previous_hash)
            if email != row["email"] and previous_hash is None:
                abort(409, "Your account changed. Try again.")
            if con.execute("SELECT 1 FROM audience_account WHERE email=? AND id<>?", (email, row["id"])).fetchone():
                abort(409, "That email address is already in use.")
            con.execute("UPDATE audience_account SET name=?,email=?,pronouns=? WHERE id=?", (name, email, pronouns, row["id"]))
        return private(dict(name=name, email=email, pronouns=pronouns))

    @app.post("/api/viewer/signup")
    def viewer_signup():
        secure = scheme()
        body = mutation(secure)
        name, email, pronouns = fields(body)
        entered = password(body, new=True)
        with tx() as con:
            limit(con, "signup")
        encoded = generate_password_hash(entered)
        uid = uuid.uuid4().hex
        with tx() as con:
            if con.execute("SELECT 1 FROM audience_account WHERE email=?", (email,)).fetchone():
                abort(409, "That email address is already in use.")
            con.execute("INSERT INTO audience_account VALUES(?,?,?,?,?)", (uid, email, name, pronouns, encoded))
            token = issue(con, uid, browser_session(secure))
        return cookie(private(dict(name=name, email=email, pronouns=pronouns), 201), secure, token)

    @app.route("/api/viewer/session", methods=("POST", "DELETE"))
    def viewer_session():
        secure = scheme()
        old = browser_session(secure)
        body = mutation(secure)
        if request.method == "DELETE":
            with tx() as con:
                con.execute("DELETE FROM audience_session WHERE token_hash=?", (old,))
            return cookie(private("", 204), secure)
        email, entered = email_key(body.get("email")), password(body)
        with tx() as con:
            limit(con, "login", email)
            row = con.execute("SELECT * FROM audience_account WHERE email=?", (email,)).fetchone()
        expected = row["password_hash"] if row else dummy
        correct = check_password_hash(expected, entered)
        if not correct or row is None:
            abort(401, "The email or password did not match.")
        with tx() as con:
            row = con.execute("SELECT * FROM audience_account WHERE id=? AND email=?", (row["id"], email)).fetchone()
            if row is None or row["password_hash"] != expected:
                abort(401, "The email or password did not match.")
            token = issue(con, row["id"], old)
        return cookie(private(account_view(row)), secure, token)

    @app.put("/api/viewer/password")
    def viewer_password():
        secure = scheme()
        session = browser_session(secure)
        body = mutation(secure)
        new = password(body, "new_password", new=True)
        previous_hash = reauthenticate(session, password(body, "current_password"))
        encoded = generate_password_hash(new)
        with tx() as con:
            row = current(con, session)
            same_password(row, previous_hash)
            con.execute("UPDATE audience_account SET password_hash=? WHERE id=?", (encoded, row["id"]))
            con.execute("DELETE FROM audience_session WHERE account=?", (row["id"],))
            token = issue(con, row["id"], None)
        return cookie(private("", 204), secure, token)

    @app.get("/watch/later")
    def watch_later_page():
        secure = scheme()
        with tx() as con:
            account = current(con, browser_session(secure), required=False)
            available = listing(con) if account else {}
            rows = []
            if account:
                for kind, slug in con.execute(
                    """SELECT kind,slug FROM audience_watch_later WHERE account=?
                       ORDER BY saved DESC,kind,slug""", (account["id"],),
                ):
                    if (entry := available.get((kind, slug))) is not None:
                        rows.append(entry_payload(entry))
        return private(render_template("viewer_watch_later.html", signed_in=bool(account), rows=rows,
                                       title="Watch Later", endpoint="watch-later",
                                       empty_label="Your Watch Later list is empty.", remove_label="Remove"))

    @app.route("/api/viewer/watch-later/<any(work,meeting):kind>/<slug>", methods=("GET", "PUT", "DELETE"))
    def viewer_watch_later(kind, slug):
        secure = scheme()
        if not KEY.fullmatch(slug):
            abort(404)
        if request.method not in ("GET", "HEAD"):
            mutation(secure)
        with tx() as con:
            account = current(con, browser_session(secure))
            key = account["id"], kind, slug
            if request.method == "DELETE":
                con.execute("DELETE FROM audience_watch_later WHERE account=? AND kind=? AND slug=?", key)
                return private(dict(saved=False))
            if (kind, slug) not in listing(con, (kind, slug)):
                abort(404)
            if request.method == "PUT":
                con.execute("INSERT OR IGNORE INTO audience_watch_later VALUES(?,?,?,?)", (*key, time.time()))
                return private(dict(saved=True))
            saved = con.execute("SELECT 1 FROM audience_watch_later WHERE account=? AND kind=? AND slug=?", key).fetchone()
            return private(dict(saved=bool(saved)))

    def viewer(con, required=True):
        return current(con, browser_session(scheme()), required)

    audience_library.register(app, tx, viewer, lambda: mutation(scheme()), listing, private)
    audience_comments.register(app, tx, viewer, lambda: mutation(scheme()), listing, private, limit, action, staff)
