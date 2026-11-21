"""Render saved projects outside the request and keep restart-safe receipts."""

from collections import defaultdict
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from threading import Event, Thread
import json
import sqlite3
import uuid

from filelock import FileLock, Timeout
from flask import abort, request
from werkzeug.exceptions import HTTPException

from catalog import VideoReplacement, update_video_version, video_version_key
from library_editing import clip_chapters
from library_interactivity import readable_chapters
from library_languages import save_blob, tracks_for, track_language
from library_timeline import find_cut, SIZES, video_source
from media_tools import prepare_screening
import subtitles
import timeline_cards
from timeline_render import Clip, Music, render


SCHEMA = """
CREATE TABLE IF NOT EXISTS video_editor_job(
 id TEXT PRIMARY KEY, work TEXT NOT NULL, actor TEXT NOT NULL, revision TEXT NOT NULL,
 base TEXT NOT NULL, contents TEXT NOT NULL,
 state TEXT NOT NULL, result TEXT, error TEXT, UNIQUE(work,actor,revision));
"""


def public_job(row):
    return {key: row[key] for key in ("id", "state", "result", "error")}


def combined_attachments(root, sources):
    all_cues = defaultdict(list)
    chapters = {}
    offset = 0
    for item, work, cut in sources:
        start, end = item["start"], item["end"]
        if work is not None:
            for track in tracks_for(work, cut, vtt_only=False):
                if track["format"] not in ("vtt", "srt"):
                    raise ValueError("Convert the subtitle files to WebVTT before combining these videos.")
                language = track_language(track)
                data = (root / "objects" / track["sha256"]).read_bytes()
                for cue in subtitles.parse(data, track["format"]):
                    left, right = max(start, cue["start"]), min(end, cue["end"])
                    if left < right:
                        all_cues[language].append(dict(cue, start=offset + left - start, end=offset + right - start))
            context = dict(work, files=cut["files"], chapters=cut.get("chapters", []))
            for chapter in clip_chapters(readable_chapters(context), start, end):
                second = chapter["timecode"] + (offset + 500) // 1000
                chapters[second] = dict(chapter, timecode=second)
        offset += end - start
    captions = tuple(
        dict(role="caption", format="vtt", language=language, provenance="Timeline edit",
             **save_blob(root, subtitles.serialize(cues)))
        for language, cues in all_cues.items()
    )
    return captions, [entry for second, entry in chapters.items() if second * 1000 < offset]


