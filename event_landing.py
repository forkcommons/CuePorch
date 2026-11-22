"""Live event landing page API module."""

import json
from uuid import uuid4

from flask import abort, request

from library_appearance import PALETTE

DEFAULTS = {
    "enabled": False,
    "accent": "#2b7e7d",
    "theme": "light",
    "layout": "stacked",
    "show_logo": True,
    "show_poster": False,
}

SCHEMA = """CREATE TABLE IF NOT EXISTS live_event_landing (
    uid TEXT PRIMARY KEY,
    revision TEXT NOT NULL,
    settings TEXT NOT NULL
);"""

UPSERT = """INSERT INTO live_event_landing (uid, revision, settings)
VALUES (?, ?, ?)
ON CONFLICT(uid) DO UPDATE SET revision=excluded.revision, settings=excluded.settings"""


def _payload(row):
    if row is None:
        return {"revision": None, "settings": dict(DEFAULTS)}
    merged = dict(DEFAULTS)
    merged.update(json.loads(row[1] or "{}"))
    return {"revision": row[0], "settings": merged}


def _valid_settings(data):
    if not isinstance(data, dict) or set(data) != {"revision", "settings"}:
        abort(422)
    settings = data["settings"]
    if not isinstance(settings, dict) or set(settings) != set(DEFAULTS):
        abort(422)
    for key in ("enabled", "show_logo", "show_poster"):
        if type(settings[key]) is not bool:
            abort(422)
    if settings["theme"] not in ("light", "dark"):
        abort(422)
    if settings["layout"] not in ("stacked", "split"):
        abort(422)
    value = settings["accent"]
    if not isinstance(value, str) or not PALETTE.fullmatch(value):
        abort(422)
    return settings


def viewer(con, uid, embed):
    if embed:
        return None
    row = con.execute(
        "SELECT settings FROM live_event_landing WHERE uid = ?", (uid,)
    ).fetchone()
    if row is None:
        return None
    settings = dict(DEFAULTS)
    settings.update(json.loads(row[0] or "{}"))
    if not settings["enabled"]:
        return None
    return settings


def register(app, tx, staff, read):
    @app.route("/api/live-events/<uid>/landing", methods=("GET", "PUT"))
    def landing(uid):
        staff()
        with tx() as con:
            read(con, uid)
            landing_row = con.execute(
                "SELECT revision, settings FROM live_event_landing WHERE uid = ?",
                (uid,),
            ).fetchone()
            if request.method in ("GET", "HEAD"):
                return _payload(landing_row)
            data = request.get_json(silent=True)
            settings = _valid_settings(data)
            current = _payload(landing_row)
            if data["revision"] != current["revision"]:
                abort(409, "This landing page changed. Reload it before saving.")
            if settings == current["settings"]:
                return {
                    "revision": current["revision"],
                    "settings": current["settings"],
                }
            revision = uuid4().hex
            con.execute(UPSERT, (uid, revision, json.dumps(settings, sort_keys=True)))
            return {"revision": revision, "settings": settings}
