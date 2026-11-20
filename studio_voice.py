"""Transient microphone recognition for the Studio teleprompter."""

from collections import deque
from contextlib import suppress
import json
from queue import Empty, Full, Queue
import subprocess
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from flask import abort, make_response, request

from event_captions import live_runtime


PCM_BYTES = 16000  # Half a second of signed 16-bit, 16 kHz mono audio.
IDLE_SECONDS = 20
MAX_RECORDERS = 8


class Recognition:
    def __init__(self, actor, runtime):
        self.actor = actor
        self.pcm = Queue(maxsize=8)
        self.lines = deque(maxlen=16)
        self.lock = Lock()
        self.done = Event()
        self.ending = Event()
        self.state = "starting"
        self.last_activity = monotonic()
        args, env = runtime
        self.process = subprocess.Popen(args, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.reader = Thread(target=self.collect, name="CuePorch prompter results", daemon=True)
        self.writer = Thread(target=self.feed, name="CuePorch prompter microphone", daemon=True)
        self.reader.start()
        self.writer.start()

    def collect(self):
        with self.process.stdout as stream:
            for raw in stream:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                with self.lock:
                    if message["type"] == "ready":
                        self.state = "listening"
                    elif message["type"] == "cue":
                        cue = message["event"]
                        if cue["committed_delta"]:
                            self.lines.append({"sequence": cue["seq"], "text": cue["committed_delta"]})
                    elif message["type"] == "error":
                        self.state = "unavailable"

    def feed(self):
        try:
            while not self.ending.is_set() and self.process.poll() is None:
                try:
                    data = self.pcm.get(timeout=0.25)
                except Empty:
                    continue
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except OSError:
            pass
        finally:
            self.cancel()
            with suppress(OSError):
                self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.reader.join(timeout=2)
            self.done.set()

    def cancel(self):
        self.ending.set()
        if self.process.poll() is None:
            with suppress(OSError):
                self.process.terminate()

    def submit(self, data, after):
        if self.ending.is_set() or self.done.is_set():
            abort(410, "The microphone session has ended.")
        try:
            self.pcm.put_nowait(data)
        except Full:
            abort(503, "Local speech recognition is catching up.")
        with self.lock:
            self.last_activity = monotonic()
            return {"state": self.state, "lines": [line for line in self.lines if line["sequence"] > after]}


def register(app, staff):
    sessions = {}
    lock = Lock()
    closed = Event()
    keeper = None

    def reap():
        while not closed.wait(1):
            with lock:
                for key, session in list(sessions.items()):
                    if session.done.is_set():
                        sessions.pop(key)
                    elif monotonic() - session.last_activity >= IDLE_SECONDS:
                        session.cancel()

    def start_for(actor):
        nonlocal keeper
        runtime = live_runtime("en")
        if not runtime:
            abort(503, "Install the local TextFlowKit voice runtime to use Auto.")
        with lock:
            if closed.is_set():
                abort(503)
            for key in [key for key, session in sessions.items() if session.done.is_set()]:
                sessions.pop(key)
            if len(sessions) >= MAX_RECORDERS:
                abort(503, "There are already active voice sessions. Pause one and try again.")
            for session in sessions.values():
                if session.actor == actor:
                    session.cancel()
            key = uuid4().hex
            try:
                sessions[key] = Recognition(actor, runtime)
            except OSError:
                abort(503, "CuePorch could not start local speech recognition.")
            if keeper is None:
                keeper = Thread(target=reap, name="CuePorch prompter cleanup", daemon=True)
                keeper.start()
        response = make_response({"session": key}, 201)
        response.cache_control.no_store = True
        return response

    def exchange(actor, key):
        with lock:
            session = sessions.get(key)
            if not session or session.actor != actor:
                abort(404)
            if request.method == "DELETE":
                session.cancel()
                return "", 204
        if request.mimetype != "application/octet-stream" or request.content_length != PCM_BYTES:
            abort(400, "Send a single half-second PCM buffer.")
        data = request.stream.read(PCM_BYTES)
        if len(data) != PCM_BYTES:
            abort(400)
        response = make_response(session.submit(data, request.args.get("after", 0, type=int)))
        response.cache_control.no_store = True
        return response

    @app.post("/api/studio/teleprompter/voice")
    def start_prompt_voice():
        return start_for(staff())

    @app.route("/api/studio/teleprompter/voice/<key>", methods=("POST", "DELETE"))
    def prompt_voice(key):
        return exchange(staff(), key)

    def stop():
        closed.set()
        with lock:
            active = list(sessions.values())
            sessions.clear()
        for session in active:
            session.cancel()
        for session in active:
            session.writer.join(timeout=5)
        if keeper:
            keeper.join(timeout=2)

    return stop, start_for, exchange