def register(app, tx, staff, get_work, root, remember_work):
    wake = Event()
    stopping = Event()

    @app.post("/api/library/work/<uid>/timeline/render")
    def queue_video_editor_job(uid):
        actor = staff()
        request.max_content_length = 4096
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get("revision"), str):
            abort(422, "Save the timeline before rendering.")
        with tx() as con:
            work = get_work(con, uid)
            prior = con.execute("SELECT * FROM video_editor_project WHERE work=? AND actor=?", (uid, actor)).fetchone()
            if prior is None or body["revision"] != prior["revision"]:
                abort(409, "Reopen the saved project before rendering.")
            existing = con.execute("""SELECT * FROM video_editor_job WHERE work=? AND actor=? AND revision=?""",
                                   (uid, actor, prior["revision"])).fetchone()
            if existing:
                return public_job(existing), 200 if existing["state"] == "saved" else 202
            if prior["base"] != video_version_key(work):
                abort(409, "The current cut has changed. Save a new draft first.")
            settings = json.loads(prior["contents"])
            if not settings["clips"]:
                abort(422, "Add at least one clip before rendering.")
            job = uuid.uuid4().hex
            con.execute("INSERT INTO video_editor_job VALUES(?,?,?,?,?,?,'queued',NULL,NULL)",
                        (job, uid, actor, prior["revision"], prior["base"], prior["contents"]))
        wake.set()
        return dict(id=job, state="queued", result=None, error=None), 202

    @app.get("/api/library/work/<uid>/timeline/render/<job>")
    def editor_job_status(uid, job):
        actor = staff()
        with tx() as con:
            get_work(con, uid)
            row = con.execute("SELECT * FROM video_editor_job WHERE id=? AND work=? AND actor=?",
                              (job, uid, actor)).fetchone()
        if row is None:
            abort(404)
        return public_job(row)

    def load_job(con, job):
        uid = job["work"]
        work = get_work(con, uid)
        for cut in (work, *work.get("versions_history", [])):
            if cut.get("editor_job") == job["id"]:
                return video_version_key(cut)
        if video_version_key(work) != job["base"]:
            raise ValueError("The current cut changed before rendering. Open the editor and save a new draft.")
        render_input = json.loads(job["contents"])
        brand = render_input.get("brand") or {}
        ordered = (brand.get("intro"), *render_input["clips"], brand.get("outro"))
        sources = []
        for item in (item for item in ordered if item):
            if "card" in item:
                sources.append((item, None, None))
                continue
            source_work = get_work(con, item["work"])
            sources.append((item, source_work, find_cut(source_work, item["cut"])))
        music = render_input["music"]
        if music:
            source = find_cut(get_work(con, music["work"]), music["cut"])
            asset = next(file for file in source["files"] if file["role"] == "original")
            music = Music(root / "objects" / asset["sha256"], music["start"], music["gain"])
        return render_input, sources, music

    def process(job):
        with tx() as con:
            loaded = load_job(con, job)
        if isinstance(loaded, str):
            return loaded
        settings, sources, music = loaded
        with TemporaryDirectory(dir=root / "objects", prefix="timeline-") as name:
            scratch = Path(name)
            clips = []
            for item, _, cut in sources:
                if cut is None:
                    path = scratch / (uuid.uuid4().hex + ".png")
                    timeline_cards.draw_card(path, item["card"], SIZES[settings["ratio"]])
                    clips.append(Clip(str(path), 0, item["end"], False, still=True))
                else:
                    file, info = video_source(root, cut)
                    clips.append(Clip(file, item["start"], item["end"], bool(info["audio"])))
            original, screening = scratch / "timeline.mp4", scratch / "screening.mp4"
            duration = render(original, clips, SIZES[settings["ratio"]], settings["color"], music, stopping)
            prepare_screening(original, screening, stopping=stopping)
            captions, chapters = combined_attachments(root, sources)
            replacement = VideoReplacement(original, screening, duration, captions, chapters, editor_job=job["id"])
            with tx() as con:
                get_work(con, job["work"])
                updated = update_video_version(root, job["work"], expected=job["base"], replacement=replacement)
                remember_work(updated)
                return video_version_key(updated)

    def render_next():
        with tx() as con:
            job = con.execute("""SELECT * FROM video_editor_job WHERE state IN ('queued','rendering')
                                 ORDER BY rowid LIMIT 1""").fetchone()
            if not job:
                return False
            con.execute("UPDATE video_editor_job SET state='rendering' WHERE id=?", (job["id"],))
        error, cut = None, None
        try:
            cut = process(job)
        except InterruptedError:
            return False
        except (HTTPException, ValueError) as issue:
            error = issue.description if isinstance(issue, HTTPException) else str(issue)
        except (OSError, TimeoutExpired):
            error = "A media file could not be read or the render timed out."
        with tx() as con:
            con.execute("UPDATE video_editor_job SET state=?,result=?,error=? WHERE id=?",
                        ("failed" if error else "saved", cut, error, job["id"]))
        return True

    def render_jobs():
        lock = FileLock(str(root / ".timeline-worker.lock"), timeout=0)
        while not stopping.is_set():
            try:
                with lock:
                    if render_next():
                        continue
            except Timeout:
                pass
            except sqlite3.OperationalError:
                app.logger.exception("The editor could not read its queue")
            wake.wait(2)
            wake.clear()

    def start():
        worker = Thread(target=render_jobs, name="CuePorch editor", daemon=True)
        worker.start()

        def stop():
            stopping.set()
            wake.set()
            worker.join(timeout=6)

        return stop

    return start
