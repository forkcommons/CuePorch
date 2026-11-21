"""Local production sources for a connected event's RTMP relay."""

from contextlib import suppress
from hashlib import sha256
from hmac import compare_digest
from ipaddress import ip_address
import math
from pathlib import Path
from queue import Empty, Full, Queue
import re
import secrets
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
import time
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

from filelock import FileLock, Timeout
import requests

import event_backup
from event_dash_buffer import Buffer as DashBuffer, TARGET_DELAY


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


def output_arguments(encoder, playback=None):
    codec = [
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
        "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-vf", "fps=30", "-g", "30" if playback else "60",
        "-sc_threshold", "0", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
    ]
    address = ingest_address(encoder)
    if not playback:
        return [*codec, "-rw_timeout", "10000000", "-f", "flv", address]
    local = (
        f"[f=hls:onfail=ignore:hls_time=1:hls_list_size={playback.window}:hls_delete_threshold=2:"
        "hls_flags=delete_segments+independent_segments+program_date_time+temp_file:"
        "hls_segment_filename=0-%09d.ts]0.m3u8"
    )
    relay = ("[f=fifo:fifo_format=flv:queue_size=120:drop_pkts_on_overflow=1:onfail=ignore:"
             "attempt_recovery=1:max_recovery_attempts=3:restart_with_keyframe=1]" + address)
    return [*codec, "-f", "tee", local + "|" + relay]


def probe_arguments(playback):
    return ["-probesize", "128k", "-analyzeduration", "300000"] if playback else []


class LocalPlayback:
    def __init__(self, directory, window):
        self.identity = uuid4().hex
        self.window = max(4, window)
        self.storage = TemporaryDirectory(dir=directory, prefix="hls-", ignore_cleanup_errors=True)
        self.path = Path(self.storage.name)

    def live(self):
        try:
            with (self.path / "0.m3u8").open("rb") as stream:
                if time.time() - (self.path / "0.m3u8").stat().st_mtime > 6:
                    return False
                return stream.read(256 * 1024).count(b"#EXTINF:") >= 2
        except OSError:
            return False

    def close(self):
        with suppress(OSError):
            self.storage.cleanup()


class EncoderProgress:
    def __init__(self, args, stdin, cwd=None):
        self.latest = None
        self.process = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-progress", "pipe:1", "-stats_period", "1", *args],
            cwd=cwd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        Thread(target=self.read, name="CuePorch encoder progress", daemon=True).start()

    def read(self):
        # FFmpeg batches progress fields until each progress=continue/end marker.
        with self.process.stdout as stream:
            values = {}
            for line in stream:
                name, _, value = line.decode("ascii", errors="replace").strip().partition("=")
                if name in ("fps", "bitrate", "drop_frames"):
                    try:
                        measured = float(value.removesuffix("kbits/s"))
                    except ValueError:
                        continue
                    if math.isfinite(measured) and measured >= 0:
                        values[name] = int(measured) if name == "drop_frames" else measured
                elif name == "progress":
                    self.latest = (time.monotonic(), values)
                    values = {}

    def sample(self):
        latest = self.latest
        if self.process.poll() is not None or latest is None or time.monotonic() - latest[0] > 5:
            return None
        return latest[1]


class Capture:
    def __init__(self, encoder, lease, playback=None):
        self.token = uuid4().hex
        self.lease = lease
        self.chunks = Queue(maxsize=8)
        self.lock = Lock()
        self.stop = Event()
        self.done = Event()
        self.next = 0
        self.last = None
        self.error = False
        self.playback = playback
        self.progress = EncoderProgress(
            ["-fflags", "+genpts", *probe_arguments(playback),
             "-protocol_whitelist", "pipe", "-f", "webm", "-i", "pipe:0", *output_arguments(encoder, playback)],
            subprocess.PIPE, playback.path if playback else None,
        )
        self.process = self.progress.process
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
                if self.playback:
                    self.playback.close()
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


