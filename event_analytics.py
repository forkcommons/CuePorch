"""Per-occurrence audience metrics from CuePorch-controlled players."""

import time

from flask import abort

from library_analytics import permitted


WINDOW = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS live_playback(
 id TEXT PRIMARY KEY, event TEXT NOT NULL, opens INTEGER NOT NULL, phase TEXT NOT NULL,
 sequence INTEGER NOT NULL, played INTEGER NOT NULL, playing INTEGER NOT NULL,
 watched INTEGER NOT NULL, seen REAL NOT NULL);
CREATE INDEX IF NOT EXISTS live_playback_active ON live_playback(event,opens,phase,playing,seen);
CREATE TABLE IF NOT EXISTS live_playback_peak(
 event TEXT NOT NULL, opens INTEGER NOT NULL, phase TEXT NOT NULL, peak INTEGER NOT NULL,
 PRIMARY KEY(event,opens,phase));
"""


def current(con, uid, opens, phase, now=None):
    return con.execute(
        "SELECT count(*) FROM live_playback WHERE event=? AND opens=? AND phase=? AND playing=1 AND seen>?",
        (uid, opens, phase, (time.time() if now is None else now) - WINDOW),
    ).fetchone()[0]


def record(con, uid, opens, phase, nonce, body):
    if not isinstance(body, dict):
        abort(400, "Enter a playback observation.")
    sequence, watched = body.get("sequence"), body.get("watched")
    played, playing = body.get("played"), body.get("playing")
    if (type(sequence) is not int or not 1 <= sequence < 10**9 or type(watched) is not int
            or not 0 <= watched <= 24 * 3600 * 1000 or type(played) is not bool or type(playing) is not bool
            or (not played and (playing or watched))):
        abort(422, "Invalid playback observation.")
    now = time.time()
    prior = con.execute("SELECT sequence,playing,seen FROM live_playback WHERE id=?", (nonce,)).fetchone()
    if prior and sequence <= prior["sequence"]:
        return
    con.execute(
        """INSERT INTO live_playback VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET sequence=excluded.sequence, played=max(played,excluded.played),
           playing=excluded.playing, watched=max(watched,excluded.watched), seen=excluded.seen""",
        (nonce, uid, opens, phase, sequence, played, playing, watched, now),
    )
    if playing and (not prior or not prior["playing"] or prior["seen"] <= now - WINDOW):
        count = current(con, uid, opens, phase, now)
        con.execute(
            """INSERT INTO live_playback_peak VALUES(?,?,?,?)
               ON CONFLICT(event,opens,phase) DO UPDATE SET peak=max(peak,excluded.peak)""",
            (uid, opens, phase, count),
        )


def summarize(con, uid, opens, active_phase):
    result = {}
    for phase in ("live", "replay"):
        loads, views, watch_ms = con.execute(
            """SELECT count(*),coalesce(sum(played),0),coalesce(sum(watched),0)
               FROM live_playback WHERE event=? AND opens=? AND phase=?""",
            (uid, opens, phase),
        ).fetchone()
        peak = con.execute("SELECT peak FROM live_playback_peak WHERE event=? AND opens=? AND phase=?",
                           (uid, opens, phase)).fetchone()
        seconds = watch_ms / 1000
        result[phase] = dict(loads=loads, plays=views, watched_seconds=seconds,
                             average_seconds_per_load=seconds / loads if loads else 0,
                             peak=peak["peak"] if peak else 0,
                             current=current(con, uid, opens, phase) if active_phase == phase else 0)
    return result
