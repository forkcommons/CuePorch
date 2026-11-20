"""Private viewer accounts, browser sessions and Watch Later."""

import hashlib
import hmac
import ipaddress
import re
import secrets
import time
import uuid
from urllib.parse import urlsplit

from flask import abort, make_response, render_template, request
from werkzeug.security import check_password_hash, generate_password_hash

from library_analytics import source_origin
from library_discovery import entry_payload
from library_registration import EMAIL

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
"""
SESSION_SECONDS = 30 * 24 * 3600
LOCAL_COOKIE = "cueporch-viewer"
HTTPS_COOKIE = "__Host-cueporch-viewer"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
VIDEO_PATH = re.compile(r"/watch/(?:work|meeting)/[A-Za-z0-9_-]{1,128}")
KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")


def private(body, status=200):
    response = make_response(body, status)
    response.cache_control.no_store = True
    response.cache_control.private = True
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def fields(body):
    name, email, pronouns = body.get("name"), body.get("email"), body.get("pronouns", "")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        abort(400, "Enter a display name.")
    if not isinstance(pronouns, str) or len(pronouns.strip()) > 60:
        abort(400, "Use up to 60 characters for pronouns.")
    return name.strip(), email_key(email), pronouns.strip()


def email_key(value):
    if not isinstance(value, str):
        abort(400, "Enter a valid email address.")
    value = value.strip().casefold()
    if len(value) > 254 or not EMAIL.fullmatch(value) or any(ord(char) < 32 for char in value):
        abort(400, "Enter a valid email address.")
    return value


def password(body, key="password", new=False):
    value = body.get(key)
    if not isinstance(value, str) or not (12 if new else 1) <= len(value) <= 256:
        abort(400, "Use a password of 12 to 256 characters." if new else "Enter your password.")
    return value


def token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def is_loopback(value):
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value or "").is_loopback
    except ValueError:
        return False


def account_view(row):
    return {key: row[key] for key in ("name", "email", "pronouns")}


def register(app, tx, origin_url, operator, listing):
    public_origin = source_origin(origin_url)
    dummy = generate_password_hash(secrets.token_urlsafe(24))
    limit_key = hmac.digest(operator.encode(), b"CuePorch viewer attempts", "sha256")

    def scheme():
        site = source_origin(request.host_url)
        if site == "Unknown":
            abort(400)
        host = urlsplit(site).hostname
        if request.is_secure or (public_origin.startswith("https://") and
                                 source_origin("https://" + request.host) == public_origin):
            return True
        if not is_loopback(host) or not is_loopback(request.remote_addr):
            abort(403, "Open CuePorch using its HTTPS address.")
        return False

    def cookie_name(secure):
        return HTTPS_COOKIE if secure else LOCAL_COOKIE

    def browser_session(secure):
        token = request.cookies.get(cookie_name(secure), "")
        return token_hash(token) if TOKEN.fullmatch(token) else None

    def mutation(secure):
        if not request.is_json or request.headers.get("X-CuePorch-Viewer") != "1":
            abort(403)
        if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            abort(403)
        origin = request.headers.get("Origin")
        expected = source_origin(("https://" if secure else "http://") + request.host)
        if origin is not None and source_origin(origin) != expected:
            abort(403)
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        return body

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
        flags = dict(path="/", secure=secure, httponly=True, samesite="Lax")
        if token:
            response.set_cookie(cookie_name(secure), token, max_age=SESSION_SECONDS, **flags)
        else:
            response.delete_cookie(cookie_name(secure), **flags)
        return response

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
        return private(render_template("viewer_watch_later.html", signed_in=bool(account), rows=rows))

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