def srt_endpoint(address, port):
    if not isinstance(address, str):
        raise ValueError("Enter an IP address for the receiving interface.")
    try:
        host = ip_address(address)
    except ValueError:
        raise ValueError("Enter an IP address for the receiving interface.") from None
    if host.is_multicast or host.is_unspecified or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Choose a receiving interface and UDP port between 1024 and 65535.")
    return f"srt://{'[' + str(host) + ']' if host.version == 6 else host}:{port}"


class Relay:
    def __init__(self, args, lease, playback=None):
        self.stop = Event()
        self.done = Event()
        self.lease = lease
        self.playback = playback
        self.progress = EncoderProgress(args, subprocess.DEVNULL, playback.path if playback else None)
        self.process = self.progress.process
        try:
            self.process.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise ValueError("The encoder stopped before transmitting.")
        Thread(target=self.monitor, name="CuePorch event encoder", daemon=True).start()

    def monitor(self):
        try:
            while self.process.poll() is None:
                if self.stop.wait(0.35):
                    with suppress(ProcessLookupError):
                        self.process.terminate()
                    try:
                        self.process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            self.process.kill()
                        self.process.wait()
                    break
        finally:
            if self.playback:
                self.playback.close()
            self.lease.release()
            self.done.set()


class SrtPublisher:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.passphrase = secrets.token_hex(16)

    def read_arguments(self, playback):
        listener = self.endpoint + "?" + urlencode({
            "mode": "listener", "passphrase": self.passphrase, "enforced_encryption": 1,
            "listen_timeout": 300_000_000, "timeout": 15_000_000,
        })
        return ["-fflags", "+genpts", *probe_arguments(playback), "-protocol_whitelist", "srt,udp",
                "-f", "mpegts", "-i", listener]

    def credentials(self):
        return dict(server=self.endpoint, passphrase=self.passphrase, key_bytes=16)


class SrtRelay(Relay):
    def __init__(self, encoder, endpoint, lease, playback=None):
        self.endpoints = (endpoint, None)
        self.publisher = SrtPublisher(endpoint)
        super().__init__([*self.publisher.read_arguments(playback), *output_arguments(encoder, playback)], lease, playback)

    def credentials(self):
        return self.publisher.credentials()


class RtmpPublisher:
    """Provide separate write and read credentials for a MediaMTX input."""

    def __init__(self, config, stop):
        self.config = config
        self.stop = stop
        self.seen = Event()
        self.route = uuid4().hex
        self.path = f"cueporch/{self.route}"
        self.write_key = secrets.token_urlsafe(32)
        self.read_key = secrets.token_urlsafe(32)
        self.publishers = set()
        self.connection = None
        self.auth_lock = Lock()

    def read_arguments(self, playback):
        config = self.config
        query = urlencode({"user": "cueporch-reader", "pass": self.read_key})
        source = f"{config.read}/{self.path}?{query}"
        tls = ["-tls_verify", "1"] if config.read.startswith("rtmps://") else []
        return ["-fflags", "+genpts", *probe_arguments(playback), "-rtmp_live", "live",
                *tls, "-rw_timeout", "3000000", "-i", source]

    def credentials(self):
        query = urlencode({"user": "cueporch-publisher", "pass": self.write_key})
        return dict(server=self.config.publish + "/cueporch", key=self.route + "?" + query, secure_server=None)

    def authorize(self, item):
        if item.get("path") != self.path or self.stop.is_set():
            return False
        user, password, action = (item.get(key) for key in ("user", "password", "action"))
        if not isinstance(password, str):
            return False
        supplied = password.encode("utf8")
        if action == "read" and user == "cueporch-reader":
            return compare_digest(supplied, self.read_key.encode("ascii"))
        if action == "publish" and user == "cueporch-publisher" and compare_digest(supplied, self.write_key.encode("ascii")):
            try:
                connection = str(UUID(str(item.get("id"))))
            except ValueError:
                return False
            with self.auth_lock:
                if self.stop.is_set():
                    return False
                self.publishers.add(connection)
                self.connection = connection
                self.seen.set()
            return True
        return False

    def disconnect(self):
        with self.auth_lock:
            publishers = list(self.publishers)
        protocols = ("rtmps", "rtmp") if self.config.publish.startswith("rtmps://") else ("rtmp", "rtmps")
        for connection in publishers:
            for protocol in protocols:
                with suppress(requests.RequestException):
                    response = requests.post(
                        f"{self.config.control}/v3/{protocol}/conns/kick/{connection}", timeout=2)
                    if response.status_code != 404:
                        break


