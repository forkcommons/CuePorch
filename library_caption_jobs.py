"""Generate captions with the installed local TextFlowKit and the institution's caption gate."""

import json
import sqlite3
from threading import Event, Thread
import time
import uuid

from filelock import FileLock, Timeout
from flask import abort, request

import caption_adapter
from catalog import video_version_key
import library_languages
from library_trash import hidden
import subtitles
import team_ai_policy

SCHEMA = """
CREATE TABLE IF NOT EXISTS caption_job(
 id TEXT PRIMARY KEY, work TEXT NOT NULL, version TEXT NOT NULL,
 language TEXT NOT NULL, replaces TEXT, state TEXT NOT NULL,
 created REAL NOT NULL, error TEXT);
"""


class Superseded(ValueError):
    pass


def initialize(con):
    columns = {row[1] for row in con.execute("PRAGMA table_info(caption_job)")}
    for column in ("source_language", "source_digest", "requested_by", "requested_member"):
        if column not in columns:
            con.execute(f"ALTER TABLE caption_job ADD COLUMN {column} TEXT")
    if "cue_count" not in columns:
        con.execute("ALTER TABLE caption_job ADD COLUMN cue_count INTEGER NOT NULL DEFAULT 0")


def track_digest(work, language):
    return next((track["sha256"] for track in library_languages.tracks_for(work)
                 if library_languages.track_language(track) == language), None)


def translation_source(work, language):
    return next((track["sha256"] for track in library_languages.viewer_tracks(work)
                 if library_languages.track_language(track) == language), None)


def queue(con, action, work, actor, require_missing=False, source_language=None, target_language=None):
    requested_by, requested_member = actor
    feature = "translation" if source_language else "caption"
    restriction = team_ai_policy.restriction(con, feature, requested_member)
    if restriction:
        if require_missing:
            return None
        abort(403, restriction)
    language = target_language or library_languages.summary(work)["original_language"]
    source_digest = None
    if source_language:
        source_digest = translation_source(work, source_language)
        if not source_digest or source_language == language:
            abort(409, "Choose an existing source track and a different target language.")
        if not caption_adapter.translation_runtime():
            abort(409, "Configure TextFlowKit and a local translation model first.")
    if not language:
        if require_missing:
            return None
        abort(409, "Set the original language first.")
    if not source_language and not caption_adapter.command()[0]:
        if require_missing:
            return None
        abort(409, "Set up the local TextFlowKit runtime to generate subtitles.")
    if not source_language and not any(item["role"] in ("original", "playback") for item in work["files"]):
        if require_missing:
            return None
        abort(409, "A retained media file is required to generate subtitles.")
    before = track_digest(work, language)
    if require_missing and before is not None:
        return None
    version = video_version_key(work)
    previous = con.execute(
        """SELECT id,source_language,source_digest FROM caption_job
           WHERE work=? AND version=? AND language=? AND state IN ('queued','running')
           ORDER BY created DESC LIMIT 1""", (work["id"], version, language),
    ).fetchone()
    if previous:
        if (previous["source_language"], previous["source_digest"]) != (source_language, source_digest):
            if require_missing:
                return None
            abort(409, "Another job already targets this language on this cut.")
        return previous["id"]
    uid = "caption-" + uuid.uuid4().hex
    payload = dict(work=work["id"], version=version, language=language)
    if source_language:
        payload["source_language"] = source_language
    action(con, uid, feature, payload)
    con.execute("""INSERT INTO caption_job(id,work,version,language,replaces,state,created,source_language,
                                          source_digest,requested_by,requested_member)
                   VALUES(?,?,?,?,?,'queued',?,?,?,?,?)""",
                (uid, work["id"], version, language, before, time.time(), source_language, source_digest,
                 requested_by, requested_member))
    return uid


def register(app, tx, get_work, action, request_actor):
    @app.route("/api/library/work/<uid>/caption-jobs", methods=["GET", "POST"])
    def caption_jobs(uid):
        with tx() as con:
            actor = request_actor(con)
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            if request.method == "POST":
                body = request.get_json()
                if not isinstance(body, dict) or body.get("version") != video_version_key(work):
                    abort(409, "The current cut changed. Open Languages again.")
                source = target = None
                if "source_language" in body or "target_language" in body:
                    source = library_languages.language_tag(body.get("source_language"))
                    target = library_languages.language_tag(body.get("target_language"))
                queue(con, action, work, actor, source_language=source, target_language=target)
            jobs = [
                dict(row) for row in con.execute(
                    """SELECT j.id,j.language,j.source_language,j.created,j.error,
                       CASE WHEN j.state='queued' AND a.status='waiting' THEN 'waiting' ELSE j.state END AS state
                       FROM caption_job j JOIN action a ON a.id=j.id
                       WHERE j.work=? AND j.version=? ORDER BY j.created DESC LIMIT 20""",
                    (uid, video_version_key(work)),
                )
            ]
            restrictions = {feature: team_ai_policy.restriction(con, feature, actor[1])
                            for feature in team_ai_policy.FEATURES}
        return dict(available=bool(caption_adapter.command()[0]) and not restrictions["caption"],
                    translation_available=bool(caption_adapter.translation_runtime()) and not restrictions["translation"],
                    restrictions=restrictions, jobs=jobs)


