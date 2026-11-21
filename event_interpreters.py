"""Receive a temporary, occurrence-scoped interpreter audio feed."""

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic, time

import requests

from event_capture import BusyError, EncoderProgress, MAX_ROOMS, RtmpPublisher, RtmpRelay
from event_interpreter_timing import AlignedAudio


class Input:
    def __init__(self, directory, config, name, language, deadline, parent):
        self.directory = directory
        self.name = name
        self.language = language
        self.deadline = deadline
        self.parent_stop = parent.stop
        self.stop = Event()
        self.done = Event()
        self.publisher = RtmpPublisher(config, self.stop)
        self.path = None
        self.mixer = AlignedAudio(parent.playback, parent.recording) if isinstance(parent, RtmpRelay) else None
        self.mixing = None
        if self.mixer is not None:
            self.mixing = Thread(target=self.mixer.run, args=(self,), name="CuePorch interpreter mixer", daemon=True)
            self.mixing.start()
        Thread(target=self.run, name="CuePorch interpreter receiver", daemon=True).start()

    def live(self):
        return not self.stop.is_set() and not self.parent_stop.is_set() and time() < self.deadline

    def authorize(self, body):
        return self.live() and self.publisher.authorize(body)

    def credentials(self):
        path = self.path
        try:
            receiving = path is not None and time() - (path / "1.m3u8").stat().st_mtime < 5
        except OSError:
            receiving = False
        return self.publisher.credentials() | dict(
            name=self.name, language=self.language, state="receiving" if receiving else "waiting"
        )

    def ready(self, connection):
        # MediaMTX accepts the connection before it finishes discovering the audio track.
        try:
            response = requests.get(
                f"{self.publisher.config.control}/v3/paths/get/{self.publisher.path}",
                timeout=(.8, .8), allow_redirects=False,
            )
            body = response.json() if response.ok else None
        except (ValueError, requests.RequestException):
            return False
        if not isinstance(body, dict) or body.get("online", body.get("ready")) is not True:
            return False
        source = body.get("source")
        return isinstance(source, dict) and source.get("id") == connection

    def receive(self):
        with TemporaryDirectory(dir=self.directory, prefix="audio-", ignore_cleanup_errors=True) as temporary:
            path = Path(temporary)
            args = [
                *self.publisher.read_arguments(True), "-map", "0:a:0", "-vn",
                "-c:a", "aac", "-ar", "48000", "-b:a", "128k", "-f", "hls",
                "-hls_time", "1", "-hls_list_size", "12", "-hls_delete_threshold", "2",
                "-hls_flags", "program_date_time+delete_segments+temp_file",
                "-hls_segment_filename", "1-%09d.ts", "1.m3u8",
            ]
            process = EncoderProgress(args, subprocess.DEVNULL, path).process
            self.path = path
            try:
                while process.poll() is None and self.live():
                    self.stop.wait(.2)
            finally:
                self.path = None
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

    def run(self):
        last_connection = None
        attempts = 0
        try:
            while self.live():
                if not self.publisher.seen.wait(.2):
                    continue
                connection = self.publisher.connection
                if connection != last_connection:
                    last_connection = connection
                    attempts = 0
                if connection and attempts < 3 and self.ready(connection):
                    self.receive()
                    attempts += 1
                self.stop.wait(.4)
        except OSError:
            pass
        finally:
            self.stop.set()
            try:
                self.publisher.disconnect()
            finally:
                if self.mixing is not None:
                    self.mixing.join()
                self.done.set()


class InterpreterInputs:
    def __init__(self, database):
        self.directory = database.parent / "event-interpreters"
        self.lock = Lock()
        self.active = {}

    def list(self, uid, opens):
        with self.lock:
            items = [
                v for (u, o, _), v in self.active.items()
                if u == uid and o == opens and v.live()
            ]
        return [v.credentials() for v in items]

    def get(self, uid, opens, language):
        with self.lock:
            item = self.active.get((uid, opens, language))
            return item if item is not None and item.live() else None

    def audience(self, uid, opens, primary):
        with self.lock:
            for (event, occurrence, _), item in self.active.items():
                if event == uid and occurrence == opens and item.live():
                    mixer = item.mixer
                    if mixer is not None and mixer.primary is primary and mixer.available():
                        return mixer
        return None

    def prepare(self, uid, opens, parent, config, name, language, deadline):
        key = (uid, opens, language)
        with self.lock:
            self.active = {
                k: v for k, v in self.active.items() if not v.done.is_set()
            }
            existing = self.active.get(key)
            if existing is not None:
                if existing.live() and existing.name == name:
                    return existing
                if existing.live():
                    raise BusyError("This interpreter label is already active.")
                raise BusyError("Close the stopped interpreter before restarting.")
            if any(u == uid and o == opens for (u, o, _) in self.active):
                raise BusyError("This occurrence already has an interpreter.")
            if len(self.active) >= MAX_ROOMS:
                raise BusyError("Close another interpreter input before starting this one.")
            self.directory.mkdir(parents=True, exist_ok=True)
            item = Input(self.directory, config, name, language, deadline, parent)
            self.active[key] = item
            return item

    def authorize(self, body):
        with self.lock:
            items = [v for v in self.active.values() if v.live()]
        return any(v.authorize(body) for v in items)

    def stop_input(self, uid, opens, language):
        with self.lock:
            item = self.active.get((uid, opens, language))
            if item is not None:
                item.stop.set()

    def stop_all(self, uid, opens):
        with self.lock:
            for (u, o, _), v in self.active.items():
                if u == uid and o == opens:
                    v.stop.set()

    def close(self):
        with self.lock:
            items = list(self.active.values())
            for v in items:
                v.stop.set()
        deadline = monotonic() + 7.0
        for v in items:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            v.done.wait(remaining)
        with self.lock:
            self.active = {
                k: v for k, v in self.active.items() if not v.done.is_set()
            }