class RtmpRelay:
    """Pull an authenticated local MediaMTX publisher into the existing event fanout."""

    def __init__(self, encoder, config, lease, playback, recording=None):
        self.lease = lease
        self.playback = playback
        self.stop = Event()
        self.done = Event()
        self.publisher = RtmpPublisher(config, self.stop)
        self.progress = None
        self.public = encoder["public"]
        self.recording = recording
        self.args = [*self.publisher.read_arguments(playback), *output_arguments(encoder, playback)]
        Thread(target=self.run, name="CuePorch RTMP receiver", daemon=True).start()

    @property
    def process(self):
        return self.progress.process if self.progress else None

    def credentials(self):
        return self.publisher.credentials() | dict(public=self.public, preview=False)

    def authorize(self, item):
        return self.publisher.authorize(item)

    def run(self):
        deadline = time.monotonic() + 300
        try:
            while not self.stop.is_set() and time.monotonic() < deadline:
                if not self.publisher.seen.wait(.25):
                    continue
                self.progress = EncoderProgress(self.args, subprocess.DEVNULL, self.playback.path)
                process = self.process
                while process.poll() is None and not self.stop.wait(.25):
                    if self.recording is not None:
                        self.recording.collect(self.playback.path)
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if self.progress.latest or self.stop.wait(.45):
                    break
        except OSError:
            pass
        finally:
            self.stop.set()
            try:
                if self.recording is not None:
                    self.recording.collect(self.playback.path)
                    self.recording.finish()
            except (OSError, ValueError):
                pass
            finally:
                self.publisher.disconnect()
                self.playback.close()
                self.lease.release()
                self.done.set()


class BackupInput:
    def __init__(self, publisher, shared_output, height):
        self.publisher = publisher
        self.listener = isinstance(publisher, SrtPublisher)
        self.progress = None
        self.storage = None
        self.connection = None
        self.retry_until = 0
        self.retry_at = 0
        self.newest = None
        self.updated = 0
        self.shared_output = shared_output
        width = round(height * 8 / 9) * 2
        scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2"
        self.arguments = [*self.publisher.read_arguments(True), "-map", "0:v:0", "-map", "0:a:0",
                          "-vf", f"{scale},pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
                          "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                          "-g", "30", "-sc_threshold", "0", "-c:a", "aac", "-ac", "2", "-ar", "48000",
                          "-b:a", "128k", "-f", "hls", "-hls_time", "1", "-hls_list_size", "12",
                          "-hls_flags", "delete_segments+independent_segments+program_date_time+temp_file",
                          "-hls_segment_filename", "feed-%09d.ts", "feed.m3u8"]

    def poll(self, now):
        connection = self.publisher.endpoint if self.listener else self.publisher.connection
        if not connection:
            return []
        if not self.progress or self.progress.process.poll() is not None:
            if connection != self.connection:
                self.connection = connection
                self.retry_until = now + 8
                self.newest = None
            try_rtmp = now < self.retry_until and (not self.progress or not self.progress.latest or self.newest is None)
            if now >= self.retry_at and (self.listener or try_rtmp):
                self.retry_at = now + .5
                if self.storage:
                    self.storage.cleanup()
                self.storage = TemporaryDirectory(dir=self.shared_output, prefix="input-", ignore_cleanup_errors=True)
                try:
                    self.progress = EncoderProgress(self.arguments, subprocess.DEVNULL, self.storage.name)
                except OSError:
                    self.progress = None
        if not self.progress or self.progress.process.poll() is not None:
            return []
        fragments = event_backup.snapshot(Path(self.storage.name))
        if fragments and fragments[-1] != self.newest:
            self.newest = fragments[-1]
            self.updated = now
        return fragments if now - self.updated <= event_backup.FRESH_FOR and len(fragments) >= 2 else []

    def finish(self):
        if self.progress:
            finish_process(self.progress.process, 0)
        if self.storage:
            self.storage.cleanup()
        if not self.listener:
            self.publisher.disconnect()