def start(app, root, tx, remember_work):
    stopping = Event()

    def process():
        with tx() as con:
            job = con.execute(
                """SELECT j.*,a.status AS gate,a.step FROM caption_job j JOIN action a ON j.id=a.id
                   WHERE j.state IN ('queued','running') AND a.status IN ('eligible','rejected')
                   ORDER BY j.created LIMIT 1"""
            ).fetchone()
            if job is None:
                return
            if job["gate"] == "rejected":
                con.execute("UPDATE caption_job SET state='rejected' WHERE id=?", (job["id"],))
                return
            restricted = team_ai_policy.restriction(con, job["step"], job["requested_member"])
            if restricted:
                con.execute("UPDATE caption_job SET state='blocked',error=? WHERE id=?", (restricted, job["id"]))
                return
            work = next(
                (item for item in json.loads((root / "manifest.json").read_text(encoding="utf8"))["works"]
                 if item["id"] == job["work"]), None,
            )
            if work is None or hidden(con, "work", work["id"]) or video_version_key(work) != job["version"]:
                con.execute("UPDATE caption_job SET state='superseded' WHERE id=?", (job["id"],))
                return
            con.execute("UPDATE caption_job SET state='running',error=NULL WHERE id=?", (job["id"],))

        def verify_source(current):
            if job["source_language"] and translation_source(current, job["source_language"]) != job["source_digest"]:
                raise Superseded("The source subtitle track changed. Start a new translation.")

        def apply(current, edits):
            verify_source(current)
            if track_digest(current, job["language"]) != job["replaces"]:
                raise Superseded("A subtitle edit was saved before the generated track was ready.")
            library_languages.store_track(
                root, current, edits, job["language"], cues, provenance, kind=kind
            )

        try:
            if job["source_language"]:
                verify_source(work)
                source = (root / "objects" / job["source_digest"]).read_bytes()
                cues = caption_adapter.translate_cues(subtitles.parse(source, "vtt"), job["language"], stopping)
                provenance, kind = "TextFlowKit machine-translated", "subtitles"
            else:
                media = next(item for item in work["files"] if item["role"] in ("original", "playback"))
                vtt, _ = caption_adapter.transcribe_caption(root, media, language=job["language"], stopping=stopping)
                cues = subtitles.parse(vtt, "vtt")
                provenance, kind = "TextFlowKit machine-generated", "captions"
            if not cues:
                raise caption_adapter.CaptionError("TextFlowKit did not find speech in this cut.")
            if stopping.is_set():
                return
            with tx() as con:
                _, settings = team_ai_policy.load(con)
                restricted = team_ai_policy.restriction(con, job["step"], job["requested_member"], settings)
                if restricted:
                    con.execute("UPDATE caption_job SET state='blocked',error=? WHERE id=?", (restricted, job["id"]))
                    return
                if hidden(con, "work", job["work"]):
                    raise Superseded("The work was removed before captions were ready.")
                current = next(
                    (item for item in json.loads((root / "manifest.json").read_text(encoding="utf8"))["works"]
                     if item["id"] == job["work"]), None,
                )
                if current is None or video_version_key(current) != job["version"]:
                    raise Superseded("The cut changed before captions were ready.")
                cues = team_ai_policy.apply_dictionary(cues, job["language"], settings["dictionary"])
                remember_work(library_languages.update(root, job["work"], job["version"], apply))
                con.execute("UPDATE caption_job SET state='complete',error=NULL,cue_count=? WHERE id=?",
                            (len(cues), job["id"]))
            return
        except Superseded as exc:
            state, error = "superseded", str(exc)
        except (OSError, ValueError) as exc:
            state = "failed"
            error = str(exc) if isinstance(exc, ValueError) else "Could not read the local caption runtime or media."
        except Exception:
            app.logger.exception("Local caption job failed")
            state, error = "failed", "Could not generate subtitles with the local caption runtime."
        if not stopping.is_set():
            with tx() as con:
                con.execute("UPDATE caption_job SET state=?,error=? WHERE id=?", (state, error, job["id"]))

    def run():
        lock = FileLock(str(root / ".caption-run.lock"), timeout=0)
        while not stopping.is_set():
            try:
                with lock:
                    process()
            except (Timeout, sqlite3.OperationalError):
                pass
            except Exception:
                app.logger.exception("Caption worker could not process its queue")
            stopping.wait(1)

    worker = Thread(target=run, name="CuePorch caption worker", daemon=True)
    worker.start()

    def stop():
        stopping.set()
        worker.join(timeout=6)

    return stop
