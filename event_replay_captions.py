"""Queue local captions against the finished replay timeline."""

import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Thread
import uuid

from filelock import FileLock, Timeout
from flask import Response, abort, request
import requests

import caption_adapter
import event_captions
import event_peer
import event_replay_spool
from event_media import root_path
from media_tools import run_media
import subtitles
import team_ai_policy


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_replay_captions(
 event TEXT NOT NULL, opens INTEGER NOT NULL, language TEXT NOT NULL, state TEXT NOT NULL,
 revision TEXT NOT NULL, approval TEXT NOT NULL, cues TEXT,
 PRIMARY KEY(event,opens));
"""


def read(con, uid, opens):
    return con.execute("SELECT * FROM live_replay_captions WHERE event=? AND opens=?", (uid, opens)).fetchone()


def store_approval(con, action, uid, opens, revision):
    key = f"replay-captions-{uid}-{opens}-{revision}"
    action(con, key, "caption", dict(event=uid, occurrence=opens, artifact="replay captions"))
    return key


def queue(con, action, uid, opens):
    setting = event_captions.settings(con, uid, opens)
    if setting and setting["enabled"] and read(con, uid, opens) is None:
        revision = uuid.uuid4().hex
        approval = store_approval(con, action, uid, opens, revision)
        con.execute("INSERT INTO live_replay_captions VALUES(?,?,?,'queued',?,?,NULL)",
                    (uid, opens, setting["language"], revision, approval))


def archive_source(media, origin, remote):
    raw = media.metadata(origin, remote)
    state = event_peer.status(raw)
    if state["state"] != "replay":
        raise ValueError("The recording is not ready.")
    playlist = next(item for item in raw["streamingPlaylists"] if item["type"] == 1)
    files = [item for item in playlist.get("files", [])
             if item.get("hasAudio") and isinstance(item.get("size"), int) and item["size"] > 0]
    file = min(files, key=lambda item: item["size"])
    directory, name = root_path(origin, file["fileUrl"], remote, not state["public"])
    if not name.endswith(".mp4"):
        raise ValueError("A suitable replay rendition is unavailable.")
    return f"{directory}/{name}", file["size"]


class ReplayCaptionJobs:
    def __init__(self, app, tx, media, database, native_source=None):
        self.app = app
        self.tx = tx
        self.media = media
        self.native_source = native_source
        self.directory = database.parent / "replay-captions"
        self.replay_root = database.parent / "interpreted-replays"
        self.stopping = Event()
        self.thread = Thread(target=self.scan, name="CuePorch replay caption scheduler", daemon=True)
        self.thread.start()

    def scan(self):
        while not self.stopping.wait(3):
            try:
                self.directory.mkdir(exist_ok=True)
                with FileLock(self.directory / "worker.lock", timeout=0):
                    self.process()
            except Timeout:
                pass
            except Exception:
                self.app.logger.exception("The replay caption worker could not advance its queue")

    def process(self):
        with self.tx() as con:
            job = con.execute("""SELECT j.*,o.origin,o.remote FROM live_replay_captions j
                JOIN live_occurrence o ON j.event=o.event AND j.opens=o.opens
                WHERE j.state IN ('queued','running') AND o.delivery IN ('ready','replay') ORDER BY j.opens LIMIT 1""").fetchone()
            if not job:
                return
            if team_ai_policy.restriction(con, event_captions.FEATURE, None):
                con.execute("UPDATE live_replay_captions SET state='blocked' WHERE revision=?", (job["revision"],))
                return
            verified_path = (self.native_source(con, job["event"], job["opens"], job["remote"])
                             if not job["origin"] and self.native_source else None)
            con.execute("UPDATE live_replay_captions SET state='running' WHERE revision=?", (job["revision"],))
        state, cues = "ready", None
        try:
            with TemporaryDirectory(dir=self.directory) as temp:
                folder = Path(temp)
                if job["origin"]:
                    source = folder / "replay.mp4"
                    self.download(job, source)
                else:
                    if verified_path is None:
                        raise ValueError("The imported recording is unavailable.")
                    source = verified_path
                vtt, _ = caption_adapter.transcribe_path(source, folder / "tracks", job["language"], self.stopping)
                cues = subtitles.parse(vtt, "vtt")
                if not cues:
                    state = "no_speech"
        except (OSError, ValueError, StopIteration, KeyError, requests.RequestException, subprocess.TimeoutExpired) as error:
            state = "failed"
            if not self.stopping.is_set():
                self.app.logger.warning("Replay caption generation failed for %s: %s", job["event"],
                                        str(error) if isinstance(error, ValueError) else type(error).__name__)
        if not self.stopping.is_set():
            with self.tx() as con:
                policy = team_ai_policy.load(con)[1]
                if team_ai_policy.restriction(con, event_captions.FEATURE, None, policy):
                    state, cues = "blocked", None
                elif cues:
                    cues = team_ai_policy.apply_dictionary(cues, job["language"], policy["dictionary"])
                con.execute("UPDATE live_replay_captions SET state=?,cues=? WHERE revision=? AND state='running'",
                            (state, json.dumps(cues) if cues else None, job["revision"]))

    def download(self, job, destination):
        archive = event_replay_spool.available(self.replay_root, job["event"], job["opens"], job["remote"])
        if archive is not None:
            budget = event_replay_spool.FREE_RESERVE + round(archive.duration * 32_000)
            if shutil.disk_usage(self.directory).free < budget:
                raise ValueError("The local replay caption workspace has insufficient free space.")
            result = run_media([
                "ffmpeg", "-nostdin", "-v", "error", *event_replay_spool.HLS_INPUT,
                str(archive.directory / "2.m3u8"), "-map", "0:a:0", "-c:a", "copy",
                "-map_metadata", "-1", str(destination),
            ], stopping=self.stopping)
            if result.returncode or not destination.is_file() or not destination.stat().st_size:
                raise ValueError("The replay program audio could not be prepared for captioning.")
            return
        path, size = archive_source(self.media, job["origin"], job["remote"])
        if shutil.disk_usage(self.directory).free < size + 512 * 1024**2:
            raise ValueError("The local replay caption workspace has insufficient free space.")
        copied = 0
        with self.media.read(job["origin"], path, stream=True) as response, destination.open("wb") as target:
            for chunk in response.iter_content(256 * 1024):
                if self.stopping.is_set():
                    raise ValueError("Replay caption service is stopping.")
                copied += len(chunk)
                if copied > size:
                    raise ValueError("The replay rendition size changed.")
                target.write(chunk)
        if copied != size:
            raise ValueError("The replay rendition download ended early.")

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=12)


def approved(con, record):
    if record and record["cues"]:
        gate = con.execute("SELECT status FROM action WHERE id=?", (record["approval"],)).fetchone()
        return bool(gate and gate["status"] == "eligible")
    return False


def register(app, tx, staff, action, operator_access, audience_access):
    @app.route("/api/live-events/<uid>/<int:opens>/replay-captions", methods=["GET", "POST"])
    def edit_replay_captions(uid, opens):
        staff()
        if request.method == "POST":
            request.max_content_length = 8 * 1024**2
            upload = request.files.get("track")
            extension = Path(upload.filename or "").suffix.lower() if upload else ""
            if extension not in (".vtt", ".srt"):
                abort(415, "Choose a WebVTT or SRT subtitle file.")
            try:
                cues = subtitles.parse(upload.read(), extension[1:])
                if not cues:
                    raise ValueError("The subtitle file must contain at least one cue.")
            except ValueError as error:
                abort(422, str(error))
            language = request.form.get("language", "en")
            if not subtitles.LANGUAGE.fullmatch(language):
                abort(422, "Enter a valid subtitle language.")
        with tx() as con:
            operator_access(con, uid, opens)
            record = read(con, uid, opens)
            archived = bool(con.execute("SELECT 1 FROM action WHERE id=?", (f"live-replay-{uid}-{opens}",)).fetchone())
            if request.method == "POST":
                if not archived:
                    abort(409, "The event recording is still being prepared.")
                if (record["revision"] if record else "") != request.form.get("revision"):
                    abort(409, "The subtitle track changed. Refresh its status before replacing it.")
                if record:
                    con.execute("UPDATE action SET status='rejected',decision='superseded' WHERE id=? AND status='waiting'",
                                (record["approval"],))
                revision = uuid.uuid4().hex
                approval = store_approval(con, action, uid, opens, revision)
                con.execute("INSERT OR REPLACE INTO live_replay_captions VALUES(?,?,?,'ready',?,?,?)",
                            (uid, opens, language, revision, approval, json.dumps(cues)))
                record = read(con, uid, opens)
            if request.args.get("download") == "1":
                if not record or not record["cues"]:
                    abort(404)
                return Response(subtitles.serialize(json.loads(record["cues"])), mimetype="text/vtt", headers={
                    "Content-Disposition": 'attachment; filename="cueporch-replay-captions.vtt"',
                    "Cache-Control": "no-store",
                })
            return dict(state=record["state"] if record else "unavailable", language=record["language"] if record else "en",
                        revision=record["revision"] if record else "", has_track=bool(record and record["cues"]),
                        published=approved(con, record), archived=archived)

    @app.get("/events/<slug>/<int:opens>/replay-captions")
    def audience_replay_captions(slug, opens):
        with tx() as con:
            scope, _ = audience_access(con, slug, opens)
            uid = scope[0]
            delivery = con.execute("SELECT delivery FROM live_occurrence WHERE event=? AND opens=?",
                                   (uid, opens)).fetchone()
            if not delivery or delivery["delivery"] != "replay":
                abort(404)
            record = read(con, uid, opens)
            if not approved(con, record):
                return dict(enabled=False, cues=[])
            since = request.args.get("since", -1, type=int) if request.args.get("revision") == record["revision"] else -1
            cues = [dict(starts=cue["start"], ends=cue["end"], text=cue["text"])
                    for cue in json.loads(record["cues"]) if cue["start"] > since][:1000]
            return Response(json.dumps(dict(enabled=True, cues=cues, revision=record["revision"],
                                            language=record["language"])), content_type="application/json",
                            headers={"Cache-Control": "no-store"})
