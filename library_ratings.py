"""Timed anonymous video ratings and operator reports."""

import csv
from io import StringIO
import time

from flask import Response, abort, request

from catalog import video_version_key
from library_interactivity import text_field


SCHEMA = """
CREATE TABLE IF NOT EXISTS video_rating_widget(
 work TEXT PRIMARY KEY, mode TEXT NOT NULL, prompt TEXT NOT NULL, start INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS video_rating(
 work TEXT NOT NULL, version TEXT NOT NULL, session TEXT NOT NULL, mode TEXT NOT NULL,
 score INTEGER NOT NULL, created REAL NOT NULL, PRIMARY KEY(work,version,session));
"""
SCORES = {"thumbs": (0, 1), "stars": (1, 2, 3, 4, 5)}
DEFAULT = dict(mode="none", prompt="How was this video?", start=0)


def widget(con, uid):
    row = con.execute("SELECT mode,prompt,start FROM video_rating_widget WHERE work=?", (uid,)).fetchone()
    return dict(row) if row is not None else DEFAULT.copy()


def available(con, work):
    settings = widget(con, work["id"])
    duration = work.get("duration_seconds")
    return settings if settings["mode"] != "none" and (not duration or settings["start"] < duration) else None


def rate(con, work, session, expected_mode, score):
    shown = available(con, work)
    if not shown or shown["mode"] != expected_mode:
        abort(404)
    if type(score) is not int or score not in SCORES[expected_mode]:
        abort(422, "Choose an available rating.")
    result = con.execute("INSERT OR IGNORE INTO video_rating VALUES(?,?,?,?,?,?)", (
        work["id"], video_version_key(work), session, expected_mode, score, time.time(),
    ))
    return {"recorded": result.rowcount == 1}


def report(con, uid):
    rows = con.execute(
        """SELECT version,mode,score,count(*) AS responses FROM video_rating WHERE work=?
           GROUP BY version,mode,score ORDER BY version,mode,score""",
        (uid,),
    ).fetchall()
    return [dict(row) for row in rows]


def register(app, tx, staff, get_work):
    @app.route("/api/library/work/<uid>/rating", methods=["GET", "PUT"])
    def rating_settings(uid):
        staff()
        with tx() as con:
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            settings = widget(con, uid)
            version = video_version_key(work)
            if request.method == "PUT":
                body = request.get_json()
                if not isinstance(body, dict) or body.get("mode") not in ("none", *SCORES):
                    abort(422, "Choose Off, Thumbs or Stars.")
                if body.get("previous") != settings or body.get("version") != version:
                    abort(409, "This video or its rating settings changed. Reopen the editor.")
                mode, start = body["mode"], body.get("start")
                if mode == "none":
                    settings = DEFAULT.copy()
                else:
                    duration = work.get("duration_seconds")
                    if type(start) is not int or not 0 <= start < 1_000_000_000 or (duration and start >= duration):
                        abort(422, "Choose a rating start time within the video.")
                    prompt = text_field(body.get("prompt"), "a rating prompt", 42, True)
                    settings = dict(mode=mode, prompt=prompt, start=start)
                con.execute("""INSERT INTO video_rating_widget VALUES(?,?,?,?) ON CONFLICT(work)
                               DO UPDATE SET mode=excluded.mode,prompt=excluded.prompt,start=excluded.start""",
                            (uid, settings["mode"], settings["prompt"], settings["start"]))
            return dict(settings=settings, version=version, report=report(con, uid))

    @app.get("/api/library/work/<uid>/rating/export")
    def export_rating(uid):
        staff()
        with tx() as con:
            get_work(con, uid)
            counts = report(con, uid)
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["video_id", "version", "rating_type", "score", "responses"])
        writer.writerows((uid, row["version"], row["mode"], row["score"], row["responses"]) for row in counts)
        return Response(output.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": "attachment; filename=cueporch-video-rating.csv",
            "Cache-Control": "no-store",
        })
