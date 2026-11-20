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


class SrtRelay(Relay):
    def __init__(self, encoder, endpoint, lease, playback=None):
        self.endpoint = endpoint
        self.passphrase = secrets.token_hex(16)
        listener = endpoint + "?" + urlencode({
            "mode": "listener", "passphrase": self.passphrase, "enforced_encryption": 1,
            "listen_timeout": 300_000_000, "timeout": 15_000_000,
        })
        super().__init__(
            ["-fflags", "+genpts", *probe_arguments(playback), "-protocol_whitelist", "srt,udp",
             "-f", "mpegts", "-i", listener, *output_arguments(encoder, playback)], lease, playback,
        )

    def credentials(self):
        return dict(server=self.endpoint, passphrase=self.passphrase, key_bytes=16)


class RtmpRelay:
    """Pull an authenticated local MediaMTX publisher into the existing event fanout."""

    def __init__(self, encoder, config, lease, playback):
        self.config = config
        self.lease = lease
        self.playback = playback
        self.stop = Event()
        self.done = Event()
        self.publisher_seen = Event()
        self.route = uuid4().hex
        self.path = f"cueporch/{self.route}"
        self.write_key = secrets.token_urlsafe(32)
        self.read_key = secrets.token_urlsafe(32)
        self.publishers = set()
        self.auth_lock = Lock()
        self.progress = None
        self.public = encoder["public"]
        query = urlencode({"user": "cueporch-reader", "pass": self.read_key})
        source = f"{config.read}/{self.path}?{query}"
        tls = ["-tls_verify", "1"] if config.read.startswith("rtmps://") else []
        self.args = ["-fflags", "+genpts", *probe_arguments(playback), "-rtmp_live", "live",
                     *tls, "-rw_timeout", "3000000", "-i", source, *output_arguments(encoder, playback)]
        Thread(target=self.run, name="CuePorch RTMP receiver", daemon=True).start()

    @property
    def process(self):
        return self.progress.process if self.progress else None

    def credentials(self):
        query = urlencode({"user": "cueporch-publisher", "pass": self.write_key})
        return dict(server=self.config.publish + "/cueporch", key=self.route + "?" + query,
                    secure_server=None, public=self.public, preview=False)

    def authorize(self, item):
        if item.get("path") != self.path or self.stop.is_set() or self.done.is_set():
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
                self.publisher_seen.set()
            return True
        return False

    def run(self):
        deadline = time.monotonic() + 300
        try:
            while not self.stop.is_set() and time.monotonic() < deadline:
                if not self.publisher_seen.wait(.25):
                    continue
                self.progress = EncoderProgress(self.args, subprocess.DEVNULL, self.playback.path)
                process = self.process
                while process.poll() is None and not self.stop.wait(.25):
                    pass
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
            self.playback.close()
            self.lease.release()
            self.done.set()


class Studio:
    def __init__(self, database):
        self.directory = database.parent / "event-browser"
        self.active = {}
        self.lock = Lock()

    def prepare(self, uid, opens, construct, srt_address=None, playback_window=None):
        with self.lock:
            self.active = {key: value for key, value in self.active.items() if not value.done.is_set()}
            existing = self.active.get((uid, opens))
            if isinstance(existing, SrtRelay) and existing.endpoint == srt_address and not existing.stop.is_set():
                return existing
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

    def srt(self, uid, opens, encoder, endpoint, playback_window=None):
        return self.prepare(uid, opens, lambda lease, playback: SrtRelay(encoder, endpoint, lease, playback),
                            srt_address=endpoint, playback_window=playback_window).credentials()

    def rtmp(self, uid, opens, encoder, config, window):
        return self.prepare(uid, opens, lambda lease, playback: RtmpRelay(encoder, config, lease, playback),
                            playback_window=window).credentials()

    def rtmp_credentials(self, uid, opens):
        with self.lock:
            room = self.active.get((uid, opens))
            if isinstance(room, RtmpRelay) and not room.stop.is_set() and not room.done.is_set():
                return room.credentials()
        return None

    def authorize_rtmp(self, item):
        with self.lock:
            rooms = [room for room in self.active.values() if isinstance(room, RtmpRelay)]
        return any(room.authorize(item) for room in rooms)

    def close_rtmp(self, uid, opens):
        with self.lock:
            room = self.active.get((uid, opens))
            if isinstance(room, RtmpRelay):
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

    def srt_credentials(self, uid, opens, endpoint):
        with self.lock:
            room = self.active.get((uid, opens))
            if isinstance(room, SrtRelay) and room.endpoint == endpoint and not room.done.is_set() and not room.stop.is_set():
                return room.credentials()
        return None

    def health(self, uid, opens):
        with self.lock:
            room = self.active.get((uid, opens))
            if not room or room.done.is_set() or room.stop.is_set():
                return None
        source = ("browser" if isinstance(room, Capture) else "srt" if isinstance(room, SrtRelay) else
                  "rtmp" if isinstance(room, RtmpRelay) else "program")
        progress = room.progress
        values = progress.sample() if progress else None
        return dict(source=source, metrics=values)

    def get(self, uid, opens, token):
        with self.lock:
            room = self.active.get((uid, opens))
            return room if isinstance(room, Capture) and room.token == token else None

    def close_srt(self, uid, opens):
        with self.lock:
            room = self.active.get((uid, opens))
            if isinstance(room, SrtRelay):
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
