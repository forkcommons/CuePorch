"""Private viewer accounts, browser sessions and Watch Later."""

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid

from flask import abort, render_template, request
from werkzeug.security import check_password_hash, generate_password_hash

import account_email
import account_email_factor
import account_recovery
import account_totp
import audience_comments
import audience_library
from browser_account_security import (email_key, json_mutation, password, private, secure_site, token_hash,
                                      workspace_session, workspace_session_cookie)
import community_analytics
import content_rating
import creator_profiles
from library_analytics import source_origin
from library_discovery import PAGE_SIZE, entry_payload, ordered

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
CREATE TABLE IF NOT EXISTS audience_view_preferences(
 account TEXT PRIMARY KEY, maturity TEXT NOT NULL);
""" + audience_library.SCHEMA + audience_comments.SCHEMA + community_analytics.SCHEMA
SESSION_SECONDS = 30 * 24 * 3600
VIEWER_RETURN_PATH = re.compile(r"/(?:watch/(?:work|meeting)/[A-Za-z0-9_-]{1,128}|creators/[a-z0-9-]{3,40}|channels/[a-z0-9-]{1,64}|watch/feed)")
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


def account_preferences(con, account):
    saved = con.execute("SELECT maturity FROM audience_view_preferences WHERE account=?", (account,)).fetchone()
    return json.loads(saved["maturity"]) if saved else content_rating.PREFERENCE_DEFAULT


def register(app, tx, origin_url, operator, listing, action, staff):
    public_origin = source_origin(origin_url)
    dummy = generate_password_hash(secrets.token_urlsafe(24))
    limit_key = hmac.digest(operator.encode(), b"CuePorch viewer attempts", "sha256")

    def scheme():
        return secure_site(public_origin)

    def browser_session(secure):
        return workspace_session("viewer", secure)

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

    def viewing_preferences(con):
        secure = secure_site(public_origin, required=False)
        session = browser_session(secure) if secure is not None else None
        account = current(con, session, required=False) if session else None
        return account_preferences(con, account["id"]) if account else content_rating.PREFERENCE_DEFAULT

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
        return workspace_session_cookie(response, "viewer", secure, SESSION_SECONDS, token)

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

    def rotate_security_session(con, uid, secure):
        con.execute("DELETE FROM audience_session WHERE account=?", (uid,))
        return issue(con, uid, None)

    def security_account(con, secure):
        return current(con, browser_session(secure))

    authenticator = account_totp.register(
        app, tx, "viewer", operator, scheme, mutation, security_account,
        limit, rotate_security_session, cookie,
    )
    email_security = account_email.register(app, tx, "viewer", operator, public_origin, scheme, mutation,
                                            security_account, limit)
    email_factor = account_email_factor.register(app, tx, email_security, authenticator, scheme, mutation,
                                                security_account, limit, rotate_security_session, cookie)

    def reset_password(con, uid, encoded):
        con.execute("UPDATE audience_account SET password_hash=? WHERE id=?", (encoded, uid))
        con.execute("DELETE FROM audience_session WHERE account=?", (uid,))

    account_recovery.register(
        app, tx, "viewer", public_origin, email_security, scheme, mutation,
        lambda con, address: con.execute("SELECT * FROM audience_account WHERE email=?", (address,)).fetchone(),
        limit, authenticator, reset_password,
    )

    @app.get("/account")
    def viewer_account_page():
        scheme()
        destination = request.args.get("next", "")
        return private(render_template("viewer_account.html", content_choices=content_rating.PREFERENCE_CHOICES,
                                       can_recover=bool(email_security.config),
                                       destination=destination if VIEWER_RETURN_PATH.fullmatch(destination) else ""))

    @app.route("/api/viewer/preferences", methods=("GET", "PUT"))
    def viewer_preferences():
        secure = scheme()
        session = browser_session(secure)
        body = mutation(secure) if request.method == "PUT" else None
        if isinstance(body, dict) and "autoplay" in body:
            autoplay = body["autoplay"]
            if autoplay is not None and type(autoplay) is not bool:
                abort(400)
        settings = content_rating.validate_preference(body) if request.method == "PUT" else None
        with tx() as con:
            account = current(con, session)["id"]
            if settings is None:
                return private(account_preferences(con, account))
            if isinstance(body, dict) and "autoplay" in body:
                if body["autoplay"] is None:
                    settings.pop("autoplay", None)
                else:
                    settings["autoplay"] = body["autoplay"]
            else:
                existing = account_preferences(con, account)
                if isinstance(existing, dict) and existing.get("autoplay") is not None:
                    settings["autoplay"] = existing["autoplay"]
            con.execute("""INSERT INTO audience_view_preferences VALUES(?,?) ON CONFLICT(account)
                           DO UPDATE SET maturity=excluded.maturity""", (account, json.dumps(settings)))
        return private(settings)

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
                con.execute("DELETE FROM audience_creator_follow WHERE account=?", (row["id"],))
                con.execute("DELETE FROM audience_category_follow WHERE account=?", (row["id"],))
                con.execute("DELETE FROM audience_watch_later WHERE account=?", (row["id"],))
                con.execute("DELETE FROM audience_channel_follow WHERE account=?", (row["id"],))
                con.execute("DELETE FROM audience_view_preferences WHERE account=?", (row["id"],))
                authenticator.forget(con, row["id"])
                email_factor.forget(con, row["id"])
                email_security.forget(con, row["id"])
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
            if email != row["email"]:
                if email_factor.enabled(con, row["id"]):
                    abort(409, "Turn off email sign-in before changing the address.")
                email_security.forget(con, row["id"])
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
            needs_mail, prepared = email_factor.check(con, row, body)
            if not needs_mail:
                token = issue(con, row["id"], old)
        if needs_mail:
            return email_factor.deliver(tx, row, prepared)
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
            reset_password(con, row["id"], encoded)
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
    creator_profiles.register_audience(app, tx, viewer, lambda: mutation(scheme()), listing, entry_payload,
                                       ordered, PAGE_SIZE, account_preferences)
    return viewing_preferences
