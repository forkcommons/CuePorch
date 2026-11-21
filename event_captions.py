"""Record timed live captions from a connected event's actual outgoing program."""

from contextlib import suppress
import json
import os
from pathlib import Path
import subprocess
from threading import Event, Thread
from time import monotonic, time

from filelock import FileLock, Timeout
from flask import abort, make_response, request
import requests

import event_peer
from event_audio import LiveAudio
import team_ai_policy


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_caption_setting(
 event TEXT NOT NULL, opens INTEGER NOT NULL, enabled INTEGER NOT NULL, language TEXT NOT NULL,
 PRIMARY KEY(event,opens));
CREATE TABLE IF NOT EXISTS live_caption_job(
 event TEXT NOT NULL, opens INTEGER NOT NULL, language TEXT NOT NULL, state TEXT NOT NULL,
 processed REAL NOT NULL DEFAULT 0, PRIMARY KEY(event,opens));
CREATE TABLE IF NOT EXISTS live_caption_cue(
 event TEXT NOT NULL, opens INTEGER NOT NULL, starts INTEGER NOT NULL, ends INTEGER NOT NULL, text TEXT NOT NULL,
 PRIMARY KEY(event,opens,starts));
"""
LANGUAGES = ("en", "de", "fr", "es", "it", "nl", "pl")
BAD_TIMES = (ValueError, TypeError, KeyError, OverflowError)
DEFAULTS = dict(enabled=False, language="en")
FEATURE = "live_caption"


def require_generation(con, actor):
    _, member = actor(con)
    message = team_ai_policy.restriction(con, FEATURE, member)
    if message:
        abort(403, message)


def live_runtime(language):
    env = os.environ.copy()
    configured = env.get("CUEPORCH_TEXTFLOWKIT_PYTHON")
    if not configured:
        if os.name != "nt" or not env.get("LOCALAPPDATA"):
            return None
        root = Path(env["LOCALAPPDATA"]) / "Programs" / "TextFlowKit"
        configured = str(root / "runtime" / "python.exe")
        env["PATH"] = str(root / "bin") + os.pathsep + env["PATH"]
        env.setdefault("TEXTFLOWKIT_MODELS_DIR", str(root / "models"))
    if not Path(configured).is_file():
        return None
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["TEXTFLOWKIT_OFFLINE"] = "1"
    return [configured, str(Path(__file__).with_name("live_caption_worker.py")), language], env


def settings(con, event, opens):
    row = con.execute("SELECT enabled,language FROM live_caption_setting WHERE event=? AND opens=?", (event, opens)).fetchone()
    if row:
        return dict(enabled=bool(row["enabled"]), language=row["language"])
    saved = con.execute("SELECT json_extract(settings,'$.setup.captions') FROM live_event WHERE id=?", (event,)).fetchone()
    return json.loads(saved[0]) if saved and saved[0] else DEFAULTS.copy()


def active_track(con, event, opens):
    if not opens:
        return False
    row = settings(con, event, opens)
    present = con.execute("SELECT 1 FROM live_caption_job WHERE event=? AND opens=?", (event, opens)).fetchone()
    return bool((row and row["enabled"]) or present)


def approval_key(event, opens):
    return f"live-captions-{event}-{opens}"


class LiveCaptionJobs:
    def __init__(self, tx, action, media, access, database):
        self.tx = tx
        self.action = action
        self.media = media
        self.access = access
        self.lease_root = database.parent / "event-captions"
        self.stopping = Event()
        self.jobs = {}
        self.thread = Thread(target=self.scan, name="CuePorch live caption scheduler", daemon=True)
        self.thread.start()

    def scan(self):
        while not self.stopping.wait(2):
            with self.tx() as con:
                rows = con.execute("""SELECT o.event,o.opens,o.origin,o.remote,e.settings,
                    COALESCE(s.language,json_extract(e.settings,'$.setup.captions.language')) AS language
                    FROM live_occurrence o JOIN live_event e ON o.event=e.id
                    LEFT JOIN live_caption_setting s ON s.event=o.event AND s.opens=o.opens
                    WHERE COALESCE(s.enabled,json_extract(e.settings,'$.setup.captions.enabled'),0)=1
                      AND o.delivery='ready' AND o.opens<=?""", (int(time()),)).fetchall()
            self.jobs = {scope: job for scope, job in self.jobs.items() if job.is_alive()}
            for row in rows:
                scope = (row["event"], row["opens"])
                if scope not in self.jobs and len(self.jobs) < 4 and self.allowed(scope):
                    job = Thread(target=self.process, args=(row,), name="CuePorch local live captions", daemon=True)
                    self.jobs[scope] = job
                    job.start()

    def allowed(self, scope):
        with self.tx() as con:
            row = settings(con, *scope)
            return bool(row and row["enabled"] and not team_ai_policy.restriction(con, FEATURE, None)
                        and self.access(con, *scope))

    def update(self, scope, language, state):
        with self.tx() as con:
            con.execute("""INSERT INTO live_caption_job(event,opens,language,state) VALUES(?,?,?,?)
                           ON CONFLICT(event,opens) DO UPDATE SET state=excluded.state""", (*scope, language, state))
            return con.execute("SELECT processed FROM live_caption_job WHERE event=? AND opens=?", scope).fetchone()["processed"]

    def ingest(self, stream, scope, first, language):
        with stream:
            for line in stream:
                try:
                    message = json.loads(line)
                    if message.get("type") != "cue":
                        continue
                    cue = message["event"]
                    text, words = cue["committed_delta"].strip(), cue["words"]
                    if not text or not words:
                        continue
                    start = round((first + words[0]["start"]) * 1000)
                    end = max(start + 800, round((first + words[-1]["end"]) * 1000))
                except BAD_TIMES:
                    continue
                with self.tx() as con:
                    policy = team_ai_policy.load(con)[1]
                    configured = settings(con, *scope)
                    if (configured["enabled"] and self.access(con, *scope)
                            and not team_ai_policy.restriction(con, FEATURE, None, policy)):
                        revised, = team_ai_policy.apply_dictionary([{"text": text}], language, policy["dictionary"])
                        self.action(con, approval_key(*scope), "caption", dict(event=scope[0], occurrence=scope[1]))
                        con.execute("INSERT OR IGNORE INTO live_caption_cue VALUES(?,?,?,?,?)",
                                    (*scope, start, end, revised["text"][:400]))

    def process(self, row):
        scope = (row["event"], row["opens"])
        self.lease_root.mkdir(exist_ok=True)
        try:
            with FileLock(self.lease_root / f"{scope[0]}-{scope[1]}.lock", timeout=0):
                self.run(row, scope)
        except Timeout:
            return

    def run(self, row, scope):
        language = row["language"]
        runtime = live_runtime(language)
        if not runtime:
            self.update(scope, language, "engine_unavailable")
            return
        try:
            raw = self.media.metadata(row["origin"], row["remote"])
            if event_peer.status(raw)["state"] != "live":
                return
            playlist = next(entry["playlistUrl"] for entry in raw["streamingPlaylists"] if entry["type"] == 1)
            audio = LiveAudio(self.media, row["origin"], row["remote"], playlist)
            watermark = self.update(scope, language, "listening")
            self.read_program(scope, audio, runtime, watermark, language)
            self.update(scope, language, "stopped")
        except (StopIteration, OSError, ValueError, KeyError, subprocess.TimeoutExpired, requests.RequestException):
            self.update(scope, language, "media_or_engine_unavailable")

    def read_program(self, scope, audio, runtime, watermark, language):
        worker = reader = None
        last_end = None
        activity = monotonic()
        try:
            while not self.stopping.is_set() and self.allowed(scope):
                done = False
                for chunk in audio.fragments():
                    if chunk.start < watermark - 0.05:
                        continue
                    if self.stopping.is_set() or not self.allowed(scope):
                        return
                    if last_end is not None and abs(chunk.start - last_end) > 0.2:
                        return  # A different HLS discontinuity needs a new sample-clock anchor.
                    pcm = audio.pcm(chunk)
                    if not pcm:
                        continue
                    if worker is None:
                        args, env = runtime
                        worker = subprocess.Popen(args, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                  stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        reader = Thread(target=self.ingest, args=(worker.stdout, scope, chunk.start, language), daemon=True)
                        reader.start()
                    worker.stdin.write(pcm)
                    worker.stdin.flush()
                    watermark = last_end = chunk.start + chunk.duration
                    with self.tx() as con:
                        con.execute("UPDATE live_caption_job SET processed=? WHERE event=? AND opens=?", (watermark, *scope))
                    activity = monotonic()
                    done = True
                if not done:
                    if monotonic() - activity > 24:
                        return
                    self.stopping.wait(1.5)
        finally:
            if worker is not None:
                with suppress(OSError):
                    worker.stdin.close()
                try:
                    code = worker.wait(timeout=20)
                    if code:
                        raise ValueError("The native live transcription worker exited unexpectedly.")
                except subprocess.TimeoutExpired:
                    worker.terminate()
                    try:
                        worker.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait(timeout=4)
                    raise
                finally:
                    if reader is not None:
                        reader.join(timeout=2)

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=4)
        for thread in self.jobs.values():
            thread.join(timeout=24)


def register(app, tx, staff, operator_access, audience_access, ai_actor):
    @app.route("/api/live-events/<event>/<int:opens>/captions", methods=["GET", "PUT"])
    def event_caption_settings(event, opens):
        staff()
        with tx() as con:
            operator_access(con, event, opens)
            prior = settings(con, event, opens)
            state = con.execute("SELECT state,language FROM live_caption_job WHERE event=? AND opens=?", (event, opens)).fetchone()
            if request.method == "PUT":
                body = request.get_json()
                if not isinstance(body, dict) or type(body.get("enabled")) is not bool or body.get("language") not in LANGUAGES:
                    abort(422, "Choose whether to caption the event and select a language.")
                if body["enabled"]:
                    require_generation(con, ai_actor)
                if state and state["language"] != body["language"]:
                    abort(409, "Choose the language before the first live caption is generated.")
                con.execute("INSERT OR REPLACE INTO live_caption_setting VALUES(?,?,?,?)", (event, opens, body["enabled"], body["language"]))
                prior = settings(con, event, opens)
            available = con.execute("SELECT count(*) FROM live_caption_cue WHERE event=? AND opens=?", (event, opens)).fetchone()[0]
            blocked = team_ai_policy.restriction(con, FEATURE, None) is not None
            return dict(enabled=bool(prior and prior["enabled"]), language=prior["language"] if prior else "en",
                        status="blocked" if blocked else state["state"] if state else "waiting_for_stream",
                        cue_count=available)

    @app.get("/events/<slug>/<int:opens>/captions")
    def viewer_event_captions(slug, opens):
        with tx() as con:
            scope, _ = audience_access(con, slug, opens)
            uid = scope[0]
            row = settings(con, uid, opens)
            gate = con.execute("SELECT status FROM action WHERE id=?", (approval_key(uid, opens),)).fetchone()
            rows = []
            if gate and gate["status"] == "eligible" and row and row["enabled"]:
                query = con.execute("""SELECT starts,ends,text FROM live_caption_cue WHERE event=? AND opens=?
                                      AND starts>? ORDER BY starts LIMIT 1000""", (uid, opens, request.args.get("since", 0, type=int)))
                rows = [dict(cue) for cue in query]
        response = make_response(dict(cues=rows, enabled=bool(row and row["enabled"]),
                                      language=row["language"] if row else None))
        response.cache_control.no_store = True
        return response
