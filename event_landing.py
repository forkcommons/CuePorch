"""Live event landing page API module."""

import json
from uuid import uuid4

from flask import abort, request

import event_landing_locale
import event_landing_speakers
from library_appearance import PALETTE

DEFAULTS = {
    "enabled": False,
    "accent": "#2b7e7d",
    "theme": "light",
    "layout": "stacked",
    "show_logo": True,
    "show_poster": False,
    "speaker_ids": [],
    "localization": event_landing_locale.DEFAULTS,
}

SCHEMA = """CREATE TABLE IF NOT EXISTS live_event_landing (
    uid TEXT PRIMARY KEY,
    revision TEXT NOT NULL,
    settings TEXT NOT NULL
);"""

UPSERT = """INSERT INTO live_event_landing (uid, revision, settings)
VALUES (?, ?, ?)
ON CONFLICT(uid) DO UPDATE SET revision=excluded.revision, settings=excluded.settings"""


def _payload(con, row):
    if row is None:
        return {
            "revision": None,
            "settings": dict(DEFAULTS),
            "profiles": event_landing_speakers.profiles(con),
        }
    merged = dict(DEFAULTS)
    merged.update(json.loads(row[1] or "{}"))
    return {
        "revision": row[0],
        "settings": merged,
        "profiles": event_landing_speakers.profiles(con),
    }


def _valid_settings(con, data, current_settings):
    if not isinstance(data, dict) or set(data) != {"revision", "settings"}:
        abort(422)
    settings = data["settings"]
    if not isinstance(settings, dict):
        abort(422)
    appearance = set(DEFAULTS) - {"speaker_ids", "localization"}
    if set(settings) not in (
        appearance,
        appearance | {"speaker_ids"},
        appearance | {"localization"},
        appearance | {"speaker_ids", "localization"},
    ):
        abort(422)
    settings = dict(settings)
    if "speaker_ids" not in settings:
        settings["speaker_ids"] = current_settings["speaker_ids"]
    if "localization" not in settings:
        settings["localization"] = current_settings["localization"]
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
    settings["speaker_ids"] = event_landing_speakers.validate_ids(con, settings["speaker_ids"])
    settings["localization"] = event_landing_locale.validate(
        settings["localization"], current_settings["localization"], settings["speaker_ids"]
    )
    return settings


def viewer(con, uid, embed):
    if embed:
        return None
    row = con.execute(
        "SELECT revision, settings FROM live_event_landing WHERE uid = ?", (uid,)
    ).fetchone()
    if row is None:
        return None
    settings = dict(DEFAULTS)
    settings.update(json.loads(row[1] or "{}"))
    settings["_revision"] = row[0]
    if not settings["enabled"]:
        return None
    settings["speakers"] = event_landing_speakers.selected(con, settings["speaker_ids"])
    return settings


def register(app, tx, staff, read, preview_url):
    @app.route("/api/live-events/<uid>/landing", methods=("GET", "PUT"))
    def landing(uid):
        staff()
        with tx() as con:
            event_row, event = read(con, uid)
            landing_row = con.execute(
                "SELECT revision, settings FROM live_event_landing WHERE uid = ?",
                (uid,),
            ).fetchone()
            if request.method in ("GET", "HEAD"):
                payload = _payload(con, landing_row)
                payload["preview"] = (
                    None
                    if landing_row is None or not payload["settings"]["enabled"]
                    else preview_url(con, event_row, event, landing_row[0])
                )
                return payload
            data = request.get_json(silent=True)
            current = _payload(con, landing_row)
            settings = _valid_settings(con, data, current["settings"])
            if data["revision"] != current["revision"]:
                abort(409, "This landing page changed. Reload it before saving.")
            if settings == current["settings"]:
                current["preview"] = (
                    None
                    if landing_row is None or not current["settings"]["enabled"]
                    else preview_url(con, event_row, event, landing_row[0])
                )
                return current
            revision = uuid4().hex
            con.execute(UPSERT, (uid, revision, json.dumps(settings, sort_keys=True)))
            return current | {
                "revision": revision,
                "settings": settings,
                "preview": (
                    None
                    if not settings["enabled"]
                    else preview_url(con, event_row, event, revision)
                ),
            }