def finish_process(process, grace):
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            process.kill()
        process.wait()


class RedundantRelay:
    def __init__(self, encoder, lease, playback, height, end_at, stop, publishers):
        self.lease = lease
        self.playback = playback
        self.stop = stop
        self.done = Event()
        self.progress = None
        self.public = encoder["public"]
        self.height = height
        self.end_at = end_at
        self.connected = dict(primary=False, backup=False)
        self.feeds = {role: BackupInput(publisher, playback.path, height) for role, publisher in publishers.items()}
        self.program = event_backup.Program(playback.path, playback.window)
        self.destination = ingest_address(encoder)
        Thread(target=self.run, name="CuePorch redundant programme", daemon=True).start()

    @property
    def process(self):
        return self.progress.process if self.progress else None

    def health(self):
        return dict(selected=self.program.selected if self.connected.get(self.program.selected) else None,
                    primary=self.connected["primary"], backup=self.connected["backup"])

    def run(self):
        try:
            with event_backup.serve_program(self.playback.path) as address:
                self.relay(address)
        finally:
            self.stop.set()
            for feed in self.feeds.values():
                feed.finish()
            self.playback.close()
            self.lease.release()
            self.done.set()

    def relay(self, address):
        arguments = [
            "-re", "-live_start_index", "0", "-fflags", "+genpts", "-dts_delta_threshold", "0.25",
            "-protocol_whitelist", "http,tcp,crypto", "-i", address, "-map", "0:v:0", "-map", "0:a:0",
            "-c", "copy", "-rw_timeout", "10000000", "-f", "flv", self.destination,
        ]
        accept_until = time.monotonic() + 300
        try:
            while not self.stop.is_set() and time.time() < self.end_at:
                now = time.monotonic()
                if not self.program.fragments and now >= accept_until:
                    break
                ready = {name: feed.poll(now) for name, feed in self.feeds.items()}
                self.connected = {name: bool(fragments) for name, fragments in ready.items()}
                self.program.update(ready)
                if self.playback.live() and (not self.progress or self.process.poll() is not None):
                    self.progress = EncoderProgress(arguments, subprocess.DEVNULL, self.playback.path)
                self.stop.wait(.2)
        finally:
            self.stop.set()
            if self.process:
                if self.program.fragments:
                    self.program.publish(ended=True)
                finish_process(self.process, 5)


class RedundantRtmpRelay(RedundantRelay):
    def __init__(self, encoder, config, lease, playback, height, end_at):
        stop = Event()
        publishers = {role: RtmpPublisher(config, stop) for role in ("primary", "backup")}
        super().__init__(encoder, lease, playback, height, end_at, stop, publishers)

    def credentials(self):
        return self.feeds["primary"].publisher.credentials() | dict(
            public=self.public, preview=False, backup_key=self.feeds["backup"].publisher.credentials()["key"],
            output_height=self.height)

    def authorize(self, item):
        return any(feed.publisher.authorize(item) for feed in self.feeds.values())


