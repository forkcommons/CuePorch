"""Per-occurrence audience metrics from CuePorch-controlled players."""

from collections import defaultdict
import time

from flask import abort

from library_analytics import record_identity, track_observations
from subtitles import LANGUAGE


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
CREATE TABLE IF NOT EXISTS live_playback_reporting(
 session TEXT PRIMARY KEY, created REAL NOT NULL, surface TEXT NOT NULL,
 source TEXT NOT NULL, device TEXT NOT NULL, region TEXT, completed INTEGER NOT NULL, duration_ms INTEGER);
CREATE INDEX IF NOT EXISTS live_playback_reporting_date ON live_playback_reporting(created);
"""


def current(con, uid, opens, phase, now=None):
    return con.execute(
        "SELECT count(*) FROM live_playback WHERE event=? AND opens=? AND phase=? AND playing=1 AND seen>?",
        (uid, opens, phase, (time.time() if now is None else now) - WINDOW),
    ).fetchone()[0]


def event_track_rows(samples, watched):
    def label(key, kinds, unmeasured):
        if key in unmeasured:
            return key, key
        kind, _, language = key.partition(":")
        if kind not in kinds or len(language) > 53 or not LANGUAGE.fullmatch(language):
            abort(422, "Invalid event playback track.")
        return language.lower(), kind

    totals = defaultdict(int)
    for (audio, caption), milliseconds in track_observations(samples, watched, 2).items():
        audio_label = label(audio, ("original", "alternate"), ("unknown",))
        caption_label = label(caption, ("captions_ai",), ("none", "unknown"))
        if milliseconds:
            totals[(*audio_label, *caption_label)] += milliseconds
    return [(*pair, milliseconds) for pair, milliseconds in totals.items()]


def record(con, uid, opens, phase, nonce, body, *, report_scope=None, viewer_key=None):
    if not isinstance(body, dict):
        abort(400, "Enter a playback observation.")
    sequence, watched = body.get("sequence"), body.get("watched")
    played, playing = body.get("played"), body.get("playing")
    completed = body.get("completed", False)
    if (type(sequence) is not int or not 1 <= sequence < 10**9 or type(watched) is not int
            or not 0 <= watched <= 24 * 3600 * 1000 or type(played) is not bool or type(playing) is not bool
            or type(completed) is not bool or (not played and (playing or watched or completed))):
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
    if report_scope is not None:
        surface, source, device, region, duration_ms = report_scope
        if prior is None:
            con.execute("INSERT INTO live_playback_reporting VALUES(?,?,?,?,?,?,?,?)",
                        (nonce, now, surface, source, device, region, completed and phase == "replay", duration_ms))
        elif completed and phase == "replay":
            con.execute("UPDATE live_playback_reporting SET completed=1 WHERE session=?", (nonce,))
        record_identity(con, nonce, body.get("viewer"), viewer_key)
        if "tracks" in body:
            pairs = event_track_rows(body["tracks"], watched)
            if pairs and not played:
                abort(422, "A play is required for viewing progress.")
            captions = defaultdict(int)
            for _, _, language, kind, milliseconds in pairs:
                captions[language, kind] += milliseconds
            con.execute("DELETE FROM video_analytics_track_pair WHERE session=?", (nonce,))
            con.execute("DELETE FROM video_analytics_caption WHERE session=?", (nonce,))
            con.executemany("INSERT INTO video_analytics_track_pair VALUES(?,?,?,?,?,?)",
                            [(nonce, *pair) for pair in pairs])
            con.executemany("INSERT INTO video_analytics_caption VALUES(?,?,?,?)",
                            [(nonce, *label, ms) for label, ms in captions.items()])
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
        summary = con.execute(
            """
               SELECT COUNT(*) AS loads,
                      COALESCE(SUM(p.played), 0) AS plays,
                      COALESCE(SUM(p.watched), 0) AS watched,
                      COUNT(DISTINCT i.viewer) FILTER (WHERE p.played = 1) AS unique_viewers,
                      COUNT(i.viewer) FILTER (WHERE p.played = 1) AS identified_plays,
                      SUM(r.completed) FILTER (WHERE p.played = 1) AS finishes,
                      AVG(1.0 * p.watched / r.duration_ms) FILTER (WHERE p.played = 1 AND r.duration_ms > 0) AS average_watched_fraction
               FROM live_playback p
               LEFT JOIN live_playback_reporting r ON r.session = p.id
               LEFT JOIN video_analytics_viewer i ON i.session = p.id
               WHERE p.event = ? AND p.opens = ? AND p.phase = ?
            """,
            (uid, opens, phase),
        ).fetchone()
        peak = con.execute("SELECT peak FROM live_playback_peak WHERE event=? AND opens=? AND phase=?",
                           (uid, opens, phase)).fetchone()
        loads, views = summary["loads"], summary["plays"]
        seconds = summary["watched"] / 1000
        identified = summary["identified_plays"]
        result[phase] = dict(
            loads=loads, plays=views, watched_seconds=seconds,
            average_seconds_per_load=seconds / loads if loads else 0,
            peak=peak["peak"] if peak else 0,
            current=current(con, uid, opens, phase) if active_phase == phase else 0,
            unique_viewers=summary["unique_viewers"] if identified or not views else None,
            identified_plays=identified,
            finishes=(summary["finishes"] if views else 0) if phase == "replay" else None,
            average_watched_fraction=summary["average_watched_fraction"] if phase == "replay" else None,
        )
    return result
