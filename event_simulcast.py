"""Operator-configured RTMP destinations for the connected live program."""

from base64 import urlsafe_b64encode
from contextlib import suppress
from hmac import digest
import os
from queue import Empty, Full, Queue
import re
import subprocess
from threading import Event, Lock, Thread
from time import monotonic
from urllib.parse import urlsplit
import uuid

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock, Timeout
from flask import abort, request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests

from event_audio import MASTER_MAX, SEGMENT_MAX, released_fragments, released_variant, selected_variant
from event_capture import EncoderProgress
from event_media import LIVE_FILE, MANIFEST_URI, manifest_text, root_path
import event_peer


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_simulcast_destination(
 id TEXT PRIMARY KEY, event TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE,
 server TEXT NOT NULL, secret BLOB NOT NULL, enabled INTEGER NOT NULL, opens INTEGER,
 UNIQUE(event,name));
"""


def migrate(con):
    columns = {row[1] for row in con.execute("PRAGMA table_info(live_simulcast_destination)")}
    if "opens" not in columns:
        con.execute("ALTER TABLE live_simulcast_destination ADD COLUMN opens INTEGER")


MAX_DESTINATIONS = 20
RECONNECT_SECONDS = 6
TRANSPORT_ERRORS = (OSError, ValueError, KeyError, StopIteration, requests.RequestException)


def target(body):
    name, server, key = (body.get(field, "") for field in ("name", "server", "key"))
    enabled = body.get("enabled", True)
    if (any(not isinstance(value, str) for value in (name, server, key)) or type(enabled) is not bool
            or not 1 <= len(name.strip()) <= 80 or not 1 <= len(server) <= 500 or not 1 <= len(key) <= 512
            or not server.isprintable() or not key.isprintable() or any(character.isspace() for character in key)):
        abort(422, "Enter a destination name, server and stream key.")
    try:
        address = urlsplit(server)
        if (address.scheme not in ("rtmp", "rtmps") or not address.hostname or address.username is not None
                or address.query or address.fragment or any(char.isspace() for char in server)
                or not re.fullmatch(r"/[a-zA-Z0-9._/-]+", address.path)
                or (address.port is not None and not 1 <= address.port <= 65535)):
            raise ValueError
    except ValueError:
        abort(422, "Enter an RTMP or RTMPS server URL with its application path and a separate key.")
    return name.strip(), server.rstrip("/"), key, enabled


def destination_scope(body, server, opens):
    preset = body.get("preset", "custom")
    if preset == "custom":
        return None
    if preset != "patreon":
        abort(422, "Choose a supported destination preset or use the custom option.")
    if not server.startswith("rtmps://"):
        abort(422, "Patreon destinations require an RTMPS server URL. Enter an RTMPS server or choose the custom option.")
    return opens


def highest_variant(master):
    selected = selected_variant(master, audio=False)
    lines = master.splitlines()
    retained = []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            if index + 1 < len(lines) and lines[index + 1] == selected:
                retained.extend((line, selected))
        elif line.startswith("#"):
            retained.append(line)
    return "\n".join(retained) + "\n"


def common_start(video, audio, latest):
    """Choose video and audio fragments from the same point in host time."""
    positions = range(len(video) - 1)
    for video_index in (reversed(positions) if latest else positions):
        picture = video[video_index]
        audio_index, sound = min(enumerate(audio[:-1]), key=lambda item: abs(item[1].start - picture.start))
        if abs(sound.start - picture.start) < min(picture.duration, sound.duration) / 2:
            return video_index, audio_index
    raise ValueError("Waiting for released video and audio to share a start time.")


class ProgramStream:
    """Mux separate HLS audio and video from a private, occurrence-scoped loopback source."""

    def __init__(self, media, origin, remote, address, private, cutoff=None):
        directory, master = root_path(origin, address, remote, private)
        scope = "/" + uuid.uuid4().hex + "/"
        self.pending = Queue(maxsize=24)
        self.stopping = Event()
        released_parts = {}
        first_playlists = {}
        first_times = {}
        released_lock = Lock()

        def read(name, limit):
            with media.read(origin, directory + "/" + name, stream=True) as response:
                payload = response.raw.read(limit + 1)
            if len(payload) > limit:
                raise ValueError("The live resource is too large.")
            return payload

        initial_master = highest_variant(manifest_text(read(master, MASTER_MAX)))
        video = selected_variant(initial_master, audio=False)
        audio = [name for line in initial_master.splitlines() if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO,")
                 for name in MANIFEST_URI.findall(line)]
        inputs = (video, audio[0]) if audio else (video,)
        aligned = cutoff is not None
        if aligned or audio:
            if any(not LIVE_FILE.fullmatch(name) or not name.endswith(".m3u8") for name in inputs):
                raise ValueError("The program must use supported live playlists.")
            playlists = [manifest_text(read(name, MASTER_MAX)) for name in inputs]
            aligned = aligned or all("#EXT-X-PROGRAM-DATE-TIME:" in playlist for playlist in playlists)
        if aligned:
            lower_bound = cutoff if cutoff is not None else float("-inf")
            fragments = [released_fragments(playlist, lower_bound) for playlist in playlists]
            if any(len(parts) < 2 for _, parts in fragments):
                raise ValueError("Waiting for released video and audio to be ready together.")
            starts = common_start(*(parts for _, parts in fragments), latest=cutoff is None) if audio else (0,)
            for name, playlist, (sequence, parts), offset in zip(inputs, playlists, fragments, starts):
                selected = parts[offset:]
                first_playlists[name] = released_variant(playlist, sequence + offset, selected).encode()
                first_times[name] = selected[0].start
                released_parts[name] = {part.name for part in selected}
        else:
            inputs = (master,)
        self.audio_required = bool(aligned and audio)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                name = self.path.removeprefix(scope)
                if not self.path.startswith(scope) or not LIVE_FILE.fullmatch(name):
                    self.send_error(404)
                    return
                playlist = name.endswith(".m3u8")
                limit = MASTER_MAX if playlist else SEGMENT_MAX
                try:
                    payload = None
                    if aligned:
                        with released_lock:
                            if playlist and name not in released_parts:
                                raise ValueError("This playlist is outside the released program.")
                            if not playlist and not any(name in segments for segments in released_parts.values()):
                                raise ValueError("This segment is outside the released program.")
                            payload = first_playlists.pop(name, None)
                    if payload is None:
                        payload = read(name, limit)
                        if playlist:
                            content = manifest_text(payload)
                            if aligned:
                                sequence, fragments = released_fragments(content, first_times[name])
                                content = released_variant(content, sequence, fragments)
                                with released_lock:
                                    released_parts[name] = {fragment.name for fragment in fragments}
                            elif name == master:
                                content = highest_variant(content)
                            payload = content.encode()
                except TRANSPORT_ERRORS:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Type", "application/vnd.apple.mpegurl" if playlist else "video/mp2t")
                self.end_headers()
                with suppress(OSError):
                    self.wfile.write(payload)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        url = f"http://127.0.0.1:{self.server.server_port}{scope}"
        input_arguments = []
        for filename in inputs:
            input_arguments.extend(("-rw_timeout", "8000000", "-protocol_whitelist", "http,tcp",
                                    "-live_start_index", "0" if aligned else "-1", "-i", url + filename))
        timestamps = ["-copyts"] if aligned else []
        normalize = ["-avoid_negative_ts", "make_non_negative"] if aligned else []
        try:
            self.process = subprocess.Popen([
                "ffmpeg", "-nostdin", "-v", "error", *timestamps, *input_arguments,
                "-map", "0:v:0", "-map", "1:a:0" if self.audio_required else "0:a:0?", "-c", "copy", "-f", "mpegts",
                *normalize,
                "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-flush_packets", "1", "pipe:1",
            ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            self.server.server_close()
            raise
        self.http_thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .2}, daemon=True)
        self.read_thread = Thread(target=self.collect, name="CuePorch simulcast program mux", daemon=True)
        self.http_thread.start()
        self.read_thread.start()

    def collect(self):
        with self.process.stdout as stream:
            while not self.stopping.is_set():
                payload = stream.read(188 * 180)
                if not payload:
                    break
                while not self.stopping.is_set():
                    try:
                        self.pending.put(payload, timeout=.25)
                        break
                    except Full:
                        continue

    def close(self):
        self.stopping.set()
        end_process(self.process)
        self.server.shutdown()
        self.server.server_close()
        self.read_thread.join(timeout=.5)
        self.http_thread.join(timeout=.5)


def end_process(process):
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            process.kill()
        process.wait()


class Leg:
    def __init__(self, server, key, audio_required):
        self.pending = Queue(maxsize=48)
        self.stopping = Event()
        tls = []
        if server.startswith("rtmps://"):
            tls = ["-tls_verify", "1", "-verifyhost", urlsplit(server).hostname,
                   "-ca_file", os.environ.get("SSL_CERT_FILE") or requests.certs.where()]
        probe = ["-probesize", "1048576", "-analyzeduration", "5000000"] if audio_required else [
            "-probesize", "262144", "-analyzeduration", "1000000"]
        self.encoder = EncoderProgress([
            "-fflags", "+genpts", *probe,
            "-protocol_whitelist", "pipe", "-f", "mpegts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0" if audio_required else "0:a:0?", "-c", "copy", "-rw_timeout", "6000000",
            *tls, "-rtmp_playpath", key, "-f", "flv", "-flush_packets", "1", server,
        ], subprocess.PIPE)
        self.thread = Thread(target=self.feed, name="CuePorch RTMP destination", daemon=True)
        self.thread.start()

    def feed(self):
        process = self.encoder.process
        try:
            with process.stdin as stream:
                while not self.stopping.is_set() and process.poll() is None:
                    try:
                        payload = self.pending.get(timeout=.25)
                    except Empty:
                        continue
                    stream.write(payload)
                    stream.flush()
        except OSError:
            pass

    def send(self, payload):
        try:
            self.pending.put_nowait(payload)
            return True
        except Full:
            return False

    def healthy(self):
        return self.encoder.process.poll() is None and self.thread.is_alive()

    def close(self):
        self.stopping.set()
        end_process(self.encoder.process)
        self.thread.join(timeout=.5)


class Program:
    def __init__(self, media, origin, remote, cipher, cutoff):
        self.media = media
        self.origin = origin
        self.remote = remote
        self.cipher = cipher
        self.cutoff = cutoff
        self.lock = Lock()
        self.rows = {}
        self.legs = {}
        self.next_connect = {}
        self.states = {}
        self.stopping = Event()
        self.thread = None

    def configure(self, rows):
        with self.lock:
            self.rows = {row["id"]: row for row in rows}

    def phase(self, uid):
        with self.lock:
            return self.states.get(uid, "waiting")

    def status(self, uid, state):
        with self.lock:
            self.states[uid] = state

    def sync(self, audio_required):
        with self.lock:
            rows = self.rows.copy()
        now = monotonic()
        for uid, leg in list(self.legs.items()):
            if uid not in rows or not leg.healthy():
                self.disconnect(uid, uid in rows)
        for uid, row in rows.items():
            if uid in self.legs or now < self.next_connect.get(uid, 0):
                continue
            try:
                key = self.cipher.decrypt(row["secret"]).decode("utf-8")
                self.legs[uid] = Leg(row["server"], key, audio_required)
                self.status(uid, "connecting")
            except (InvalidToken, OSError, ValueError):
                self.next_connect[uid] = now + RECONNECT_SECONDS
                self.status(uid, "retrying")

    def disconnect(self, uid, failed):
        self.legs.pop(uid).close()
        if failed:
            self.next_connect[uid] = monotonic() + RECONNECT_SECONDS
        else:
            self.next_connect.pop(uid, None)
        self.status(uid, "retrying" if failed else "waiting")

    def program(self):
        raw = self.media.metadata(self.origin, self.remote)
        state = event_peer.status(raw)
        if state["state"] != "live":
            return None
        master = next(entry["playlistUrl"] for entry in raw["streamingPlaylists"] if entry["type"] == 1)
        return ProgramStream(self.media, self.origin, self.remote, master, not state["public"], self.cutoff)

    def run(self, lease_path):
        try:
            with FileLock(lease_path, timeout=0):
                self.relay()
        except Timeout:
            pass
        finally:
            for uid in list(self.legs):
                self.disconnect(uid, False)

    def relay(self):
        source = None
        try:
            while not self.stopping.is_set():
                if source is None:
                    with suppress(*TRANSPORT_ERRORS):
                        source = self.program()
                    if source is None:
                        self.stopping.wait(1)
                        continue
                    last_chunk = monotonic()
                self.sync(source.audio_required)
                try:
                    payload = source.pending.get(timeout=.25)
                except Empty:
                    if source.process.poll() is not None or monotonic() - last_chunk > 25:
                        source.close()
                        source = None
                        for uid in list(self.legs):
                            self.disconnect(uid, False)
                    continue
                last_chunk = monotonic()
                for uid, leg in list(self.legs.items()):
                    if leg.send(payload):
                        if leg.encoder.sample():
                            self.status(uid, "sending")
                    else:
                        self.disconnect(uid, True)
        finally:
            if source is not None:
                source.close()


class Simulcast:
    def __init__(self, app, tx, media, database, signing_key, candidates, directory="event-simulcast"):
        self.app = app
        self.tx = tx
        self.media = media
        self.candidates = candidates
        self.cipher = Fernet(urlsafe_b64encode(digest(signing_key.encode(), b"CuePorch RTMP streams", "sha256")))
        self.directory = database.parent / directory
        self.jobs = {}
        self.stopping = Event()
        self.lock = Lock()
        self.thread = Thread(target=self.scan, name="CuePorch simulcast scheduler", daemon=True)
        self.thread.start()

    def scan(self):
        while not self.stopping.wait(1):
            try:
                with self.tx() as con:
                    candidates = self.candidates(con)
                if candidates:
                    self.directory.mkdir(exist_ok=True)
                with self.lock:
                    self.update(candidates)
            except Exception:
                self.app.logger.exception("CuePorch could not advance scheduled simulcasts")

    def update(self, candidates):
        self.jobs = {scope: job for scope, job in self.jobs.items() if job.thread.is_alive()}
        for scope, job in self.jobs.items():
            if scope not in candidates:
                job.stopping.set()
        for scope, (origin, remote, rows, cutoff) in candidates.items():
            if scope in self.jobs:
                self.jobs[scope].configure(rows)
                continue
            job = Program(self.media, origin, remote, self.cipher, cutoff)
            job.configure(rows)
            filename = self.directory / f"{scope[0]}-{scope[1]}.lock"
            job.thread = Thread(target=job.run, args=(filename,), name="CuePorch live simulcast", daemon=True)
            self.jobs[scope] = job
            job.thread.start()

    def state(self, uid, opens, destination):
        with self.lock:
            current = self.jobs.get((uid, opens))
            return current.phase(destination) if current and not current.stopping.is_set() else "waiting"

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=4)
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            job.stopping.set()
        for job in jobs:
            job.thread.join(timeout=15)


def register(app, tx, staff, event_access, outputs, sharing_access, enforce_sharing):
    @app.route("/api/live-events/<uid>/<int:opens>/destinations", methods=["GET", "POST"])
    def event_destinations(uid, opens):
        staff()
        with tx() as con:
            available = event_access(con, uid, opens)
            if not sharing_access(con)["outbound"] and request.method in ("GET", "HEAD"):
                return dict(available=False, destinations=[])
            if request.method == "POST":
                enforce_sharing(con, feature="outbound")
                if not available:
                    abort(409, "Destinations require a public event without audience registration.")
                body = request.get_json()
                if not isinstance(body, dict):
                    abort(422, "Enter a destination.")
                name, server, key, enabled = target(body)
                scope = destination_scope(body, server, opens)
                rows = con.execute("SELECT name FROM live_simulcast_destination WHERE event=?", (uid,)).fetchall()
                if len(rows) >= MAX_DESTINATIONS:
                    abort(409, "An event supports up to 20 custom destinations.")
                if any(row["name"].casefold() == name.casefold() for row in rows):
                    abort(409, "Choose a different destination name.")
                con.execute("INSERT INTO live_simulcast_destination(id,event,name,server,secret,enabled,opens)"
                            " VALUES(?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, uid, name, server, outputs.cipher.encrypt(key.encode()), enabled, scope))
            rows = con.execute(
                "SELECT id,name,server,enabled,opens FROM live_simulcast_destination"
                " WHERE event=? AND (opens IS NULL OR opens=?) ORDER BY name",
                (uid, opens)).fetchall()
        return dict(available=available, destinations=[
            dict(row) | {"state": outputs.state(uid, opens, row["id"]) if row["enabled"] else "disabled"} for row in rows
        ])

    @app.route("/api/live-events/<uid>/<int:opens>/destinations/<destination>", methods=["PUT", "DELETE"])
    def event_destination(uid, opens, destination):
        staff()
        with tx() as con:
            available = event_access(con, uid, opens)
            saved = con.execute(
                "SELECT id FROM live_simulcast_destination WHERE id=? AND event=? AND (opens IS NULL OR opens=?)",
                (destination, uid, opens)).fetchone()
            if not saved:
                abort(404)
            if request.method == "DELETE":
                con.execute("DELETE FROM live_simulcast_destination WHERE id=?", (destination,))
            else:
                body = request.get_json()
                if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
                    abort(422, "Set whether this destination is enabled.")
                if body["enabled"]:
                    enforce_sharing(con, feature="outbound")
                if body["enabled"] and not available:
                    abort(409, "Destinations require a public event without audience registration.")
                con.execute("UPDATE live_simulcast_destination SET enabled=? WHERE id=?", (body["enabled"], destination))
        return "", 204