class RedundantSrtRelay(RedundantRelay):
    def __init__(self, encoder, endpoints, lease, playback, height, end_at):
        self.endpoints = endpoints
        publishers = dict(primary=SrtPublisher(endpoints[0]), backup=SrtPublisher(endpoints[1]))
        super().__init__(encoder, lease, playback, height, end_at, Event(), publishers)

    def credentials(self):
        backup = self.feeds["backup"].publisher.credentials()
        return self.feeds["primary"].publisher.credentials() | dict(
            backup_server=backup["server"], backup_passphrase=backup["passphrase"], output_height=self.height)


RTMP_ROOMS = (RtmpRelay, RedundantRtmpRelay)
SRT_ROOMS = (SrtRelay, RedundantSrtRelay)


class DashRelay:
    """Feed the delayed DASH contribution to the connected host through local HLS."""

    def __init__(self, encoder, lease, staging, end_at):
        self.lease = lease
        self.staging = staging
        self.playback = DashBuffer(staging.path, staging.identity, staging.window)
        self.key = secrets.token_urlsafe(32)
        self.stop = Event()
        self.done = Event()
        self.progress = None
        self.accept_until = time.monotonic() + 300
        self.end_at = end_at
        self.arguments = ["-re", "-live_start_index", "0", "-protocol_whitelist", "file,crypto",
                          "-i", "relay-master.m3u8", *output_arguments(encoder)]
        Thread(target=self.run, name="CuePorch DASH contribution", daemon=True).start()

    @property
    def process(self):
        return self.progress.process if self.progress else None

    def credentials(self):
        return dict(receiver=self.playback.identity, token=self.key, buffer_seconds=TARGET_DELAY)

    def run(self):
        saved = {}
        try:
            while not self.stop.is_set() and time.time() < self.end_at:
                if self.playback.origin is None and time.monotonic() >= self.accept_until:
                    break
                playlists = self.playback.relay_playlists()
                for name, document in playlists.items():
                    if saved.get(name) != document:
                        destination = self.staging.path / name
                        temporary = destination.with_suffix(".tmp")
                        try:
                            temporary.write_text(document, encoding="ascii")
                            temporary.replace(destination)
                        except PermissionError:
                            continue
                        saved[name] = document
                if playlists and all(saved.get(name) == value for name, value in playlists.items()):
                    if not self.progress or (self.process.poll() is not None and not self.progress.latest):
                        with suppress(OSError):
                            self.progress = EncoderProgress(self.arguments, subprocess.DEVNULL, self.staging.path)
                self.stop.wait(.5)
        finally:
            self.stop.set()
            self.playback.close()
            if self.process and self.process.poll() is None:
                with suppress(ProcessLookupError):
                    self.process.terminate()
                try:
                    self.process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        self.process.kill()
                    self.process.wait()
            self.staging.close()
            self.lease.release()
            self.done.set()


