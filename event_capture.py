"""Bounded browser media intake into a connected event's RTMP relay."""

from contextlib import suppress
from hashlib import sha256
import os
from queue import Empty, Full, Queue
import re
import subprocess
from threading import Event, Lock, Thread
import time
from urllib.parse import urlsplit
from uuid import uuid4

from filelock import FileLock, Timeout


CHUNK_BYTES = 3 * 1024 * 1024
MAX_ROOMS = 4
IDLE_SECONDS = 20


class BusyError(ValueError):
    pass


def ingest_address(encoder):
    server = (encoder.get("secure_server") or encoder.get("server") or "").rstrip("/")
    address = urlsplit(server)
    key = encoder.get("key", "")
    if (address.scheme not in ("rtmp", "rtmps") or not address.hostname or address.username or address.password or
            address.query or address.fragment or not re.fullmatch(r"/[a-zA-Z0-9/_-]+", address.path) or
            not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", key)):
        raise ValueError("The video host did not return a usable encoder endpoint.")
    return server + "/" + key


class Capture:
    def __init__(self, encoder, lease):
        self.token = uuid4().hex
        self.lease = lease
        self.chunks = Queue(maxsize=8)
        self.lock = Lock()
        self.stop = Event()
        self.done = Event()
        self.next = 0
        self.last = None
        self.error = False
        self.process = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-fflags", "+genpts",
             "-protocol_whitelist", "pipe", "-f", "webm", "-i", "pipe:0",
             "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
             "-pix_fmt", "yuv420p", "-vf", "fps=30", "-g", "60", "-sc_threshold", "0",
             "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
             "-rw_timeout", "10000000", "-f", "flv", ingest_address(encoder)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        self.thread = Thread(target=self.receive, name="CuePorch browser encoder", daemon=True)
        self.thread.start()

    def receive(self):
        try:
            deadline = time.monotonic() + IDLE_SECONDS
            while self.process.poll() is None:
                try:
                    chunk = self.chunks.get(timeout=0.3)
                except Empty:
                    if self.stop.is_set() or time.monotonic() >= deadline:
                        break
                    continue
                deadline = time.monotonic() + IDLE_SECONDS
                self.process.stdin.write(chunk)
                self.process.stdin.flush()
            self.process.stdin.close()
            self.error = self.process.wait(timeout=10) != 0
        except (OSError, subprocess.TimeoutExpired):
            self.error = True
        finally:
            try:
                if self.process.poll() is None:
                    with suppress(ProcessLookupError):
                        self.process.terminate()
                    try:
                        self.process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            self.process.kill()
                        self.process.wait()
                with suppress(OSError):
                    self.process.stdin.close()
            finally:
                self.lease.release()
                self.done.set()

    def add(self, sequence, chunk):
        if not 0 < len(chunk) <= CHUNK_BYTES:
            raise ValueError("The media chunk is empty or too large.")
        digest = sha256(chunk).digest()
        with self.lock:
            if self.error or self.done.is_set() or self.stop.is_set():
                raise ValueError("This browser transmission has ended.")
            if sequence == self.next - 1 and digest == self.last:
                return
            if sequence != self.next:
                raise ValueError("The media chunks arrived out of sequence.")
            try:
                self.chunks.put_nowait(chunk)
            except Full:
                raise BusyError("The video host is catching up.") from None
            self.next += 1
            self.last = digest


class Studio:
    def __init__(self, database):
        self.directory = database.parent / "event-browser"
        self.active = {}
        self.lock = Lock()

    def start(self, uid, opens, encoder):
        with self.lock:
            self.active = {key: value for key, value in self.active.items() if not value.done.is_set()}
            if (uid, opens) in self.active or len(self.active) >= MAX_ROOMS:
                raise BusyError("Close another browser transmission before starting this one.")
            self.directory.mkdir(exist_ok=True)
            lease = FileLock(str(self.directory / f"{uid}-{opens}.lock"), timeout=0, thread_local=False)
            try:
                lease.acquire()
            except Timeout:
                raise BusyError("This event already has a browser transmission.") from None
            try:
                room = Capture(encoder, lease)
            except BaseException:
                lease.release()
                raise
            self.active[uid, opens] = room
            return room.token

    def get(self, uid, opens, token):
        with self.lock:
            room = self.active.get((uid, opens))
            return room if room and room.token == token else None

    def withdraw(self, uid, opens):
        with self.lock:
            room = self.active.get((uid, opens))
            if room:
                room.stop.set()

    def close(self):
        with self.lock:
            rooms = list(self.active.values())
            for room in rooms:
                room.stop.set()
        deadline = time.monotonic() + 8
        for room in rooms:
            if not room.done.wait(max(0, deadline - time.monotonic())) and room.process.poll() is None:
                room.process.terminate()
