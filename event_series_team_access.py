"""Short-lived, browser-bound sign-in grants for custom Event Series hostnames."""

import secrets
import time

from flask import abort, request
from itsdangerous import BadSignature

from browser_account_security import TOKEN, token_hash

CHALLENGE_TTL = 300
ACCESS_TTL = 43200
BIND_COOKIE = "__Host-cueporch-series-binding"
ACCESS_COOKIE = "__Host-cueporch-series-team"
CONTINUE_COOKIE = "__Host-cueporch-series-continue"


def browser_binding():
    value = request.cookies.get(BIND_COOKIE)
    if value and TOKEN.fullmatch(value):
        return value
    return secrets.token_urlsafe(32)


def continuation():
    value = request.cookies.get(CONTINUE_COOKIE)
    if value is not None and len(value) <= 1024:
        return value
    return None


def challenge(serializer, alias, row, raw_binding):
    return serializer.dumps([alias, row["id"], row["revision"], token_hash(raw_binding)])


def read_challenge(serializer, value):
    if not isinstance(value, str) or len(value) > 1024:
        abort(404)
    try:
        data = serializer.loads(value, max_age=CHALLENGE_TTL)
    except BadSignature:
        abort(404)
    if not isinstance(data, list) or len(data) != 4 or not all(isinstance(part, str) for part in data):
        abort(404)
    return data


SCHEMA = """CREATE TABLE IF NOT EXISTS live_series_team_grant(
    digest TEXT PRIMARY KEY,
    session_digest TEXT NOT NULL,
    alias TEXT NOT NULL,
    series TEXT NOT NULL,
    revision TEXT NOT NULL,
    binding_digest TEXT NOT NULL,
    expires REAL NOT NULL,
    redeemed INTEGER NOT NULL DEFAULT 0
);"""


def issue(con, session_digest, scope):
    alias, series, revision, binding_digest = scope
    now = time.time()
    con.execute("DELETE FROM live_series_team_grant WHERE expires<=?", (now,))
    con.execute(
        "DELETE FROM live_series_team_grant WHERE redeemed=0 AND alias=? AND series=? AND revision=? AND binding_digest=?",
        (alias, series, revision, binding_digest),
    )
    raw = secrets.token_urlsafe(32)
    con.execute(
        "INSERT INTO live_series_team_grant(digest,session_digest,alias,series,revision,binding_digest,expires,redeemed)"
        " VALUES(?,?,?,?,?,?,?,0)",
        (token_hash(raw), session_digest, alias, series, revision, binding_digest, now + CHALLENGE_TTL),
    )
    return raw


def redeem(con, alias, row, code):
    if not TOKEN.fullmatch(code):
        abort(404)
    bind = request.cookies.get(BIND_COOKIE)
    if not bind or not TOKEN.fullmatch(bind):
        abort(404)
    now = time.time()
    cur = con.execute(
        "UPDATE live_series_team_grant SET redeemed=1, expires=?"
        " WHERE digest=? AND alias=? AND series=? AND revision=? AND binding_digest=?"
        " AND redeemed=0 AND expires>?",
        (now + ACCESS_TTL, token_hash(code), alias, row["id"], row["revision"],
         token_hash(bind), now),
    )
    if cur.rowcount != 1:
        abort(404)
    r = con.execute(
        "SELECT session_digest FROM live_series_team_grant WHERE digest=?",
        (token_hash(code),),
    ).fetchone()
    return code, r[0]


def session_digest(con, alias, row):
    code = request.cookies.get(ACCESS_COOKIE)
    if not code or not TOKEN.fullmatch(code):
        return None
    now = time.time()
    r = con.execute(
        "SELECT session_digest FROM live_series_team_grant"
        " WHERE digest=? AND alias=? AND series=? AND revision=?"
        " AND redeemed=1 AND expires>?",
        (token_hash(code), alias, row["id"], row["revision"], now),
    ).fetchone()
    return r[0] if r else None