class Studio:
    def __init__(self, database):
        self.directory = database.parent / "event-browser"
        self.active = {}
        self.lock = Lock()

    def prepare(self, uid, opens, construct, playback_window=None):
        with self.lock:
            self.active = {key: value for key, value in self.active.items() if not value.done.is_set()}
            existing = self.active.get((uid, opens))
            if existing or len(self.active) >= MAX_ROOMS:
                raise BusyError("Close another event transmission before starting this one.")
            self.directory.mkdir(exist_ok=True)
            lease = FileLock(str(self.directory / f"{uid}-{opens}.lock"), timeout=0, thread_local=False)
            try:
                lease.acquire()
            except Timeout:
                raise BusyError("This event already has a browser transmission.") from None
            playback = None
            try:
                if playback_window is not None:
                    playback = LocalPlayback(self.directory, playback_window)
                room = construct(lease, playback)
            except BaseException:
                if playback:
                    playback.close()
                lease.release()
                raise
            self.active[uid, opens] = room
            return room

    def start(self, uid, opens, encoder, playback_window=None):
        return self.prepare(uid, opens, lambda lease, playback: Capture(encoder, lease, playback),
                            playback_window=playback_window).token

    def srt(self, uid, opens, encoder, endpoints, playback_window=None, height=None, end_at=None):
        def receive(lease, playback):
            if endpoints[1]:
                return RedundantSrtRelay(encoder, endpoints, lease, playback, height, end_at)
            return SrtRelay(encoder, endpoints[0], lease, playback)
        return self.prepare(uid, opens, receive, playback_window=playback_window).credentials()

    def rtmp(self, uid, opens, encoder, config, window, height=None, end_at=None, recording=None):
        def receive(lease, playback):
            if height is not None:
                return RedundantRtmpRelay(encoder, config, lease, playback, height, end_at)
            return RtmpRelay(encoder, config, lease, playback, recording)
        return self.prepare(uid, opens, receive, playback_window=window).credentials()

    def rtmp_room(self, uid, opens):
        with self.lock:
            room = self.active.get((uid, opens))
            if isinstance(room, RTMP_ROOMS) and not room.stop.is_set() and not room.done.is_set():
                return room
        return None

    def rtmp_credentials(self, uid, opens):
        room = self.rtmp_room(uid, opens)
        return room.credentials() if room else None

    def authorize_rtmp(self, item):
        with self.lock:
            rooms = [room for room in self.active.values() if isinstance(room, RTMP_ROOMS)]
        return any(room.authorize(item) for room in rooms)

    def close_rtmp(self, uid, opens):
        with self.lock:
            room = self.active.get((uid, opens))
            if isinstance(room, RTMP_ROOMS):
                room.stop.set()

    def dash(self, uid, opens, encoder, window, end_at):
        return self.prepare(uid, opens, lambda lease, playback: DashRelay(encoder, lease, playback, end_at),
                            playback_window=window).credentials()

    def dash_contribution(self, uid, opens):
        with self.lock:
            room = self.active.get((uid, opens))
            if isinstance(room, DashRelay) and not room.stop.is_set() and not room.done.is_set():
                return room
        return None

    def close_dash(self, uid, opens):
        with self.lock:
            room = self.active.get((uid, opens))
            if isinstance(room, DashRelay):
                room.stop.set()

    def program(self, uid, opens, encoder, source):
        args = ["-re", "-protocol_whitelist", "file", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
                "-c", "copy", "-rw_timeout", "10000000", "-f", "flv", ingest_address(encoder)]
        return self.prepare(uid, opens, lambda lease, _: Relay(args, lease))

    def playback(self, uid, opens, identity=None):
        with self.lock:
            room = self.active.get((uid, opens))
            if not room or room.stop.is_set() or room.done.is_set():
                return None
            source = room.playback
        if source and (identity is None or identity == source.identity) and source.live():
            return source
        return None

    def srt_credentials(self, uid, opens, endpoints):
        with self.lock:
            room = self.active.get((uid, opens))
            if (isinstance(room, SRT_ROOMS) and room.endpoints == endpoints and
                    not room.done.is_set() and not room.stop.is_set()):
                return room.credentials()
        return None

    def health(self, uid, opens):
        with self.lock:
            room = self.active.get((uid, opens))
            if not room or room.done.is_set() or room.stop.is_set():
                return None
        source = ("browser" if isinstance(room, Capture) else "srt" if isinstance(room, SRT_ROOMS) else
                  "rtmp" if isinstance(room, RTMP_ROOMS) else "dash" if isinstance(room, DashRelay) else "program")
        progress = room.progress
        values = progress.sample() if progress else None
        information = dict(source=source, metrics=values)
        if isinstance(room, RedundantRelay):
            information["inputs"] = room.health()
        return information

    def get(self, uid, opens, token):
        with self.lock:
            room = self.active.get((uid, opens))
            return room if isinstance(room, Capture) and room.token == token else None

    def close_srt(self, uid, opens):
        with self.lock:
            room = self.active.get((uid, opens))
            if isinstance(room, SRT_ROOMS):
                room.stop.set()

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
            if not room.done.wait(max(0, deadline - time.monotonic())) and room.process and room.process.poll() is None:
                room.process.terminate()
