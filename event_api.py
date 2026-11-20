"""Scoped developer credentials and the version 1 event API."""

import hmac
import json
import re
import secrets
from time import time
import uuid

from flask import abort, g, request
from werkzeug.exceptions import HTTPException

from browser_account_security import TOKEN, private, secure_site, token_hash
from team_accounts import note


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_api_credential(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT NOT NULL, scopes TEXT NOT NULL,
 created INTEGER NOT NULL, expires INTEGER NOT NULL, revoked INTEGER,
 rate_minute INTEGER NOT NULL DEFAULT -1, rate_used INTEGER NOT NULL DEFAULT 0
);
"""
SCOPES = ("read", "schedule", "produce", "registrations")
CREDENTIAL = re.compile(r"Bearer cp_event_([a-f0-9]{32})\.(" + TOKEN.pattern + r")")
LIMIT = 120

ROUTES = (
    ("", "list_events", {"GET": "read", "POST": "schedule"}),
    ("/<uid>", "event_details", {"GET": "read"}),
    ("/<uid>", "revise_event", {"PUT": "schedule"}),
    ("/<uid>/registration", "event_registration", {"GET": "registrations", "PUT": "registrations"}),
    ("/<uid>/registration/export", "export_event_registration", {"GET": "registrations"}),
    ("/<uid>/<int:opens>/registrants", "add_event_registrant", {"POST": "registrations"}),
    ("/<uid>/<int:opens>/registrants/<registrant>/join", "revoke_event_join", {"DELETE": "registrations"}),
    ("/<uid>/<int:opens>/health", "event_health", {"GET": "read"}),
    ("/<uid>/<int:opens>/encoder", "prepare_external_encoder", {"POST": "produce"}),
    ("/<uid>/<int:opens>/preview", "encoder_preview", {"GET": "produce", "POST": "produce"}),
    ("/<uid>/<int:opens>/delivery", "withdraw_event", {"DELETE": "produce"}),
    ("/<uid>/<int:opens>/program", "event_program_settings", {"GET": "produce", "PUT": "produce"}),
)


def describe(row):
    return dict(id=row["id"], name=row["name"], scopes=json.loads(row["scopes"]),
                created_at=row["created"], expires_at=row["expires"], revoked_at=row["revoked"])


def claim(tx, scope):
    match = CREDENTIAL.fullmatch(request.headers.get("Authorization", ""))
    if not match:
        abort(401, "Supply a current CuePorch event API token.")
    uid, secret = match.groups()
    now = int(time())
    minute = now // 60
    with tx() as con:
        row = con.execute("SELECT * FROM live_api_credential WHERE id=?", (uid,)).fetchone()
        if (not row or row["revoked"] is not None or row["expires"] <= now or
                not hmac.compare_digest(row["digest"], token_hash(secret))):
            abort(401, "Supply a current CuePorch event API token.")
        if scope not in json.loads(row["scopes"]):
            abort(403, f"This endpoint requires the {scope} scope.")
        used = row["rate_used"] if row["rate_minute"] == minute else 0
        if used >= LIMIT:
            abort(429, "This event API token reached its request limit. Try again after the minute resets.")
        con.execute("UPDATE live_api_credential SET rate_minute=?,rate_used=? WHERE id=?", (minute, used + 1, uid))
    return uid


def guarded(tx, origin, view, scopes):
    def authorize(**parameters):
        method = "GET" if request.method == "HEAD" else request.method
        try:
            secure_site(origin)
            uid = claim(tx, scopes[method])
            g.event_api_actor = "event-api:" + uid
            return private(view(**parameters))
        except HTTPException as error:
            response = private({"error": {"status": error.code, "message": error.description}}, error.code)
            if error.code == 401:
                response.headers["WWW-Authenticate"] = 'Bearer realm="CuePorch live events"'
            if error.code == 429:
                response.headers["Retry-After"] = str(60 - int(time()) % 60)
            return response
        finally:
            g.pop("event_api_actor", None)
    return authorize


def register(app, tx, origin, owner):
    @app.route("/api/operator/live-api/credentials", methods=["GET", "POST"])
    def event_api_credentials():
        secure_site(origin)
        actor = owner()
        if request.method == "GET":
            with tx() as con:
                rows = con.execute("SELECT * FROM live_api_credential ORDER BY created DESC,id").fetchall()
            return private(dict(credentials=[describe(row) for row in rows], scopes=SCOPES, requests_per_minute=LIMIT))
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get("name"), str):
            abort(422, "Enter a name and at least one scope.")
        name = body["name"].strip()
        scopes = body.get("scopes")
        days = body.get("expires_in_days", 90)
        if (not 1 <= len(name) <= 80 or any(ord(char) < 32 or ord(char) == 127 for char in name) or
                not isinstance(scopes, list) or not scopes or any(scope not in SCOPES for scope in scopes) or
                type(days) is not int or not 1 <= days <= 365):
            abort(422, "Enter a name, valid scopes and an expiry in 1 to 365 days.")
        uid, secret, now = uuid.uuid4().hex, secrets.token_urlsafe(32), int(time())
        with tx() as con:
            con.execute("INSERT INTO live_api_credential(id,name,digest,scopes,created,expires) VALUES(?,?,?,?,?,?)",
                        (uid, name, token_hash(secret), json.dumps(sorted(set(scopes))), now, now + days * 86400))
            note(con, actor, "live_api_credential_created", uid, name)
            record = con.execute("SELECT * FROM live_api_credential WHERE id=?", (uid,)).fetchone()
        return private(dict(describe(record), token=f"cp_event_{uid}.{secret}"), 201)

    @app.delete("/api/operator/live-api/credentials/<uid>")
    def revoke_event_api_credential(uid):
        secure_site(origin)
        actor = owner()
        with tx() as con:
            record = con.execute("SELECT revoked FROM live_api_credential WHERE id=?", (uid,)).fetchone()
            if not record:
                abort(404)
            if record["revoked"] is None:
                con.execute("UPDATE live_api_credential SET revoked=? WHERE id=?", (int(time()), uid))
                note(con, actor, "live_api_credential_revoked", uid)
        return private("", 204)

    for suffix, name, scopes in ROUTES:
        app.add_url_rule("/api/v1/live-events" + suffix, "live_api_" + name,
                         guarded(tx, origin, app.view_functions[name], scopes), methods=list(scopes))
