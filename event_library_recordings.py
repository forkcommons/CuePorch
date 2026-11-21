"""Retain finished event playback as private, portable Library media."""

from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import time
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout
from flask import abort, request
import requests

from catalog import build_intake, merge_collection
from cueporch import work_id
from event_media import root_path
import event_peer
from media_tools import LOCAL_FFMPEG, run_media


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_library_recording(
 event TEXT NOT NULL, opens INTEGER NOT NULL, state TEXT NOT NULL,
 work TEXT, error TEXT, next_attempt REAL NOT NULL,
 PRIMARY KEY(event,opens));
"""
PROVIDER = "cueporch-live-recording"


def job(con, uid, opens):
    return con.execute("SELECT * FROM live_library_recording WHERE event=? AND opens=?", (uid, opens)).fetchone()


def media_files(origin, remote, raw):
    protected = not event_peer.status(raw)["public"]
    playlist = next((item for item in raw.get("streamingPlaylists", []) if item["type"] == 1), None)
    files = [item for item in playlist.get("files", []) if type(item.get("size")) is int and item["size"] > 0] if playlist else []
    video = max((item for item in files if item.get("hasVideo")),
                key=lambda item: (item["resolution"]["id"], bool(item.get("hasAudio")), item["size"]), default=None)
    if not video:
        raise ValueError("The replay has no downloadable video.")
    audio = None if video.get("hasAudio") else max(
        (item for item in files if item.get("hasAudio") and not item.get("hasVideo")),
        key=lambda item: item["size"], default=None,
    )
    selected = []
    for item in (video, audio):
        if not item:
            continue
        directory, name = root_path(origin, item["fileUrl"], remote, protected)
        if not name.endswith(".mp4"):
            raise ValueError("The host did not supply a usable replay file.")
        selected.append((directory + "/" + name, item["size"]))
    return selected


class Recordings:
    def __init__(self, app, tx, media, database, root, institution, remember_work):
        self.app = app
        self.tx = tx
        self.media = media
        self.root = root
        self.institution = institution
        self.remember_work = remember_work
        self.directory = database.parent / "library-event-recordings"
        self.stopping = Event()
        self.thread = Thread(target=self.scan, name="CuePorch library recording worker", daemon=True)
        self.thread.start()

    def scan(self):
        while not self.stopping.wait(3):
            try:
                self.directory.mkdir(exist_ok=True)
                with FileLock(self.directory / "worker.lock", timeout=0):
                    selected = self.next_job()
                    if selected:
                        self.process(selected)
            except Timeout:
                pass
            except Exception:
                self.app.logger.exception("The library recording worker could not advance its queue")

    def next_job(self):
        with self.tx() as con:
            now = time()
            con.execute("""INSERT OR IGNORE INTO live_library_recording
                          SELECT o.event,o.opens,'waiting',NULL,NULL,0 FROM live_occurrence o
                          JOIN live_event e ON e.id=o.event
                          WHERE o.opens<=? AND o.delivery IN ('ready','replay')
                          AND json_extract(e.settings,'$.save_replay')=1 AND json_extract(e.settings,'$.archive_replay')=1""", (now,))
            return con.execute("""SELECT j.*,o.origin,o.remote,e.settings FROM live_library_recording j
                     JOIN live_occurrence o ON o.event=j.event AND o.opens=j.opens
                     JOIN live_event e ON e.id=j.event
                     WHERE j.state IN ('waiting','preparing') AND j.next_attempt<=?
                     ORDER BY j.next_attempt,j.opens LIMIT 1""", (now,)).fetchone()

    def update(self, selected, state, *, work=None, error=None, retry=0):
        with self.tx() as con:
            con.execute("UPDATE live_library_recording SET state=?,work=?,error=?,next_attempt=? WHERE event=? AND opens=?",
                        (state, work, error, time() + retry, selected["event"], selected["opens"]))

    def process(self, selected):
        origin, remote = selected["origin"], selected["remote"]
        identity = f"{selected['event']}:{selected['opens']}"
        uid = work_id(self.institution, PROVIDER, identity)
        prior = next((work for work in json.loads((self.root / "manifest.json").read_text(encoding="utf8"))["works"]
                      if work["id"] == uid), None)
        if prior:
            self.remember_work(prior)
            self.update(selected, "ready", work=uid)
            return
        try:
            raw = self.media.metadata(origin, remote)
            status = event_peer.status(raw)["state"]
            if status in ("waiting_for_encoder", "live", "ended", "preparing_replay"):
                self.update(selected, "waiting", retry=10)
                return
            if status != "replay":
                raise ValueError("The connected host has no finished replay for this occurrence.")
            files = media_files(origin, remote, raw)
            if shutil.disk_usage(self.directory).free < 3 * sum(size for _, size in files) + 512 * 1024**2:
                raise ValueError("The Library has insufficient space to retain this recording.")
            self.update(selected, "preparing", retry=30)
            with TemporaryDirectory(dir=self.directory) as temporary:
                folder = Path(temporary)
                duration = self.retain(selected, files, folder)
                if self.stopping.is_set():
                    return
                event = json.loads(selected["settings"])
                zone = ZoneInfo(event["schedule"]["timezone"])
                date = datetime.fromtimestamp(selected["opens"], zone).date().isoformat()
                collection = build_intake(folder, self.institution, identity, event["title"][:105] + " · " + date,
                    [dict(role="playback", path="recording.mp4", format="mp4")],
                    dict(asset_type="video", description=event["description"], duration_seconds=duration,
                         source_privacy="private", source_embed_policy="private"), provider=PROVIDER)
                work = merge_collection(collection, self.root)[uid]
                self.remember_work(work)
            self.update(selected, "ready", work=uid)
        except requests.RequestException:
            self.update(selected, "waiting", retry=30)
        except ValueError as error:
            if not self.stopping.is_set():
                self.update(selected, "failed", error=str(error))
        except (KeyError, OSError, subprocess.TimeoutExpired):
            if not self.stopping.is_set():
                self.update(selected, "failed", error="CuePorch could not retain the host's recording.")

    def retain(self, selected, files, folder):
        inputs = []
        for index, (path, size) in enumerate(files):
            local = folder / f"host-{index}.mp4"
            copied = 0
            with self.media.read(selected["origin"], path, stream=True) as response, local.open("wb") as target:
                for chunk in response.iter_content(256 * 1024):
                    if self.stopping.is_set():
                        raise InterruptedError("The Library worker is stopping.")
                    copied += len(chunk)
                    if copied > size:
                        raise ValueError("A replay file changed during transfer.")
                    target.write(chunk)
            if copied != size:
                raise ValueError("A replay file ended before the transfer was complete.")
            inputs.extend(("-i", str(local)))
        result = run_media([
            *LOCAL_FFMPEG, "-v", "error", *inputs, "-map", "0:V:0",
            "-map", "0:a?", *(["-map", "1:a?"] if len(files) > 1 else []),
            "-c", "copy", "-map_metadata", "-1",
            "-movflags", "+faststart", "-progress", "pipe:1", str(folder / "recording.mp4"),
        ], stopping=self.stopping)
        if result.returncode or not (folder / "recording.mp4").is_file():
            raise ValueError("The host recording could not be prepared for Library playback.")
        durations = [int(value) for line in result.stdout.splitlines()
                     for key, _, value in [line.partition(b"=")] if key == b"out_time_us" and value.isdigit()]
        return durations[-1] / 1_000_000 if durations else None

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=12)


def register(app, tx, staff, access):
    @app.route("/api/live-events/<uid>/<int:opens>/library-recording", methods=["GET", "POST"])
    def event_library_recording(uid, opens):
        staff()
        with tx() as con:
            event, target = access(con, uid, opens)
            current = job(con, uid, opens)
            available = bool(event["save_replay"] and target and target["delivery"] != "staged")
            if request.method == "POST":
                if not available:
                    abort(409, "Enable replay recording and prepare the occurrence before retaining its Library copy.")
                con.execute("""INSERT INTO live_library_recording VALUES(?,?,'waiting',NULL,NULL,0)
                               ON CONFLICT(event,opens) DO UPDATE SET state='waiting',error=NULL,next_attempt=0
                               WHERE live_library_recording.state='failed'""", (uid, opens))
                current = job(con, uid, opens)
            if current:
                return dict(state=current["state"], work=current["work"], error=current["error"])
            return dict(state="available" if available else "unavailable", work=None, error=None)
