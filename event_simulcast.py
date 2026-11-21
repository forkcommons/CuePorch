"""Operator-configured RTMP destinations for the connected live program."""

from base64 import urlsafe_b64encode
from contextlib import suppress
from datetime import datetime, timezone
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
from werkzeug.exceptions import HTTPException

from event_audio import MASTER_MAX, SEGMENT_MAX, released_fragments, released_variant, selected_variant
from event_capture import EncoderProgress
from event_media import LIVE_FILE, MANIFEST_URI, manifest_text, root_path
from browser_account_security import private
import event_peer
import facebook_live_job
import linkedin_live
import linkedin_live_job
import facebook_upload
import youtube_live


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_simulcast_destination(
 id TEXT PRIMARY KEY, event TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE,
 server TEXT NOT NULL, secret BLOB NOT NULL, enabled INTEGER NOT NULL, opens INTEGER,
 provider TEXT, revision TEXT, remote TEXT, stream TEXT, preset TEXT,
 UNIQUE(event,name));
CREATE TABLE IF NOT EXISTS youtube_live_creation(
 event TEXT NOT NULL, opens INTEGER NOT NULL, event_revision TEXT NOT NULL,
 revision TEXT NOT NULL, channel TEXT NOT NULL, privacy TEXT NOT NULL, marker TEXT NOT NULL,
 phase TEXT NOT NULL, broadcast TEXT, stream TEXT, destination TEXT,
 PRIMARY KEY(event,opens));
