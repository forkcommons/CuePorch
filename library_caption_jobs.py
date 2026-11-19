"""Generate captions with the installed local TextFlowKit and the institution's caption gate."""

import json
import sqlite3
from threading import Thread
import time
import uuid

from filelock import FileLock, Timeout
from flask import abort, request

import caption_adapter
from catalog import video_version_key
import library_languages
from library_trash import hidden
import subtitles

SCHEMA = """
CREATE TABLE IF NOT EXISTS caption_job(
 id TEXT PRIMARY KEY, work TEXT NOT NULL, version TEXT NOT NULL,
 language TEXT NOT NULL, replaces TEXT, state TEXT NOT NULL,
 created REAL NOT NULL, error TEXT);
"""


class Superseded(ValueError):
    pass


def track_digest(work, language):
    return next((track["sha256"] for track in library_languages.tracks_for(work)
                 if library_languages.track_language(track) == language), None)


def queue(con, action, work, require_missing=False):
    language = library_languages.summary(work)["original_language"]
    if not language:
        if require_missing:
            return None
        abort(409, "Set the original language first.")
    if not caption_adapter.command()[0]:
        if require_missing:
            return None
        abort(409, "Set up the local TextFlowKit runtime to generate subtitles.")
    if not any(item["role"] in ("original", "playback") for item in work["files"]):
        if require_missing:
            return None
        abort(409, "A retained media file is required to generate subtitles.")
    before = track_digest(work, language)
    if require_missing and before is not None:
        return None
    version = video_version_key(work)
    previous = con.execute(
        """SELECT id FROM caption_job
           WHERE work=? AND version=? AND language=? AND state IN ('queued','running')
           ORDER BY created DESC LIMIT 1""", (work["id"], version, language),
    ).fetchone()
    if previous:
        return previous["id"]
    uid = "caption-" + uuid.uuid4().hex
    action(con, uid, "caption", {"work": work["id"], "version": version, "language": language})
    con.execute("INSERT INTO caption_job VALUES(?,?,?,?,?,'queued',?,NULL)",
                (uid, work["id"], version, language, before, time.time()))
    return uid


def register(app, tx, staff, get_work, action):
    @app.route("/api/library/work/<uid>/caption-jobs", methods=["GET", "POST"])
    def caption_jobs(uid):
        staff()
        with tx() as con:
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            if request.method == "POST":
                body = request.get_json()
                if not isinstance(body, dict) or body.get("version") != video_version_key(work):
                    abort(409, "The current cut changed. Open Languages again.")
                queue(con, action, work)
            jobs = [
                dict(row) for row in con.execute(
                    """SELECT j.id,j.language,j.created,j.error,
                       CASE WHEN j.state='queued' AND a.status='waiting' THEN 'waiting' ELSE j.state END AS state
                       FROM caption_job j JOIN action a ON a.id=j.id
                       WHERE j.work=? AND j.version=? ORDER BY j.created DESC LIMIT 20""",
                    (uid, video_version_key(work)),
                )
            ]
        return dict(available=bool(caption_adapter.command()[0]), jobs=jobs)


def start(app, root, tx, remember_work):
    def process():
        with tx() as con:
            job = con.execute(
                """SELECT j.*,a.status AS gate FROM caption_job j JOIN action a ON j.id=a.id
                   WHERE j.state IN ('queued','running') AND a.status IN ('eligible','rejected')
                   ORDER BY j.created LIMIT 1"""
            ).fetchone()
            if job is None:
                return
            if job["gate"] == "rejected":
                con.execute("UPDATE caption_job SET state='rejected' WHERE id=?", (job["id"],))
                return
            work = next(
                (item for item in json.loads((root / "manifest.json").read_text(encoding="utf8"))["works"]
                 if item["id"] == job["work"]), None,
            )
            if work is None or hidden(con, "work", work["id"]) or video_version_key(work) != job["version"]:
                con.execute("UPDATE caption_job SET state='superseded' WHERE id=?", (job["id"],))
                return
            con.execute("UPDATE caption_job SET state='running',error=NULL WHERE id=?", (job["id"],))

        def apply(current, edits):
            if track_digest(current, job["language"]) != job["replaces"]:
                raise Superseded("A subtitle edit was saved before the generated track was ready.")
            library_languages.store_track(
                root, current, edits, job["language"], cues, "TextFlowKit machine-generated"
            )

        state, error = "complete", None
        try:
            media = next(item for item in work["files"] if item["role"] in ("original", "playback"))
            vtt, _ = caption_adapter.transcribe_caption(root, media, language=job["language"])
            cues = subtitles.parse(vtt, "vtt")
            if not cues:
                raise caption_adapter.CaptionError("TextFlowKit did not find speech in this cut.")
            with tx() as con:
                if hidden(con, "work", job["work"]):
                    raise Superseded("The work was removed before captions were ready.")
                current = next(
                    (item for item in json.loads((root / "manifest.json").read_text(encoding="utf8"))["works"]
                     if item["id"] == job["work"]), None,
                )
                if current is None or video_version_key(current) != job["version"]:
                    raise Superseded("The cut changed before captions were ready.")
                remember_work(library_languages.update(root, job["work"], job["version"], apply))
        except Superseded as exc:
            state, error = "superseded", str(exc)
        except (OSError, ValueError) as exc:
            state = "failed"
            error = str(exc) if isinstance(exc, ValueError) else "Could not read the local caption runtime or media."
        except Exception:
            app.logger.exception("Local caption job failed")
            state, error = "failed", "Could not generate subtitles with the local caption runtime."
        with tx() as con:
            con.execute("UPDATE caption_job SET state=?,error=? WHERE id=?", (state, error, job["id"]))

    def run():
        lock = FileLock(str(root / ".caption-run.lock"), timeout=0)
        while True:
            try:
                with lock:
                    process()
            except (Timeout, sqlite3.OperationalError):
                pass
            except Exception:
                app.logger.exception("Caption worker could not process its queue")
            time.sleep(1)

    Thread(target=run, name="CuePorch caption worker", daemon=True).start()
