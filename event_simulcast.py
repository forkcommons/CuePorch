"""Operator-configured RTMP destinations for the connected live program."""

from base64 import urlsafe_b64encode
from contextlib import suppress
from datetime import datetime, timezone
from hmac import digest
import json
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
import facebook_live
import facebook_live_job
import linkedin_live
import linkedin_live_job
import live_destination_series
import facebook_upload
import youtube_live


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_simulcast_destination(
 id TEXT PRIMARY KEY, event TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE,
 server TEXT NOT NULL, secret BLOB NOT NULL, enabled INTEGER NOT NULL, opens INTEGER,
 provider TEXT, revision TEXT, remote TEXT, stream TEXT, preset TEXT,
 UNIQUE(event,name));
CREATE TABLE IF NOT EXISTS live_simulcast_key_series(
 destination TEXT PRIMARY KEY REFERENCES live_simulcast_destination(id) ON DELETE CASCADE,
 start_opens INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS live_simulcast_key_skip(
 destination TEXT NOT NULL REFERENCES live_simulcast_destination(id) ON DELETE CASCADE,
 opens INTEGER NOT NULL,
 PRIMARY KEY(destination,opens));
CREATE TABLE IF NOT EXISTS youtube_live_creation(
 event TEXT NOT NULL, opens INTEGER NOT NULL, event_revision TEXT NOT NULL,
 revision TEXT NOT NULL, channel TEXT NOT NULL, privacy TEXT NOT NULL, marker TEXT NOT NULL,
 phase TEXT NOT NULL, broadcast TEXT, stream TEXT, destination TEXT,
 PRIMARY KEY(event,opens));
""" + facebook_live_job.SCHEMA + linkedin_live_job.SCHEMA + live_destination_series.SCHEMA


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
    facebook_live_job.migrate(con)


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
    if preset in ("instagram", "tiktok", "x", "vimeo-ott"):
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
                            or ("provider" in fields and row["provider"] in ("linkedin-live", "linkedin-member-live")))
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
             facebook_live_connection, facebook_profile_live_connection, linkedin_live_connection,
             linkedin_member_live_connection,
             creation_access):
    linkedin_connections = {item.key: item for item in (linkedin_live_connection, linkedin_member_live_connection)}
    live_connections = {item.key: item for item in (
        youtube_live_connection, facebook_live_connection, facebook_profile_live_connection)} | linkedin_connections
    linkedin_lease_dir = outputs.directory / "linkedin-schedule"

    def linkedin_lease(key):
        linkedin_lease_dir.mkdir(parents=True, exist_ok=True)
        name = digest(b"CuePorch LinkedIn schedule", ":".join(map(str, key)).encode(), "sha256").hex()
        return linkedin_lease_dir / (name + ".lock")

    def linked_owner(connection, revision):
        if connection is linkedin_member_live_connection:
            profile = connection.authorized_profile(revision)
            return "member:" + profile["id"], profile["urn"]
        company = connection.authorized_company(revision)
        return company["id"], company["urn"]

    def linkedin_eligible(con, row, creation):
        if not creation or creation["state"] != "scheduled":
            return False
        saved = con.execute("SELECT enabled FROM live_simulcast_destination WHERE id=?",
                            (row["destination"],)).fetchone()
        connection = linkedin_connections[linkedin_live_job.provider_for(row["company"])]
        current = connection.connection(con)
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

        remember_series = payload.get("series", False) if isinstance(payload, dict) else False
        if not isinstance(remember_series, bool):
            abort(422, "Choose whether to remember this YouTube destination.")

        return prepare_youtube(uid, opens, privacy, remember_series=remember_series)

    def prepare_youtube(uid, opens, privacy, *, series=None, remember_series=False):
        connection = youtube_live_connection

        def access(con):
            if series is None:
                enforce_sharing(con, feature="outbound")
            elif not live_destination_series.enabled(con, series, opens):
                abort(409)
            event_revision, title, slot = creation_access(con, uid, opens)
            saved = connection.connection(con)
            if not saved:
                abort(409, "Connect YouTube Live before creating a broadcast.")
            if series is not None:
                if (event_revision, saved["revision"]) != (series["event_revision"], series["revision"]):
                    abort(409)
            return event_revision, title[:100].strip(), slot, saved["revision"]

        with tx() as con:
            event_revision, title, slot, revision = access(con)
            if remember_series:
                live_destination_series.ensure_recurring(con, uid)
        lease_dir = outputs.directory / "youtube-creation"
        lease_dir.mkdir(parents=True, exist_ok=True)
        lease_name = digest(b"CuePorch YouTube creation", f"{uid}:{opens}".encode(), "sha256").hex()

        def check(con):
            live_revision, _, _, live_connection = access(con)
            if live_revision != event_revision or live_connection != revision:
                abort(409, "The event or YouTube connection changed. Reload destinations.")

        def before_send():
            if series is None:
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
                if series is not None and channels["items"][0]["id"] != series["owner"]:
                    abort(409, "The authorized YouTube channel changed.")
                marker = f"Created by CuePorch ({uuid.uuid4().hex})"
                with tx() as con:
                    check(con)
                    check_provider_capacity(con, uid, opens)
                    con.execute("INSERT INTO youtube_live_creation VALUES(?,?,?,?,?,?,?,'new',NULL,NULL,NULL)",
                                (uid, opens, event_revision, revision, channels["items"][0]["id"], privacy, marker))
                    job = con.execute("SELECT * FROM youtube_live_creation WHERE event=? AND opens=?",
                                      (uid, opens)).fetchone()
            if series is not None and (job["channel"], job["revision"], job["event_revision"]) != (series["owner"], series["revision"], series["event_revision"]):
                abort(409)
            if (job["revision"], job["event_revision"], job["privacy"]) != (revision, event_revision, privacy):
                abort(409, "Use the original settings to check this broadcast, or select an existing broadcast.")
            if job["destination"]:
                with tx() as con:
                    check(con)
                    if not con.execute("SELECT 1 FROM live_simulcast_destination WHERE id=?", (job["destination"],)).fetchone():
                        abort(409, "This broadcast was removed. Use the existing broadcasts list to add it again.")
                    if remember_series:
                        live_destination_series.save(con, uid, 'youtube-live', job['channel'], revision, event_revision, opens, {'privacy': privacy})
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
                if remember_series:
                    live_destination_series.save(con, uid, 'youtube-live', job['channel'], revision, event_revision, opens, {'privacy': privacy})
                advance(con, "complete", destination=destination)
            return private(dict(state="complete", id=destination), 201)

        try:
            with FileLock(lease_dir / (lease_name + ".lock"), timeout=0):
                return provision()
        except Timeout:
            return private(dict(state="pending"), 202)

    def prepare_destination_series(row, opens):
        connection = linkedin_connections.get(row["provider"])
        if connection:
            with tx() as con:
                existing = con.execute(linkedin_live_job.READ, (row["event"], opens, row["owner"])).fetchone()
            if existing and (existing["state"] == "scheduled" or (existing["event_revision"], existing["revision"]) != (row["event_revision"], row["revision"])):
                return
            selected = row["owner"].removeprefix("member:") if row["provider"] == "linkedin-member-live" else row["owner"]
            auto_prepare = json.loads(row["options"])["auto_prepare"]
            schedule_linkedin(row["event"], opens, connection, selected, auto_prepare, series=row)
            return
        if row["provider"] != "youtube-live":
            return
        with tx() as con:
            exists = con.execute(
                "SELECT 1 FROM live_simulcast_destination WHERE event=? AND opens=? AND provider='youtube-live'",
                (row["event"], opens),
            ).fetchone()
            if exists:
                return
        prepare_youtube(row["event"], opens, json.loads(row["options"])["privacy"], series=row)

    app.extensions["stop_recurring_simulcast"] = live_destination_series.start(app, tx, prepare_destination_series)

    @app.route("/api/live-events/<uid>/<int:opens>/destinations/youtube/series", methods=["GET", "DELETE", "PUT"])
    @app.route("/api/live-events/<uid>/<int:opens>/destinations/linkedin/series", methods=["GET", "DELETE", "PUT"], defaults={"provider": "linkedin-live"})
    @app.route("/api/live-events/<uid>/<int:opens>/destinations/linkedin-member/series", methods=["GET", "DELETE", "PUT"], defaults={"provider": "linkedin-member-live"})
    def provider_series(uid, opens, provider="youtube-live"):
        staff()
        with tx() as con:
            enforce_sharing(con, feature="outbound")
            event_revision, _, _ = creation_access(con, uid, opens)
            connection = live_connections[provider]
            grant = connection.connection(con)
            series = con.execute("""SELECT * FROM live_destination_series
                WHERE event=? AND provider=? AND revision=? AND event_revision=?""",
                (uid, provider, grant["revision"] if grant else None, event_revision)).fetchone()
            inherited = bool(series and opens > series["after_opens"])

            def can_restore():
                if opens <= datetime.now(timezone.utc).timestamp():
                    return False
                if con.execute("SELECT 1 FROM live_simulcast_destination WHERE event=? AND opens=? AND provider=? AND revision=?",
                               (uid, opens, provider, series["revision"])).fetchone():
                    return False
                if provider == "youtube-live":
                    if con.execute("SELECT 1 FROM youtube_live_creation WHERE event=? AND opens=?",
                                   (uid, opens)).fetchone():
                        return False
                elif provider in linkedin_connections:
                    if con.execute(linkedin_live_job.READ, (uid, opens, series["owner"])).fetchone():
                        return False
                return True

            if request.method == "GET":
                skipped = inherited and not live_destination_series.enabled(con, series, opens)
                result = dict(active=series is not None, inherited=inherited, skipped=skipped)
                if skipped:
                    result["restorable"] = can_restore()
                return private(result)
            if request.method == "PUT":
                skipped = inherited and not live_destination_series.enabled(con, series, opens)
                if skipped:
                    if not can_restore():
                        abort(409, "This date can no longer be restored.")
                    con.execute("DELETE FROM live_destination_series_skip WHERE event=? AND provider=? AND owner=? AND opens=?",
                                (uid, series["provider"], series["owner"], opens))
                return private(dict(ok=True))
            payload = request.get_json()
            if not isinstance(payload, dict) or type(payload.get("date")) is not bool:
                abort(422, "Choose whether to skip this date or stop future scheduling.")
            if not series:
                return private(dict(ok=True))
            series_key = (uid, series["provider"], series["owner"])
            if not payload["date"]:
                con.execute("DELETE FROM live_destination_series WHERE event=? AND provider=? AND owner=?", series_key)
            elif inherited:
                con.execute("INSERT OR IGNORE INTO live_destination_series_skip VALUES(?,?,?,?)", (*series_key, opens))
                con.execute("""UPDATE live_simulcast_destination SET enabled=0
                    WHERE event=? AND opens=? AND provider=? AND revision=?""",
                    (uid, opens, series["provider"], series["revision"]))
                if provider in linkedin_connections:
                    linkedin_live_job.set_auto(con, (uid, opens, series["owner"]), False)
        return private(dict(ok=True))

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
        series = data.get("series", False)
        if type(series) is not bool:
            abort(422, "Choose whether to remember the YouTube Live series.")
        broadcast, channel = data.get("broadcast"), data.get("channel")
        if series:
            channels = youtube_live.channels(youtube_live_connection, revision)
            if channels["next"] or [item["id"] for item in channels["items"]] != [channel]:
                abort(409, "Connect a single YouTube channel before setting up future broadcasts.")
        bound = youtube_live.bound_target(youtube_live_connection, revision, broadcast, channel)
        staff()
        with tx() as con:
            enforce_sharing(con, feature="outbound")
            if not event_access(con, uid, opens):
                abort(409, "This occurrence no longer allows simulcasting.")
            latest = youtube_live_connection.connection(con)
            if not latest or latest["revision"] != revision:
                abort(409, "The YouTube Live connection changed. Reload destinations.")
            if series:
                live_destination_series.ensure_recurring(con, uid)
                event_revision, _, _ = creation_access(con, uid, opens)
                if bound["privacy"] not in youtube_live.PRIVACY:
                    abort(502, "YouTube did not confirm broadcast privacy.")
                live_destination_series.save(
                    con, uid, "youtube-live", channel, revision, event_revision, opens,
                    {"privacy": bound["privacy"]},
                )
            uid_destination = save_youtube_destination(con, uid, opens, revision, broadcast, bound)
        return private(dict(id=uid_destination), 201)

    def facebook_page(profile=False):
        payload = request.get_json()
        page = payload.get("page") if isinstance(payload, dict) else None
        if not isinstance(page, str) or not facebook_upload.ID.fullmatch(page):
            abort(422, "Choose a Facebook destination.")
        return "profile:" + page if profile else page

    def facebook_page_arg(profile=False):
        page = request.args.get("page", "")
        if not facebook_upload.ID.fullmatch(page):
            abort(422, "Choose a Facebook destination.")
        return "profile:" + page if profile else page

    def facebook_provider(page):
        if page.startswith("profile:"):
            return (facebook_profile_live_connection, page[8:],
                    facebook_profile_live_connection.create_profile_live)
        return facebook_live_connection, page, facebook_live_connection.create_page_live

    def facebook_lease(key):
        lease_dir = outputs.directory / "facebook-creation"
        lease_dir.mkdir(parents=True, exist_ok=True)
        lock_name = digest(b"CuePorch Facebook creation", ":".join(map(str, key)).encode(), "sha256").hex()
        return FileLock(lease_dir / (lock_name + ".lock"), timeout=0)

    def facebook_access(con, uid, opens, page):
        event_revision, title, slot = creation_access(con, uid, opens)
        if page.startswith("profile:"):
            saved = facebook_profile_live_connection.connection(con)
            if not saved:
                abort(409, "Connect Facebook before preparing a profile broadcast.")
            profile = facebook_profile_live_connection.tokens(saved)["profile"]
            if str(profile["id"]) != page[8:]:
                abort(403, "That Facebook profile is not connected.")
            page_name = profile["name"]
        else:
            saved = facebook_live_connection.connection(con)
            if not saved:
                abort(409, "Connect Facebook Live before preparing a broadcast.")
            pages = facebook_live_connection.tokens(saved)["pages"]
            if page not in pages:
                abort(403, "That Facebook Live Page is not connected.")
            page_name = pages[page]["name"]
        return event_revision, title, slot, saved["revision"], page_name

    @app.post("/api/live-events/<uid>/<int:opens>/destinations/facebook-profile", defaults={"profile": True})
    @app.post("/api/live-events/<uid>/<int:opens>/destinations/facebook")
    def create_facebook_destination(uid, opens, profile=False):
        staff()
        page = facebook_page(profile)
        privacy = request.get_json().get("privacy")
        if profile:
            privacy = facebook_live.normalize_profile_privacy(privacy)
        elif privacy is not None:
            abort(422, "Privacy is only supported for Facebook profiles.")
        result, status = prepare_facebook(uid, opens, page, privacy=privacy)
        return private(result, status)

    @app.get("/api/live-events/<uid>/<int:opens>/destinations/facebook-profile/auto", defaults={"profile": True})
    @app.get("/api/live-events/<uid>/<int:opens>/destinations/facebook/auto")
    def facebook_auto_status(uid, opens, profile=False):
        staff()
        page = facebook_page_arg(profile)
        with tx() as con:
            enforce_sharing(con, feature="outbound")
            event_revision, _, _, revision, _ = facebook_access(con, uid, opens, page)
            key = (uid, opens, page, revision)
            creation = con.execute("""SELECT state,revision FROM facebook_live_creation
                WHERE event=? AND opens=? AND page=?""", key[:3]).fetchone()
            automatic = con.execute("""SELECT state,revision,event_revision,privacy FROM facebook_live_auto
                WHERE event=? AND opens=? AND page=?""", key[:3]).fetchone()
            series = con.execute("""SELECT after_opens,privacy FROM facebook_live_series
                WHERE event=? AND page=? AND revision=? AND event_revision=?""",
                                 (uid, page, revision, event_revision)).fetchone()
        auto_current = automatic is not None and (automatic["revision"], automatic["event_revision"]) == (revision, event_revision)
        effective_auto = automatic is not None and (auto_current or automatic["state"] == "canceled")
        series_valid = series is not None and opens > series["after_opens"]
        if creation:
            state = "prepared" if creation["revision"] == revision and creation["state"] == "prepared" else "unconfirmed"
        elif effective_auto:
            state = {"queued": "queued", "attempted": "failed"}.get(automatic["state"], "idle") if auto_current else "idle"
        else:
            state = "queued" if series_valid else "idle"
        restorable = bool(
            not creation
            and series_valid
            and automatic is not None
            and automatic["state"] == "canceled"
        )
        result = dict(state=state, series=series is not None)
        if restorable:
            result["restorable"] = True
        if profile:
            if restorable:
                privacy = series["privacy"]
            elif effective_auto:
                privacy = automatic["privacy"]
            elif series_valid:
                privacy = series["privacy"]
            else:
                privacy = None
            if privacy is not None:
                result["privacy"] = privacy
        return private(result)

    @app.route("/api/live-events/<uid>/<int:opens>/destinations/facebook-profile/auto", methods=["PUT", "DELETE"], defaults={"profile": True})
    @app.route("/api/live-events/<uid>/<int:opens>/destinations/facebook/auto", methods=["PUT", "DELETE"])
    def configure_facebook_auto(uid, opens, profile=False):
        staff()
        key = (uid, opens, facebook_page(profile))
        series = request.get_json().get("series", False)
        if type(series) is not bool:
            abort(422, "Choose whether to apply automatic starts to future dates.")
        restore = False
        privacy = None
        if request.method == "PUT":
            privacy = request.get_json().get("privacy")
            if profile:
                privacy = facebook_live.normalize_profile_privacy(privacy)
            elif privacy is not None:
                abort(422, "Privacy is only supported for Facebook profiles.")
            restore = request.get_json().get("restore", False)
            if type(restore) is not bool:
                abort(422, "Invalid restore value.")
            if restore and (series or privacy is not None):
                abort(422, "Restore cannot be combined with series or privacy.")
        try:
            with facebook_lease(key), tx() as con:
                enforce_sharing(con, feature="outbound")
                if request.method == "DELETE" and series:
                    con.execute("DELETE FROM facebook_live_series WHERE event=? AND page=?", (uid, key[2]))
                    return private(dict(state="canceled"))
                if con.execute("SELECT 1 FROM facebook_live_creation WHERE event=? AND opens=? AND page=?", key).fetchone():
                    abort(409, "A Facebook broadcast already exists or has an unconfirmed result for this occurrence.")
                event_revision, _, _, revision, _ = facebook_access(con, *key)
                if request.method == "DELETE":
                    facebook_live_job.queue(con, key, revision, event_revision, state="canceled")
                    return private(dict(state="canceled"))
                if restore:
                    if not con.execute("SELECT 1 FROM facebook_live_series WHERE event=? AND page=? AND revision=? AND event_revision=? AND after_opens<?", (uid, key[2], revision, event_revision, opens)).fetchone():
                        abort(409, "No automatic starts to restore.")
                    con.execute("DELETE FROM facebook_live_auto WHERE event=? AND opens=? AND page=? AND state='canceled'", (uid, opens, key[2]))
                    if con.execute("SELECT 1 FROM facebook_live_auto WHERE event=? AND opens=? AND page=? AND revision=? AND event_revision=? AND state IN ('queued','attempted')", (uid, opens, key[2], revision, event_revision)).fetchone():
                        abort(409, "An explicit automatic start is already set for this occurrence.")
                    return private(dict(state="queued"), 202)
                check_provider_capacity(con, uid, opens)
                if series:
                    settings = con.execute("SELECT settings FROM live_event WHERE id=?", (uid,)).fetchone()["settings"]
                    if json.loads(settings)["schedule"]["repeat"] == "none":
                        abort(409, "Choose a recurring event to set up future broadcasts.")
                    con.execute("""INSERT INTO facebook_live_series (event,page,revision,event_revision,after_opens,privacy)
                        VALUES(?,?,?,?,?,?)
                        ON CONFLICT(event,page) DO UPDATE SET revision=excluded.revision,
                        event_revision=excluded.event_revision,after_opens=excluded.after_opens,
                        privacy=excluded.privacy""",
                                (uid, key[2], revision, event_revision, opens, privacy))
                facebook_live_job.queue(con, key, revision, event_revision, privacy=privacy)
        except Timeout:
            abort(409, "Another request is preparing this Facebook broadcast.")
        return private(dict(state="queued"), 202)

    def prepare_facebook(uid, opens, page, *, automatic=None, privacy=None):
        key = (uid, opens, page)
        live_now = automatic is not None
        with tx() as con:
            if not live_now:
                enforce_sharing(con, feature="outbound")
            event_revision, title, slot, revision, page_name = facebook_access(con, *key)
        if live_now and (event_revision, revision) != (automatic["event_revision"], automatic["revision"]):
            return {"state": "canceled"}, 200

        def check(con):
            if not live_now:
                enforce_sharing(con, feature="outbound")
            current, _, _, grant, _ = facebook_access(con, *key)
            if (current, grant) != (event_revision, revision):
                abort(409, "The event or Facebook connection changed. Reload destinations.")
            if live_now and not con.execute("""SELECT 1 FROM live_occurrence
                WHERE event=? AND opens=? AND delivery='ready'""", key[:2]).fetchone():
                abort(409, "The occurrence is no longer ready to stream.")
            if live_now and automatic["series"] and not con.execute("""SELECT 1 FROM facebook_live_series
                WHERE event=? AND page=? AND revision=? AND event_revision=? AND after_opens<? AND privacy IS ?""",
                (uid, page, revision, event_revision, opens, automatic["privacy"])).fetchone():
                abort(409, "Automatic starts for future dates are no longer enabled.")

        def before_send():
            if not live_now:
                staff()
            with tx() as con:
                check(con)
                check_provider_capacity(con, uid, opens)
                if not live_now and datetime.fromisoformat(slot["start"]) <= datetime.now(timezone.utc):
                    abort(409, "Choose a future occurrence to prepare a Facebook Live broadcast.")

        def save(con, target):
            check_provider_capacity(con, uid, opens)
            bound = target | {"title": page_name.strip() or "Facebook Live"}
            return save_provider_destination(con, uid, opens, facebook_provider(page)[0].key, revision, target["id"], bound)

        try:
            with facebook_lease(key):
                if not live_now:
                    with tx() as con:
                        check(con)
                        exists = con.execute(
                            "SELECT 1 FROM facebook_live_creation WHERE event=? AND opens=? AND page=?",
                            key,
                        ).fetchone()
                        if not exists:
                            facebook_live_job.queue(con, key, revision, event_revision, state="canceled", privacy=privacy)
                _, provider_page, create_live = facebook_provider(page)
                options = {"privacy": automatic["privacy"] if live_now else privacy} if page.startswith("profile:") else {}
                return facebook_live_job.provision(
                    tx, check, lambda: create_live(
                        revision, provider_page, title, opens, before_send, live_now=live_now,
                        **options),
                    save, *key, revision,
                    claim_auto=(lambda con: facebook_live_job.claim(con, key, automatic)) if live_now else None,
                )
        except Timeout:
            return {"state": "unconfirmed"}, 202

    app.extensions["stop_facebook_live"] = facebook_live_job.start(app, tx, prepare_facebook)

    @app.post("/api/live-events/<uid>/<int:opens>/destinations/linkedin/schedule")
    @app.post("/api/live-events/<uid>/<int:opens>/destinations/linkedin-member/schedule",
              defaults={"profile": True})
    def schedule_linkedin_live(uid, opens, profile=False):
        staff()
        payload = request.get_json()
        connection = linkedin_member_live_connection if profile else linkedin_live_connection
        if not connection.configured:
            abort(409, "Configure LinkedIn Live before scheduling a broadcast.")
        if not isinstance(payload, dict):
            abort(422, "Choose a connected LinkedIn Live account.")
        if not profile and payload.get("company") != connection.company:
            abort(403, "Choose the connected LinkedIn Live Company Page.")
        auto_prepare = payload.get("auto_prepare")
        if auto_prepare is not None and type(auto_prepare) is not bool:
            abort(422, "Choose whether to prepare LinkedIn delivery automatically.")
        selected = payload.get("profile") if profile else connection.company
        if not isinstance(selected, str):
            abort(422, "Choose a connected LinkedIn Live account.")
        remember_series = payload.get("series", False)
        if type(remember_series) is not bool:
            abort(422, "Choose whether to remember the LinkedIn Live series.")
        return schedule_linkedin(
            uid, opens, connection, selected, auto_prepare,
            remember_series=remember_series,
        )

    def schedule_linkedin(uid, opens, connection, selected, auto_prepare, *, series=None, remember_series=False):

        def access(con):
            if series is None:
                enforce_sharing(con, feature="outbound")
            elif not live_destination_series.enabled(con, series, opens):
                abort(409, "The LinkedIn Live series is not enabled.")
            event_revision, title, slot = creation_access(con, uid, opens)
            saved = connection.connection(con)
            if not saved:
                abort(409, "Connect LinkedIn Live before scheduling a broadcast.")
            if series is not None and (
                series["revision"] != saved["revision"]
                or series["event_revision"] != event_revision
            ):
                abort(409, "The event or LinkedIn connection changed. Reload destinations.")
            starts = datetime.fromisoformat(slot["start"])
            if starts <= datetime.now(timezone.utc):
                abort(409, "Choose a future occurrence for the LinkedIn Live announcement.")
            return event_revision, saved["revision"], title.strip()[:75], int(starts.timestamp()) * 1000

        with tx() as con:
            event_revision, revision, title, start_ms = access(con)
            if remember_series:
                live_destination_series.ensure_recurring(con, uid)

        owner_key, owner = linked_owner(connection, revision)
        expected_key = "member:" + selected if connection is linkedin_member_live_connection else selected
        if owner_key != expected_key:
            abort(403, "Choose the connected LinkedIn Live account.")

        def check(con):
            if access(con) != (event_revision, revision, title, start_ms):
                abort(409, "The event or LinkedIn connection changed. Reload destinations.")

        def before_send():
            if series is None:
                staff()
            with tx() as con:
                check(con)

        try:
            with FileLock(linkedin_lease((uid, opens, owner_key)), timeout=0):
                result, status = linkedin_live_job.provision(
                    tx, check,
                    lambda: connection.create_scheduled(revision, owner, title, start_ms, before_send),
                    lambda video: connection.announce_scheduled(revision, owner, video, title, before_send),
                    uid, opens, owner_key, event_revision, revision, auto_prepare,
                )
                if remember_series and result["state"] == "scheduled":
                    with tx() as con:
                        check(con)
                        live_destination_series.save(
                            con, uid, connection.key, owner_key, revision,
                            event_revision, opens,
                            {"auto_prepare": auto_prepare is not False},
                        )
        except Timeout:
            return private(dict(state="pending"), 202)
        return private(result, status)

    @app.route("/api/live-events/<uid>/<int:opens>/destinations/linkedin/existing", methods=["GET", "POST"])
    @app.route("/api/live-events/<uid>/<int:opens>/destinations/linkedin-member/existing",
               methods=["GET", "POST"], defaults={"profile": True})
    def existing_linkedin_event(uid, opens, profile=False):
        staff()
        connection = linkedin_member_live_connection if profile else linkedin_live_connection

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
        owner_key, owner = linked_owner(connection, revision)
        key = (uid, opens, owner_key)
        if request.method in ("GET", "HEAD"):
            start = linkedin_live.page_start(request.args.get("start", "0"))
            return private(connection.scheduled_events(revision, owner, starts_ms, start))

        body = request.get_json()
        video = body.get("id") if isinstance(body, dict) else None
        if not isinstance(video, str) or not linkedin_live.LIVE_VIDEO_ID.fullmatch(video):
            abort(422, "Choose an existing LinkedIn Live event.")
        start = linkedin_live.page_start(body.get("start", 0))
        auto_prepare = body.get("auto_prepare")
        if auto_prepare is not None and type(auto_prepare) is not bool:
            abort(422, "Choose whether to prepare LinkedIn delivery automatically.")
        series = body.get("series", False)
        if type(series) is not bool:
            abort(422, "Choose whether the LinkedIn Live event recurs.")

        try:
            with FileLock(linkedin_lease(key), timeout=0):
                with tx() as con:
                    if access(con) != scope:
                        abort(409, "The event or LinkedIn Live connection changed.")
                    if series:
                        live_destination_series.ensure_recurring(con, uid)
                    prior = con.execute(linkedin_live_job.READ, key).fetchone()
                    if prior:
                        if (prior["state"], prior["video"], prior["event_revision"], prior["revision"]) == (
                                "scheduled", video, event_revision, revision):
                            if auto_prepare is not None:
                                linkedin_live_job.set_auto(con, key, auto_prepare)
                            enabled = bool(con.execute(linkedin_live_job.AUTO, key).fetchone())
                            if series:
                                live_destination_series.save(
                                    con, uid, connection.key, owner_key, revision, event_revision, opens,
                                    {"auto_prepare": enabled})
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
                    if series:
                        live_destination_series.save(
                            con, uid, connection.key, owner_key, revision, event_revision, opens,
                            {"auto_prepare": auto_prepare is not False})
        except Timeout:
            abort(409, "Another request is changing this LinkedIn Live occurrence.")
        return private(dict(state="scheduled", post_urn=selected["post_urn"], auto_prepare=auto_prepare is not False), 201)

    @app.post("/api/live-events/<uid>/<int:opens>/destinations/linkedin/prepare")
    @app.post("/api/live-events/<uid>/<int:opens>/destinations/linkedin-member/prepare",
              defaults={"profile": True})
    def prepare_linkedin_live(uid, opens, profile=False):
        staff()
        owner_key = None
        if profile:
            with tx() as con:
                enforce_sharing(con, feature="outbound")
            owner_key, _ = linked_owner(linkedin_member_live_connection, linkedin_member_live_connection.revision())
        result, status = prepare_linkedin(uid, opens, interactive=True, owner_key=owner_key)
        return private(result, status)

    def prepare_linkedin(uid, opens, *, interactive, owner_key=None):
        owner_key = owner_key if owner_key is not None else linkedin_live_connection.company
        connection = linkedin_connections[linkedin_live_job.provider_for(owner_key)]
        if connection is linkedin_live_connection and connection.company != owner_key:
            abort(409, "The configured LinkedIn Live Company Page changed.")
        key = (uid, opens, owner_key)

        def access(con):
            if interactive:
                enforce_sharing(con, feature="outbound")
            elif con.execute("""SELECT 1 FROM live_destination_series_skip
                    WHERE event=? AND provider=? AND owner=? AND opens=?""",
                    (uid, connection.key, owner_key, opens)).fetchone():
                abort(409, "This occurrence was skipped.")
            revision, _, slot = creation_access(con, uid, opens)
            creation = con.execute(linkedin_live_job.READ, key).fetchone()
            saved = connection.connection(con)
            if not saved or not creation or creation["state"] != "scheduled":
                abort(409, "Schedule and announce this occurrence using the connected LinkedIn Live account.")
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
            return save_provider_destination(con, uid, opens, connection.key, record["revision"],
                                             record["video"], target | {"title": connection.config.name})

        try:
            with FileLock(linkedin_lease(key), timeout=0):
                actual_owner_key, owner = linked_owner(connection, record["revision"])
                if actual_owner_key != owner_key:
                    abort(409, "The authorized LinkedIn Live account changed.")
                if not interactive:
                    with tx() as con:
                        check(con)
                        if not linkedin_live_job.claim_auto(con, key):
                            return {"state": "pending"}, 202
                return linkedin_live_job.prepare_ingest(
                    tx, check, lambda: connection.register_ingest(record["revision"], owner, before_send),
                    save, key, record["revision"],
                )
        except Timeout:
            return {"state": "pending"}, 202

    linkedin_delivery = linkedin_live_job.Broadcasts(
        app, tx, linkedin_connections, outputs, linkedin_lease, linkedin_eligible, prepare_linkedin,
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
                reusable = body.get("reusable", False)
                if not isinstance(reusable, bool):
                    abort(422, "reusable must be a boolean.")
                if reusable:
                    event_row = con.execute(
                        "SELECT settings FROM live_event WHERE id=?",
                        (uid,),
                    ).fetchone()
                    settings = json.loads(event_row["settings"] or "{}") if event_row else {}
                    schedule = settings.get("schedule") or {}
                    if scope is None or schedule.get("repeat", "none") == "none":
                        abort(422, "Reusable keys require a named preset on a repeating event.")
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
                destination_id = uuid.uuid4().hex
                preset = body.get("preset") if scope is not None else None
                con.execute(
                    "INSERT INTO live_simulcast_destination(id,event,name,server,secret,enabled,opens,preset)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (destination_id, uid, name, server, outputs.cipher.encrypt(key.encode()),
                     True if reusable else enabled, None if reusable else scope, preset),
                )
                if reusable:
                    con.execute(
                        "INSERT INTO live_simulcast_key_series(destination,start_opens) VALUES(?,?)",
                        (destination_id, opens),
                    )
                    if not enabled:
                        con.execute(
                            "INSERT INTO live_simulcast_key_skip(destination,opens) VALUES(?,?)",
                            (destination_id, opens),
                        )
            rows = con.execute(
                """SELECT d.id,d.name,d.server,(d.enabled AND k.destination IS NULL) AS enabled,
                   s.destination IS NOT NULL AS reusable,d.opens,d.provider,d.revision,d.preset,i.phase AS provider_phase,
                   d.enabled AS configured_enabled,k.destination IS NOT NULL AS skipped
                   FROM live_simulcast_destination d
                   LEFT JOIN linkedin_live_ingest i ON i.destination=d.id
                   LEFT JOIN live_simulcast_key_series s ON s.destination=d.id
                   LEFT JOIN live_simulcast_key_skip k ON k.destination=d.id AND k.opens=?
                   WHERE d.event=? AND (d.opens IS NULL OR d.opens=?) AND (s.destination IS NULL OR s.start_opens<=?) ORDER BY d.name""",
                (opens, uid, opens, opens),
            ).fetchall()
            revisions = {key: row["revision"] for key, provider in live_connections.items()
                         if (row := provider.connection(con))}
        destinations = []
        for row in rows:
            item = dict(row)
            item["reusable"] = bool(item["reusable"])
            item["configured_enabled"] = bool(item["configured_enabled"])
            item["skipped"] = bool(item["skipped"])
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
                "SELECT d.id,d.provider,d.revision,d.opens,s.destination IS NOT NULL AS reusable"
                " FROM live_simulcast_destination d LEFT JOIN live_simulcast_key_series s ON s.destination=d.id"
                " WHERE d.id=? AND d.event=? AND (d.opens IS NULL OR d.opens=?)"
                " AND (s.start_opens IS NULL OR s.start_opens<=?)",
                (destination, uid, opens, opens),
            ).fetchone()
            if not saved:
                abort(404)
            if request.method == "DELETE":
                con.execute("DELETE FROM live_simulcast_key_skip WHERE destination=?", (destination,))
                con.execute("DELETE FROM live_simulcast_key_series WHERE destination=?", (destination,))
                con.execute("DELETE FROM live_simulcast_destination WHERE id=?", (destination,))
            else:
                body = request.get_json()
                if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
                    abort(422, "Set whether this destination is enabled.")
                only_date = body.get("only_date", False)
                if type(only_date) is not bool:
                    abort(422, "Choose whether to change only this occurrence.")
                if only_date:
                    if saved["provider"] is not None or saved["opens"] is not None or saved["reusable"]:
                        abort(422, "Selected-date overrides require a custom destination shared across the event.")
                    live_destination_series.ensure_recurring(con, uid)
                if body["enabled"]:
                    enforce_sharing(con, feature="outbound")
                    if not available:
                        abort(409, "Destinations require a public event without audience registration.")
                    if saved["provider"] in linkedin_connections:
                        abort(409, "A disabled LinkedIn Live stream cannot be restarted in the same occurrence.")
                    if saved["provider"] is not None:
                        provider = live_connections.get(saved["provider"])
                        connection = provider.connection(con) if provider else None
                        if not connection or connection["revision"] != saved["revision"]:
                            abort(409, "Reconnect the provider to enable this destination.")
                if saved["reusable"] or only_date:
                    if body["enabled"]:
                        con.execute(
                            "DELETE FROM live_simulcast_key_skip WHERE destination=? AND opens=?",
                            (destination, opens),
                        )
                    else:
                        con.execute(
                            "INSERT OR IGNORE INTO live_simulcast_key_skip(destination,opens) VALUES(?,?)",
                            (destination, opens),
                        )
                else:
                    con.execute(
                        "UPDATE live_simulcast_destination SET enabled=? WHERE id=?",
                        (body["enabled"], destination),
                    )
        return "", 204