""" + facebook_live_job.SCHEMA + linkedin_live_job.SCHEMA


def migrate(con):
    columns = {row[1] for row in con.execute("PRAGMA table_info(live_simulcast_destination)")}
    for column, definition in (
        ("opens", "INTEGER"),
        ("provider", "TEXT"),
        ("revision", "TEXT"),
        ("remote", "TEXT"),
        ("stream", "TEXT"),
        ("preset", "TEXT"),
    ):
        if column not in columns:
            con.execute(f"ALTER TABLE live_simulcast_destination ADD COLUMN {column} {definition}")


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
    if preset == "patreon":
        if not server.startswith("rtmps://"):
            abort(422, "Patreon destinations require an RTMPS server URL. Enter an RTMPS server or choose the custom option.")
        return opens
    if preset == "linkedin":
        if not server.startswith("rtmps://"):
            abort(422, "LinkedIn Live destinations require an RTMPS server URL.")
        return opens
    if preset in ("instagram", "tiktok", "x"):
        return opens
    abort(422, "Choose a supported destination preset or use the custom option.")


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
    def __init__(self, server, key, audio_required, *, profile=None):
        self.pending = Queue(maxsize=48)
        self.stopping = Event()
        tls = []
        if server.startswith("rtmps://"):
            tls = ["-tls_verify", "1", "-verifyhost", urlsplit(server).hostname,
                   "-ca_file", os.environ.get("SSL_CERT_FILE") or requests.certs.where()]
        probe = ["-probesize", "1048576", "-analyzeduration", "5000000"] if audio_required else [
            "-probesize", "262144", "-analyzeduration", "1000000"]
        codec = ["-c", "copy"]
        if profile == "linkedin-live":
            codec = [
                "-vf", "fps=30,scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                       "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1",
                "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p",
                "-preset", "veryfast", "-tune", "zerolatency", "-bf", "0",
                "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
                "-b:v", "4500k", "-maxrate", "4500k", "-bufsize", "9000k",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
            ]
        self.encoder = EncoderProgress([
            "-fflags", "+genpts", *probe,
            "-protocol_whitelist", "pipe", "-f", "mpegts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0" if audio_required else "0:a:0?", *codec, "-rw_timeout", "6000000",
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
                fields = row.keys()
                linkedin = (("preset" in fields and row["preset"] == "linkedin")
                            or ("provider" in fields and row["provider"] == "linkedin-live"))
                if linkedin:
                    self.legs[uid] = Leg(row["server"], key, audio_required, profile="linkedin-live")
                else:
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


def register(app, tx, staff, event_access, outputs, sharing_access, enforce_sharing, youtube_live_connection,
             facebook_live_connection, linkedin_live_connection,
             creation_access):
    live_connections = {item.key: item for item in (
        youtube_live_connection, facebook_live_connection, linkedin_live_connection)}
    linkedin_lease_dir = outputs.directory / "linkedin-schedule"

    def linkedin_lease(key):
        linkedin_lease_dir.mkdir(parents=True, exist_ok=True)
        name = digest(b"CuePorch LinkedIn schedule", ":".join(map(str, key)).encode(), "sha256").hex()
        return linkedin_lease_dir / (name + ".lock")

    def linkedin_eligible(con, row, creation):
        if not creation or creation["state"] != "scheduled":
            return False
        saved = con.execute("SELECT enabled FROM live_simulcast_destination WHERE id=?",
                            (row["destination"],)).fetchone()
        current = linkedin_live_connection.connection(con)
        if not saved or not saved["enabled"] or not current or current["revision"] != row["revision"]:
            return False
        try:
            revision, _, _ = creation_access(con, row["event"], row["opens"])
        except HTTPException:
            return False
        return revision == creation["event_revision"]

    def _require_provider_connection():
        if not youtube_live_connection.revision():
            abort(409, "Connect YouTube Live before using YouTube Live destinations.")

    def _provider_name(con, uid, title, provider, broadcast):
        base = title.strip()[:80]
        names = {
            row["name"].casefold()
            for row in con.execute("SELECT name FROM live_simulcast_destination WHERE event=?", (uid,))
        }
        if base.casefold() not in names:
            return base
        suffix = "-" + digest(provider.encode(), broadcast.encode(), "sha256").hex()[:6]
        candidate = base[:80 - len(suffix)] + suffix
        if candidate.casefold() in names:
            abort(409, "Choose a different destination name.")
        return candidate

    def _used_provider_destination(con, remote, stream):
        if not remote and not stream:
            return False
        return con.execute(
            "SELECT 1 FROM live_simulcast_destination"
            " WHERE provider='youtube-live' AND (remote=? OR stream=?) LIMIT 1",
            (remote, stream),
        ).fetchone() is not None

    def check_provider_capacity(con, uid, opens):
        count = con.execute(
            "SELECT COUNT(*) FROM live_simulcast_destination WHERE event=? AND opens=? AND provider IS NOT NULL",
            (uid, opens),
        ).fetchone()[0]
        if count >= 10:
            abort(409, "An occurrence supports up to 10 connected destinations.")

    def save_youtube_destination(con, uid, opens, revision, broadcast, bound):
        check_provider_capacity(con, uid, opens)
        if _used_provider_destination(con, broadcast, bound["stream"]):
            abort(409, "This broadcast or its stream is already assigned to an occurrence.")
        return save_provider_destination(con, uid, opens, "youtube-live", revision, broadcast, bound, bound["stream"])

    def save_provider_destination(con, uid, opens, provider, revision, broadcast, bound, stream=None):
        name = _provider_name(con, uid, bound["title"], provider, broadcast)
        destination = uuid.uuid4().hex
        con.execute(
            "INSERT INTO live_simulcast_destination"
            "(id,event,name,server,secret,enabled,opens,provider,revision,remote,stream)"
            " VALUES(?,?,?,?,?,1,?,?,?,?,?)",
            (destination, uid, name, bound["server"], outputs.cipher.encrypt(bound["key"].encode()),
             opens, provider, revision, broadcast, stream),
        )
        return destination

    @app.post("/api/live-events/<uid>/<int:opens>/destinations/youtube/create")
    def create_youtube_destination(uid, opens):
        staff()
        payload = request.get_json()
        privacy = payload.get("privacy", "private") if isinstance(payload, dict) else None
        if not isinstance(privacy, str) or privacy not in youtube_live.PRIVACY:
            abort(422, "Choose YouTube broadcast privacy.")

        connection = youtube_live_connection

        def access(con):
            enforce_sharing(con, feature="outbound")
            event_revision, title, slot = creation_access(con, uid, opens)
            saved = connection.connection(con)
            if not saved:
                abort(409, "Connect YouTube Live before creating a broadcast.")
            return event_revision, title[:100].strip(), slot, saved["revision"]

        with tx() as con:
            event_revision, title, slot, revision = access(con)
        lease_dir = outputs.directory / "youtube-creation"
        lease_dir.mkdir(parents=True, exist_ok=True)
        lease_name = digest(b"CuePorch YouTube creation", f"{uid}:{opens}".encode(), "sha256").hex()

        def check(con):
            live_revision, _, _, live_connection = access(con)
            if live_revision != event_revision or live_connection != revision:
                abort(409, "The event or YouTube connection changed. Reload destinations.")

        def before_send():
            staff()
            with tx() as con:
                check(con)

        def advance(con, phase, *, broadcast=None, stream=None, destination=None):
            con.execute("""UPDATE youtube_live_creation SET phase=?, broadcast=COALESCE(?, broadcast),
                        stream=COALESCE(?, stream), destination=COALESCE(?, destination) WHERE event=? AND opens=?""",
                        (phase, broadcast, stream, destination, uid, opens))

        def provision():
            with tx() as con:
                check(con)
                job = con.execute("SELECT * FROM youtube_live_creation WHERE event=? AND opens=?",
                                  (uid, opens)).fetchone()
            if job is None:
                channels = youtube_live.channels(connection, revision)
                if len(channels["items"]) != 1 or channels["next"]:
                    abort(409, "Connect a single YouTube channel before creating a broadcast.")
                marker = f"Created by CuePorch ({uuid.uuid4().hex})"
                with tx() as con:
                    check(con)
                    check_provider_capacity(con, uid, opens)
                    con.execute("INSERT INTO youtube_live_creation VALUES(?,?,?,?,?,?,?,'new',NULL,NULL,NULL)",
                                (uid, opens, event_revision, revision, channels["items"][0]["id"], privacy, marker))
                    job = con.execute("SELECT * FROM youtube_live_creation WHERE event=? AND opens=?",
                                      (uid, opens)).fetchone()
            if (job["revision"], job["event_revision"], job["privacy"]) != (revision, event_revision, privacy):
                abort(409, "Use the original settings to check this broadcast, or select an existing broadcast.")
            if job["destination"]:
                with tx() as con:
                    check(con)
                    if not con.execute("SELECT 1 FROM live_simulcast_destination WHERE id=?", (job["destination"],)).fetchone():
                        abort(409, "This broadcast was removed. Use the existing broadcasts list to add it again.")
                return private(dict(state="complete", id=job["destination"]))

            phase, channel, marker = job["phase"], job["channel"], job["marker"]

            def inserted(field, collection, start, create):
                if job[field]:
                    return job[field]
                if phase != start:
                    return youtube_live.find_created(connection, revision, collection, channel, marker)
                if field == "broadcast" and datetime.fromisoformat(slot["start"]) <= datetime.now(timezone.utc):
                    abort(409, "Choose a future occurrence to schedule a new YouTube broadcast.")
                pending = field + "_pending"
                with tx() as con:
                    check(con)
                    advance(con, pending)
                try:
                    return create()["id"]
                except youtube_live.YouTubeWriteUnconfirmed:
                    return youtube_live.find_created(connection, revision, collection, channel, marker)
                except HTTPException as error:
                    if error.code is not None and 400 <= error.code < 500:
                        with tx() as con:
                            advance(con, start)
                    raise

            broadcast = inserted("broadcast", "liveBroadcasts", "new", lambda: youtube_live.insert_broadcast(
                connection, revision, channel, title, slot["start"], slot["end"], privacy, marker, before_send))
            if not broadcast:
                return private(dict(state="pending"), 202)
            if not job["broadcast"]:
                with tx() as con:
                    check(con)
                    advance(con, "broadcast_confirmed", broadcast=broadcast)
                phase = "broadcast_confirmed"

            stream = inserted("stream", "liveStreams", "broadcast_confirmed", lambda: youtube_live.insert_stream(
                connection, revision, channel, title, marker, before_send))
            if not stream:
                return private(dict(state="pending"), 202)
            if not job["stream"]:
                with tx() as con:
                    check(con)
                    advance(con, "stream_confirmed", stream=stream)

            _, _, details = youtube_live.broadcast_details(connection, revision, broadcast, channel)
            binding = details.get("boundStreamId")
            if binding and binding != stream:
                abort(409, "This YouTube broadcast is already bound to a different stream.")
            if not binding:
                try:
                    youtube_live.bind_created(connection, revision, broadcast, stream, channel, before_send)
                except youtube_live.YouTubeWriteUnconfirmed:
                    return private(dict(state="pending"), 202)
            bound = youtube_live.bound_target(connection, revision, broadcast, channel)
            if bound["stream"] != stream:
                abort(409, "The YouTube broadcast binding changed before it could be added.")
            with tx() as con:
                check(con)
                destination = save_youtube_destination(con, uid, opens, revision, broadcast, bound)
                advance(con, "complete", destination=destination)
            return private(dict(state="complete", id=destination), 201)

        try:
            with FileLock(lease_dir / (lease_name + ".lock"), timeout=0):
                return provision()
        except Timeout:
            return private(dict(state="pending"), 202)

    @app.route("/api/live-events/<uid>/<int:opens>/destinations/youtube", methods=["GET", "POST"])
    def youtube_destination(uid, opens):
        staff()
        with tx() as con:
            enforce_sharing(con, feature="outbound")
            if not event_access(con, uid, opens):
                abort(409, "Destinations require a public event without audience registration.")
        revision = youtube_live_connection.revision()
        if request.method == "GET":
            return private(youtube_live.broadcasts(
                youtube_live_connection, revision, request.args.get("cursor"),
            ))
        data = request.get_json()
        if not isinstance(data, dict):
            abort(422, "Choose a YouTube Live broadcast.")
        broadcast, channel = data.get("broadcast"), data.get("channel")
        bound = youtube_live.bound_target(youtube_live_connection, revision, broadcast, channel)
        staff()
        with tx() as con:
            enforce_sharing(con, feature="outbound")
            if not event_access(con, uid, opens):
                abort(409, "This occurrence no longer allows simulcasting.")
            latest = youtube_live_connection.connection(con)
            if not latest or latest["revision"] != revision:
                abort(409, "The YouTube Live connection changed. Reload destinations.")
            uid_destination = save_youtube_destination(con, uid, opens, revision, broadcast, bound)
        return private(dict(id=uid_destination), 201)

    @app.post("/api/live-events/<uid>/<int:opens>/destinations/facebook")
    def create_facebook_destination(uid, opens):
        staff()
        payload = request.get_json()
        page = payload.get("page") if isinstance(payload, dict) else None
        if not isinstance(page, str) or not facebook_upload.ID.fullmatch(page):
            abort(422, "Choose a Facebook Live Page.")
        connection = facebook_live_connection

        def access(con):
            enforce_sharing(con, feature="outbound")
            event_revision, title, slot = creation_access(con, uid, opens)
            saved = connection.connection(con)
            if not saved:
                abort(409, "Connect Facebook Live before preparing a broadcast.")
            pages = connection.tokens(saved)["pages"]
            if page not in pages:
                abort(403, "That Facebook Live Page is not connected.")
            return event_revision, title, slot, saved["revision"], pages[page]["name"]

        with tx() as con:
            event_revision, title, slot, revision, page_name = access(con)

        def check(con):
            current, _, _, grant, _ = access(con)
            if (current, grant) != (event_revision, revision):
                abort(409, "The event or Facebook connection changed. Reload destinations.")

        def before_send():
            staff()
            with tx() as con:
                check(con)
                check_provider_capacity(con, uid, opens)
                if datetime.fromisoformat(slot["start"]) <= datetime.now(timezone.utc):
                    abort(409, "Choose a future occurrence to prepare a Facebook Live broadcast.")

        def save(con, target):
            check_provider_capacity(con, uid, opens)
            bound = target | {"title": page_name.strip() or "Facebook Live"}
            return save_provider_destination(con, uid, opens, "facebook-live", revision, target["id"], bound)

        lease_dir = outputs.directory / "facebook-creation"
        lease_dir.mkdir(parents=True, exist_ok=True)
        lock_name = digest(b"CuePorch Facebook creation", f"{uid}:{opens}:{page}".encode(), "sha256").hex()
        try:
            with FileLock(lease_dir / (lock_name + ".lock"), timeout=0):
                result, status = facebook_live_job.provision(
                    tx, check, lambda: connection.create_page_live(revision, page, title, opens, before_send),
                    save, uid, opens, page, revision,
                )
        except Timeout:
            return private(dict(state="unconfirmed"), 202)
        return private(result, status)

    @app.post("/api/live-events/<uid>/<int:opens>/destinations/linkedin/schedule")
    def schedule_linkedin_live(uid, opens):
        staff()
        payload = request.get_json()
        company = payload.get("company") if isinstance(payload, dict) else None
        connection = linkedin_live_connection
        if not connection.configured:
            abort(409, "Configure LinkedIn Live before scheduling a broadcast.")
        if company != connection.company:
            abort(403, "Choose the connected LinkedIn Live Company Page.")
        auto_prepare = payload.get("auto_prepare")
        if auto_prepare is not None and type(auto_prepare) is not bool:
            abort(422, "Choose whether to prepare LinkedIn delivery automatically.")

        def access(con):
            enforce_sharing(con, feature="outbound")
            event_revision, title, slot = creation_access(con, uid, opens)
            saved = connection.connection(con)
            if not saved:
                abort(409, "Connect LinkedIn Live before scheduling a broadcast.")
            starts = datetime.fromisoformat(slot["start"])
            if starts <= datetime.now(timezone.utc):
                abort(409, "Choose a future occurrence for the LinkedIn Live announcement.")
            return event_revision, saved["revision"], title.strip()[:75], int(starts.timestamp()) * 1000

        with tx() as con:
            event_revision, revision, title, start_ms = access(con)

        def check(con):
            if access(con) != (event_revision, revision, title, start_ms):
                abort(409, "The event or LinkedIn connection changed. Reload destinations.")

        def before_send():
            staff()
            with tx() as con:
                check(con)

        try:
            with FileLock(linkedin_lease((uid, opens, company)), timeout=0):
                owner = connection.authorized_company(revision)["urn"]
                result, status = linkedin_live_job.provision(
                    tx, check,
                    lambda: connection.create_scheduled(revision, owner, title, start_ms, before_send),
                    lambda video: connection.announce_scheduled(revision, owner, video, title, before_send),
                    uid, opens, company, event_revision, revision, auto_prepare,
                )
        except Timeout:
            return private(dict(state="pending"), 202)
        return private(result, status)

    @app.route("/api/live-events/<uid>/<int:opens>/destinations/linkedin/existing", methods=["GET", "POST"])
    def existing_linkedin_event(uid, opens):
        staff()
        connection = linkedin_live_connection
        key = (uid, opens, connection.company)

        def access(con):
            enforce_sharing(con, feature="outbound")
            event_revision, _, slot = creation_access(con, uid, opens)
            starts = datetime.fromisoformat(slot["start"])
            saved = connection.connection(con)
            if not connection.configured or not saved:
                abort(409, "Connect LinkedIn Live before choosing an existing event.")
            if starts <= datetime.now(timezone.utc):
                abort(409, "Choose a future occurrence for an existing LinkedIn event.")
            return event_revision, saved["revision"], int(starts.timestamp()) * 1000

        with tx() as con:
            scope = access(con)
        event_revision, revision, starts_ms = scope
        if request.method in ("GET", "HEAD"):
            start = linkedin_live.page_start(request.args.get("start", "0"))
            owner = connection.authorized_company(revision)["urn"]
            return private(connection.scheduled_events(revision, owner, starts_ms, start))

        body = request.get_json()
        video = body.get("id") if isinstance(body, dict) else None
        if not isinstance(video, str) or not linkedin_live.LIVE_VIDEO_ID.fullmatch(video):
            abort(422, "Choose an existing LinkedIn Live event.")
        start = linkedin_live.page_start(body.get("start", 0))
        auto_prepare = body.get("auto_prepare")
        if auto_prepare is not None and type(auto_prepare) is not bool:
            abort(422, "Choose whether to prepare LinkedIn delivery automatically.")

        try:
            with FileLock(linkedin_lease(key), timeout=0):
                owner = connection.authorized_company(revision)["urn"]
                with tx() as con:
                    if access(con) != scope:
                        abort(409, "The event or LinkedIn Live connection changed.")
                    prior = con.execute(linkedin_live_job.READ, key).fetchone()
                    if prior:
                        if (prior["state"], prior["video"], prior["event_revision"], prior["revision"]) == (
                                "scheduled", video, event_revision, revision):
                            if auto_prepare is not None:
                                linkedin_live_job.set_auto(con, key, auto_prepare)
                            enabled = bool(con.execute(linkedin_live_job.AUTO, key).fetchone())
                            return private(dict(state="scheduled", post_urn=prior["post"], auto_prepare=enabled))
                        abort(409, "This occurrence already has a LinkedIn Live schedule.")
                page = connection.scheduled_events(revision, owner, starts_ms, start)
                selected = next((item for item in page["items"] if item["id"] == video), None)
                if selected is None:
                    abort(409, "Choose an available LinkedIn Live event at this occurrence's start time.")
                with tx() as con:
                    if access(con) != scope:
                        abort(409, "The event or LinkedIn Live connection changed.")
                    if con.execute("SELECT 1 FROM linkedin_live_creation WHERE video=?", (video,)).fetchone():
                        abort(409, "This LinkedIn Live event already belongs to an occurrence.")
                    con.execute("INSERT INTO linkedin_live_creation VALUES(?,?,?,?,?,'scheduled',?,?)",
                                (*key, event_revision, revision, video, selected["post_urn"]))
                    linkedin_live_job.set_auto(con, key, auto_prepare is not False)
        except Timeout:
            abort(409, "Another request is changing this LinkedIn Live occurrence.")
        return private(dict(state="scheduled", post_urn=selected["post_urn"], auto_prepare=auto_prepare is not False), 201)

    @app.post("/api/live-events/<uid>/<int:opens>/destinations/linkedin/prepare")
    def prepare_linkedin_live(uid, opens):
        staff()
        result, status = prepare_linkedin(uid, opens, interactive=True)
        return private(result, status)

    def prepare_linkedin(uid, opens, *, interactive):
        connection = linkedin_live_connection
        key = (uid, opens, connection.company)

        def access(con):
            if interactive:
                enforce_sharing(con, feature="outbound")
            revision, _, slot = creation_access(con, uid, opens)
            creation = con.execute(linkedin_live_job.READ, key).fetchone()
            saved = connection.connection(con)
            if not saved or not creation or creation["state"] != "scheduled":
                abort(409, "Schedule and announce this occurrence using the connected LinkedIn Live Page.")
            if (creation["event_revision"], creation["revision"]) != (revision, saved["revision"]):
                abort(409, "The scheduled event or LinkedIn Live connection changed.")
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(slot["start"])).total_seconds()
            if not -900 <= elapsed <= 7200:
                abort(409, "Prepare LinkedIn delivery from 15 minutes before to two hours after the scheduled start.")
            return creation

        with tx() as con:
            record = access(con)

        def check(con):
            current = access(con)
            if (current["video"], current["revision"]) != (record["video"], record["revision"]):
                abort(409, "The LinkedIn Live schedule changed.")

        def before_send():
            if interactive:
                staff()
            with tx() as con:
                check(con)
                check_provider_capacity(con, uid, opens)

        def save(con, target):
            check_provider_capacity(con, uid, opens)
            return save_provider_destination(con, uid, opens, "linkedin-live", record["revision"],
                                             record["video"], target | {"title": "LinkedIn Live"})

        try:
            with FileLock(linkedin_lease(key), timeout=0):
                if not interactive:
                    with tx() as con:
                        check(con)
                        if not linkedin_live_job.claim_auto(con, key):
                            return {"state": "pending"}, 202
                owner = connection.authorized_company(record["revision"])["urn"]
                return linkedin_live_job.prepare_ingest(
                    tx, check, lambda: connection.register_ingest(record["revision"], owner, before_send),
                    save, key, record["revision"],
                )
        except Timeout:
            return {"state": "pending"}, 202

    linkedin_delivery = linkedin_live_job.Broadcasts(
        app, tx, linkedin_live_connection, outputs, linkedin_lease, linkedin_eligible, prepare_linkedin,
    )
    app.extensions["stop_linkedin_live"] = linkedin_delivery.close

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
                custom_rows = con.execute(
                    "SELECT name FROM live_simulcast_destination WHERE event=? AND provider IS NULL",
                    (uid,),
                ).fetchall()
                if len(custom_rows) >= MAX_DESTINATIONS:
                    abort(409, "An event supports up to 20 custom destinations.")
                all_rows = con.execute(
                    "SELECT name FROM live_simulcast_destination WHERE event=?",
                    (uid,),
                ).fetchall()
                if any(row["name"].casefold() == name.casefold() for row in all_rows):
                    abort(409, "Choose a different destination name.")
                con.execute(
                    "INSERT INTO live_simulcast_destination(id,event,name,server,secret,enabled,opens,preset)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, uid, name, server, outputs.cipher.encrypt(key.encode()),
                     enabled, scope, body.get("preset") if scope is not None else None),
                )
            rows = con.execute(
                """SELECT d.id,d.name,d.server,d.enabled,d.opens,d.provider,d.revision,d.preset,i.phase AS provider_phase
                   FROM live_simulcast_destination d
                   LEFT JOIN linkedin_live_ingest i ON i.destination=d.id
                   WHERE d.event=? AND (d.opens IS NULL OR d.opens=?) ORDER BY d.name""",
                (uid, opens),
            ).fetchall()
            revisions = {key: row["revision"] for key, provider in live_connections.items()
                         if (row := provider.connection(con))}
        destinations = []
        for row in rows:
            item = dict(row)
            revision = item.pop("revision")
            if item["provider"] is not None and revision != revisions.get(item["provider"]):
                item["state"] = "disconnected"
            elif item["enabled"]:
                item["state"] = outputs.state(uid, opens, row["id"])
            else:
                item["state"] = "disabled"
            destinations.append(item)
        return dict(available=available, destinations=destinations)

    @app.route("/api/live-events/<uid>/<int:opens>/destinations/<destination>", methods=["PUT", "DELETE"])
    def event_destination(uid, opens, destination):
        staff()
        with tx() as con:
            available = event_access(con, uid, opens)
            saved = con.execute(
                "SELECT id,provider,revision FROM live_simulcast_destination"
                " WHERE id=? AND event=? AND (opens IS NULL OR opens=?)",
                (destination, uid, opens),
            ).fetchone()
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
                    if not available:
                        abort(409, "Destinations require a public event without audience registration.")
                    if saved["provider"] == "linkedin-live":
                        abort(409, "A disabled LinkedIn Live stream cannot be restarted in the same occurrence.")
                    if saved["provider"] is not None:
                        provider = live_connections.get(saved["provider"])
                        connection = provider.connection(con) if provider else None
                        if not connection or connection["revision"] != saved["revision"]:
                            abort(409, "Reconnect the provider to enable this destination.")
                con.execute(
                    "UPDATE live_simulcast_destination SET enabled=? WHERE id=?",
                    (body["enabled"], destination),
                )
        return "", 204
