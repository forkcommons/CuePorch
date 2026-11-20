"""Workspace session lifetime and revocation for signed-in team members."""

import secrets
from time import time as clock

from flask import abort, render_template, request

from browser_account_security import json_mutation, private, same_origin, secure_site, token_hash, workspace_session


SCHEMA = """
CREATE TABLE IF NOT EXISTS team_session_policy(
 slot INTEGER PRIMARY KEY CHECK(slot=1), revision TEXT NOT NULL, mode TEXT NOT NULL,
 duration INTEGER NOT NULL, unit TEXT NOT NULL);
"""
DEFAULT = dict(revision="", mode="fixed", duration=12, unit="hours")
UNITS = {"minutes": 60, "hours": 3600, "days": 86400}
MAX_SECONDS = 365 * UNITS["days"]


def initialize(con):
    if "last_active" not in {row[1] for row in con.execute("PRAGMA table_info(team_session)")}:
        con.execute("ALTER TABLE team_session ADD COLUMN last_active REAL NOT NULL DEFAULT 0")
    con.execute("CREATE INDEX IF NOT EXISTS team_session_activity ON team_session(last_active)")


def policy(con):
    row = con.execute("SELECT revision,mode,duration,unit FROM team_session_policy WHERE slot=1").fetchone()
    return dict(row) if row else DEFAULT.copy()


def seconds(configuration):
    return configuration["duration"] * UNITS[configuration["unit"]]


def cookie_age(con):
    configuration = policy(con)
    return seconds(configuration) if configuration["mode"] == "fixed" else None


def idle_cutoff(con, now):
    configuration = policy(con)
    return now - seconds(configuration) if configuration["mode"] == "idle" else None


def issue(con, uid, secure):
    token = secrets.token_urlsafe(32)
    now = clock()
    configuration = policy(con)
    con.execute("DELETE FROM team_session WHERE expires<=? OR token_hash=?",
                (now, workspace_session("team", secure)))
    if configuration["mode"] == "idle":
        expiry = float("inf")
        con.execute("DELETE FROM team_session WHERE last_active<=?", (now - seconds(configuration),))
    else:
        expiry = now + seconds(configuration)
    con.execute("INSERT INTO team_session(token_hash,member,expires,last_active) VALUES(?,?,?,?)",
                (token_hash(token), uid, expiry, now))
    return token


def register(app, tx, origin, manager, note):
    @app.get("/team/session-policy")
    def team_session_policy_page():
        secure_site(origin)
        return private(render_template("team_session_policy.html"))

    @app.route("/api/team/session-policy", methods=("GET", "PUT", "POST"))
    def team_session_policy():
        secure = secure_site(origin)
        same_origin(secure)
        body = json_mutation(secure, "X-CuePorch-Team") if request.method not in ("GET", "HEAD") else None
        with tx() as con:
            actor = manager(con)
            current = policy(con)
            if body is None:
                return private(current)
            if request.method == "PUT":
                if body.get("revision") != current["revision"]:
                    abort(409, "Session settings changed. Reload before saving.")
                mode, duration, unit = body.get("mode"), body.get("duration"), body.get("unit")
                if (mode not in ("fixed", "idle") or not isinstance(unit, str) or unit not in UNITS
                        or type(duration) is not int or not 1 <= duration <= MAX_SECONDS // UNITS[unit]):
                    abort(400, "Choose a period between one minute and 365 days.")
                if (mode, duration, unit) == (current["mode"], current["duration"], current["unit"]):
                    return private(current | {"signed_out": False})
                revision = secrets.token_hex(16)
                con.execute("INSERT OR REPLACE INTO team_session_policy VALUES(1,?,?,?,?)",
                            (revision, mode, duration, unit))
                current = dict(revision=revision, mode=mode, duration=duration, unit=unit)
                note(con, actor, "session_policy_changed", "workspace", f"{mode}: {duration} {unit}")
            else:
                note(con, actor, "all_team_sessions_ended", "workspace")
            con.execute("DELETE FROM team_session")
            con.execute("DELETE FROM team_saml_session")
        return private(current | {"signed_out": True})
