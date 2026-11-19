"""Viewer playback settings and the released archives of a recurring event."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import abort, request


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_playlist(
 event TEXT PRIMARY KEY, enabled INTEGER NOT NULL, autoplay_next INTEGER NOT NULL, loop INTEGER NOT NULL);
"""
DEFAULTS = dict(enabled=False, autoplay_next=False, loop=False)


def settings(con, uid):
    row = con.execute("SELECT enabled,autoplay_next,loop FROM live_playlist WHERE event=?", (uid,)).fetchone()
    return {key: bool(row[key]) for key in DEFAULTS} if row else DEFAULTS.copy()


def archive_playlist(con, uid, options, schedule):
    zone = ZoneInfo(schedule["timezone"])
    entries = []
    for (opens,) in con.execute(
        "SELECT opens FROM live_occurrence WHERE event=? AND delivery='replay' ORDER BY opens", (uid,)
    ):
        local = datetime.fromtimestamp(opens, timezone.utc).astimezone(zone)
        entries.append(dict(id=opens, label=local.strftime("%Y-%m-%d %H:%M %Z")))
    return dict(entries=entries, autoplay_next=options["autoplay_next"], loop=options["loop"])


def latest_released(con, uid):
    return con.execute("SELECT * FROM live_occurrence WHERE event=? AND delivery='replay' ORDER BY opens DESC LIMIT 1",
                       (uid,)).fetchone()


def register(app, tx, staff, event_for):
    @app.route("/api/live-events/<uid>/playlist", methods=["GET", "PUT"])
    def playlist_settings(uid):
        staff()
        with tx() as con:
            _, event = event_for(con, uid)
            if request.method == "GET":
                return settings(con, uid)
            body = request.get_json()
            if not isinstance(body, dict) or any(type(body.get(key)) is not bool for key in DEFAULTS):
                abort(422, "Choose the archive playlist and playback options.")
            if body["enabled"] and event["schedule"]["repeat"] == "none":
                abort(422, "Enable a repeating schedule before showing a replay playlist.")
            con.execute("""INSERT INTO live_playlist VALUES(?,?,?,?) ON CONFLICT(event) DO UPDATE SET
                enabled=excluded.enabled,autoplay_next=excluded.autoplay_next,loop=excluded.loop""",
                (uid, body["enabled"], body["autoplay_next"], body["loop"]))
            return settings(con, uid)
