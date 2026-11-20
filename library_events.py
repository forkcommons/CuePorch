"""Institution event schedules and the operator's live production controls."""

from datetime import date, datetime, time, timedelta, timezone
from csv import Error as CSVError
from email.utils import parsedate_to_datetime
import hmac
import json
import math
import re
import secrets
import uuid

from filelock import FileLock, Timeout
from flask import abort, make_response, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
import requests
from werkzeug.security import check_password_hash, generate_password_hash

from event_media import Gateway, HLS_PREFIXES, LIVE_FILE, local_hls, root_path
from event_capture import BusyError, CHUNK_BYTES, Studio, ingest_address, srt_endpoint
import event_peer
import event_analytics
import event_chat
import event_captions
import event_engagement
import event_library_recordings
import event_player
import event_playlist
import event_program
import event_questions
import event_replay_captions
import event_schedule
import event_scenes
import event_simulcast
import event_slide_decks
import event_speakers
from catalog import video_version_key
import content_rating
from library_actions import title_for
import library_analytics
from library_interactivity import text_field
from library_privacy import allowed_ancestor, frame_policy, normalize_domain, try_password
import library_registration
import media_bandwidth
import regional_availability
import studio_voice
import studio_scripts


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event(
 id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, revision TEXT NOT NULL,
 settings TEXT NOT NULL, publication TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_occurrence(
 event TEXT NOT NULL, opens INTEGER NOT NULL, origin TEXT NOT NULL, remote TEXT NOT NULL,
 delivery TEXT NOT NULL, PRIMARY KEY(event,opens));
CREATE TABLE IF NOT EXISTS live_encoder_preview(
 event TEXT NOT NULL, opens INTEGER NOT NULL, source TEXT, cutoff REAL, server TEXT, key BLOB,
 PRIMARY KEY(event,opens));
CREATE TABLE IF NOT EXISTS live_encoder_preference(event TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS live_event_access(event TEXT PRIMARY KEY, link_key TEXT, password_hash TEXT);
""" + (event_program.SCHEMA + event_chat.SCHEMA + event_engagement.SCHEMA + event_scenes.SCHEMA
       + event_speakers.SCHEMA + event_captions.SCHEMA + event_analytics.SCHEMA
       + event_replay_captions.SCHEMA + event_simulcast.SCHEMA + event_library_recordings.SCHEMA
       + event_player.SCHEMA + event_playlist.SCHEMA + event_slide_decks.SCHEMA)
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")
TYPES = ("live", "webinar", "team", "learning", "custom")
EVENT_VISIBILITY = ("public", "private", "unlisted", "password", "embed_only")
RESTRICTED_AUDIENCES = ("unlisted", "password", "embed_only")
DELIVERED = ("ready", "replay")
UNRESTRICTED_EMBED = dict(mode="anywhere", domains=[])


def read(con, value, by_slug=False):
    field = "slug" if by_slug else "id"
    row = con.execute(f"SELECT * FROM live_event WHERE {field}=?", (value,)).fetchone()
    if not row:
        abort(404)
    return row, json.loads(row["settings"])


def publication(con, row, event):
    if event["visibility"] == "private":
        return "private"
    status = con.execute("SELECT status FROM action WHERE id=?", (row["publication"],)).fetchone()
    return status["status"] if status else "private"


def occurrence(con, uid, opens):
    return con.execute("SELECT * FROM live_occurrence WHERE event=? AND opens=?", (uid, opens)).fetchone()


def private_delivery(con, event, uid, opens):
    return bool(event.get("cueporch_delivery") or event["visibility"] in RESTRICTED_AUDIENCES
                or territories(event)["mode"] != "anywhere" or con.execute(
                    "SELECT 1 FROM live_encoder_preview WHERE event=? AND opens=?", (uid, opens)).fetchone())


def encoder_preview_enabled(con, uid):
    row = con.execute("SELECT enabled FROM live_encoder_preference WHERE event=?", (uid,)).fetchone()
    return bool(row and row["enabled"])


def publishing_action(con, action, uid, event):
    if event["visibility"] == "private":
        return None
    key = f"live-event-{uid}-{uuid.uuid4().hex}"
    action(con, key, "publication", dict(title=event["title"], event=uid, visibility=event["visibility"]))
    return key


def supersede_publication(con, key):
    if key:
        con.execute("UPDATE action SET status='rejected',decision='superseded' WHERE id=? AND status='waiting'", (key,))


def editable(con, uid):
    return con.execute(
        "SELECT 1 FROM live_occurrence WHERE event=? AND delivery<>'withdrawn' LIMIT 1", (uid,)
    ).fetchone() is None


def lock_event(database, uid, timeout=1):
    lock_root = database.parent / "event-production"
    lock_root.mkdir(exist_ok=True)
    return FileLock(str(lock_root / (uid + ".lock")), timeout=timeout)


def publish_replays(tx, database, origin, connect, action):
    with tx() as con:
        candidates = con.execute(
            """SELECT o.event,o.opens,o.remote FROM live_occurrence o JOIN live_event e ON e.id=o.event
               JOIN action a ON a.id=e.publication WHERE o.origin=? AND a.status='eligible'
               AND json_extract(e.settings,'$.save_replay')=1 AND o.opens<=? AND
               (o.delivery='ready' OR (o.delivery='replay' AND COALESCE((
                   SELECT enabled FROM live_caption_setting s WHERE s.event=o.event AND s.opens=o.opens
               ),json_extract(e.settings,'$.setup.captions.enabled'),0)=1 AND NOT EXISTS (
                   SELECT 1 FROM live_replay_captions c WHERE c.event=o.event AND c.opens=o.opens
               )))""",
            (origin, datetime.now(timezone.utc).timestamp()),
        ).fetchall()
    for item in candidates:
        uid, opens, remote = item
        try:
            with lock_event(database, uid, timeout=0):
                with tx() as con:
                    saved = occurrence(con, uid, opens)
                    if saved["delivery"] == "replay":
                        event_replay_captions.queue(con, action, uid, opens)
                        continue
                    if saved["delivery"] != "ready":
                        continue
                    _, event = read(con, uid)
                    registered = library_registration.active(con, "event", uid) is not None
                    managed = private_delivery(con, event, uid, opens)
                host = connect(origin)
                if event_peer.observe(host, remote)["state"] != "replay":
                    continue
                with tx() as con:
                    decision = action(con, f"live-replay-{uid}-{opens}", "replay",
                                      dict(title=event["title"], event=uid, occurrence=opens))
                    if decision in ("eligible", "waiting"):
                        event_replay_captions.queue(con, action, uid, opens)
                if decision == "eligible":
                    if not registered and not managed:
                        host.set_public(remote, True)
                    with tx() as con:
                        con.execute("UPDATE live_occurrence SET delivery='replay' WHERE event=? AND opens=?", (uid, opens))
        except (Timeout, ValueError, requests.RequestException, KeyError):
            continue


def edit(raw):
    if not isinstance(raw, dict):
        abort(422, "Enter event details.")
    title = text_field(raw.get("title"), "an event title", 128, True)
    description = text_field(raw.get("description", ""), "an event description", 5000)
    kind, latency, visibility = raw.get("type"), raw.get("latency"), raw.get("visibility")
    if kind not in TYPES or latency not in tuple(event_peer.LATENCY) or visibility not in EVENT_VISIBILITY:
        abort(422, "Choose the event type, latency and visibility.")
    replay = raw.get("save_replay", False)
    if type(replay) is not bool:
        abort(422, "Choose whether to record a replay.")
    if "archive_replay" in raw and type(raw["archive_replay"]) is not bool:
        abort(422, "Choose whether to retain the replay in the Library.")
    dvr = raw.get("dvr_seconds")
    if dvr is not None and (type(dvr) is not int or not 0 <= dvr <= 14400):
        abort(422, "Set a DVR window of up to four hours in seconds, or enter 0 to disable it.")
    try:
        schedule = event_schedule.validate(raw.get("schedule"))
    except ValueError as error:
        abort(422, str(error))
    value = dict(title=title, description=description, type=kind, latency=latency,
                visibility=visibility, save_replay=replay, schedule=schedule)
    if dvr is not None:
        value["dvr_seconds"] = dvr
    if "archive_replay" in raw:
        value["archive_replay"] = raw["archive_replay"]
    if "embed_policy" in raw:
        value["embed_policy"] = embed_policy(raw["embed_policy"])
    if "regional_policy" in raw:
        try:
            value["regional_policy"] = regional_availability.validated(raw["regional_policy"])
        except ValueError as error:
            abort(422, str(error))
    if "content_rating" in raw:
        value["content_rating"] = content_rating.validate(raw["content_rating"], 422)
    return value


def embed_policy(body):
    if not isinstance(body, dict) or body.get("mode") not in ("anywhere", "disabled", "domains"):
        abort(422, "Choose where the event can be embedded.")
    domains = body.get("domains", [])
    if not isinstance(domains, list) or len(domains) > 50:
        abort(422, "Enter up to 50 allowed embed domains.")
    mode = body["mode"]
    if mode != "domains":
        return dict(mode=mode, domains=[])
    try:
        domains = sorted(set(map(normalize_domain, domains)))
    except (ValueError, UnicodeError):
        abort(422, "Use domain names such as example.org, with an optional port.")
    if not domains:
        abort(422, "Enter at least one allowed embed domain.")
    return dict(mode=mode, domains=domains)


def sharing_settings(event):
    policy = event.get("embed_policy", UNRESTRICTED_EMBED)
    if event["visibility"] == "embed_only" and policy["mode"] == "disabled":
        abort(422, "Choose where an embed-only event can be watched.")
    return dict(visibility=event["visibility"], embed=policy["mode"], domains=policy["domains"])


def territories(event):
    return event.get("regional_policy", regional_availability.DEFAULT)


def validate_setup(raw):
    if not isinstance(raw, dict) or raw.get("method") not in ("browser", "encoder"):
        abort(422, "Choose a preferred method for the event.")
    for key in ("registration", "preview_encoder"):
        if type(raw.get(key)) is not bool:
            abort(422, "Choose the event setup options.")
    chat, captions, qa = (raw.get(key) for key in ("chat", "captions", "qa"))
    if any(not isinstance(value, dict) or type(value.get("enabled")) is not bool for value in (chat, captions, qa)):
        abort(422, "Choose the audience options.")
    interval, language = chat.get("interval"), captions.get("language")
    if type(interval) is not int or not 0 <= interval <= 120 or language not in event_captions.LANGUAGES:
        abort(422, "Choose a valid chat delay and caption language.")
    if any(type(qa.get(key)) is not bool for key in ("anonymous", "review")):
        abort(422, "Choose the Q&A options.")
    return dict(method=raw["method"], registration=raw["registration"], preview_encoder=raw["preview_encoder"],
                chat=dict(enabled=chat["enabled"], interval=interval),
                captions=dict(enabled=captions["enabled"], language=language),
                qa={key: qa[key] for key in ("enabled", "anonymous", "review")})


def reuse_setup(con, uid, event):
    # Occurrence-specific overrides take precedence over defaults at the source.
    chat, interval = event_chat.settings(con, uid, latest_setting(con, "live_chat_settings", uid))
    captions = event_captions.settings(con, uid, latest_setting(con, "live_caption_setting", uid))
    qa = event_questions.settings(con, uid, latest_setting(con, "live_qa_settings", uid))
    qa.pop("highlighted")
    return dict(method=event.get("setup", {}).get("method", "browser"),
                registration=library_registration.active(con, "event", uid) is not None,
                preview_encoder=encoder_preview_enabled(con, uid), chat=dict(enabled=chat, interval=interval),
                captions=captions, qa=qa)


def latest_setting(con, table, uid):
    return con.execute(f"SELECT MAX(opens) FROM {table} WHERE event=?", (uid,)).fetchone()[0]


def apply_setup(con, uid, event, setup, reference):
    if setup["preview_encoder"]:
        con.execute("INSERT INTO live_encoder_preference VALUES(?,1)", (uid,))
    if reference:
        player = event_player.settings(con, reference)
        con.execute("INSERT INTO live_player_settings VALUES(?,?)", (uid, json.dumps(player)))
        if event["schedule"]["repeat"] != "none":
            playlist = event_playlist.settings(con, reference)
            con.execute("INSERT INTO live_playlist VALUES(?,?,?,?)", (uid, *playlist.values()))
    if reference or setup["registration"]:
        _, form = library_registration.read_form(con, "event", reference)
        form["enabled"] = setup["registration"]
        con.execute("INSERT INTO viewer_form VALUES('event',?,?,?)", (uid, uuid.uuid4().hex, json.dumps(form)))


def access_settings(con, uid):
    row = con.execute("SELECT link_key,password_hash FROM live_event_access WHERE event=?", (uid,)).fetchone()
    return tuple(row) if row else (None, None)


def requested_access(event, body, previous=(None, None)):
    key, password_hash = previous
    password = body.get("view_password", "")
    if not isinstance(password, str) or len(password) > 256:
        abort(422, "Enter a viewing password of up to 256 characters.")
    if event["visibility"] == "unlisted":
        return key or secrets.token_urlsafe(21), None
    if event["visibility"] == "password":
        if not (password or password_hash):
            abort(422, "Enter a password for event viewers.")
        if password and (not password_hash or not check_password_hash(password_hash, password)):
            password_hash = generate_password_hash(password)
        return None, password_hash
    return None, None


def store_access(con, uid, access):
    if any(access):
        con.execute("INSERT OR REPLACE INTO live_event_access VALUES(?,?,?)", (uid, *access))
    else:
        con.execute("DELETE FROM live_event_access WHERE event=?", (uid,))


def viewer_url(row, event, embed, key=None):
    if event["visibility"] == "embed_only" and not embed:
        return None
    return url_for("event_embed" if embed else "event_page", slug=row["slug"], key=key)


def summary(con, row, event, first, last):
    visible = publication(con, row, event)
    can_embed = visible == "eligible" and event.get("embed_policy", UNRESTRICTED_EMBED)["mode"] != "disabled"
    key, _ = access_settings(con, row["id"]) if event["visibility"] == "unlisted" else (None, None)
    sessions = []
    for upcoming in event_schedule.occurrences(event["schedule"], first, last):
        target = occurrence(con, row["id"], upcoming["id"])
        program = event_program.program(con, row["id"], upcoming["id"])
        upcoming["delivery"] = target["delivery"] if target else "scheduled"
        upcoming["program"] = program["state"] if program else None
        sessions.append(upcoming)
    return dict(id=row["id"], slug=row["slug"], revision=row["revision"], event=event, editable=editable(con, row["id"]),
                preview_encoder=encoder_preview_enabled(con, row["id"]),
                publication=visible, occurrences=sessions,
                watch=viewer_url(row, event, False, key) if visible == "eligible" else None,
                embed=viewer_url(row, event, True, key) if can_embed else None)


def calendar_window():
    try:
        first = (datetime.combine(date.fromisoformat(request.args["from"]), time.min, timezone.utc)
                 if "from" in request.args else datetime.now(timezone.utc) - timedelta(days=1))
        last = (datetime.combine(date.fromisoformat(request.args["to"]) + timedelta(days=1), time.min, timezone.utc)
                if "to" in request.args else first + timedelta(days=31))
    except (ValueError, OverflowError):
        abort(422, "Choose valid calendar dates.")
    if not timedelta(0) < last - first <= timedelta(days=366) or not 1970 <= first.year < 9998:
        abort(422, "Choose a calendar range of up to one year.")
    return first, last


def overlapping(entries):
    schedule = sorted((item["start"], item["end"], entry["id"]) for entry in entries for item in entry["occurrences"])
    active = {}
    conflicts = set()
    for start, end, uid in schedule:
        active = {other: edge for other, edge in active.items() if edge > start}
        if active:
            conflicts.update((*active, uid))
        active[uid] = max(active.get(uid, ""), end)
    return sorted(conflicts)


def register(app, tx, staff, action, database, public_url, signing_key, root, read_works, institution, remember_work,
             sharing_access, enforce_sharing, ai_actor):
    media = Gateway()
    studio = Studio(database)
    programs = event_program.Programs(app, tx, root, database, read_works, studio)
    recordings = event_library_recordings.Recordings(app, tx, media, database, root, institution, remember_work)
    stop_voice, start_voice, voice_audio = studio_voice.register(app, staff)

    def stop():
        app.extensions["account_reset_delivery"].stop()
        outputs.close()
        preview_outputs.close()
        programs.close()
        studio.close()
        captions.close()
        replay_captions.close()
        recordings.close()
        stop_voice()
        stop_scripts()

    app.extensions["stop_event_capture"] = stop
    forms = URLSafeTimedSerializer(signing_key, salt="cueporch-event-enrollment")
    frames = URLSafeTimedSerializer(signing_key, salt="cueporch-event-frame")
    audiences = URLSafeTimedSerializer(signing_key, salt="cueporch-event-audience")
    guests = URLSafeTimedSerializer(signing_key, salt="cueporch-event-viewer")
    files = URLSafeTimedSerializer(signing_key, salt="cueporch-event-media")
    preview_files = URLSafeTimedSerializer(signing_key, salt="cueporch-event-encoder-preview")
    measurement = URLSafeTimedSerializer(signing_key, salt="cueporch-event-playback")
    measurement_context = URLSafeTimedSerializer(signing_key, salt="cueporch-event-playback-context")
    analytics_proxies = library_analytics.configured_proxies()
    viewer_key = signing_key.encode("utf-8")
    event_playlist.register(app, tx, staff, read)
    event_player.register(app, tx, staff, read)

    def enforce_territories(event):
        if not regional_availability.permits(territories(event), analytics_proxies):
            response = make_response("This event is unavailable in your region.", 403)
            response.headers["Content-Type"] = "text/plain; charset=utf-8"
            response.headers["Cache-Control"] = "no-store"
            abort(response)

    def check_territories(event):
        if territories(event)["mode"] != "anywhere" and not analytics_proxies:
            abort(422, "Connect a trusted region proxy before setting territories.")

    def viewer_enrolled(token, scope, released_archive):
        try:
            registered_scope = guests.loads(token, max_age=12 * 3600)
        except BadSignature:
            return False
        # A submitted registration covers the other released entries of the same event playlist.
        # Media, captions and engagement continue to check the exact occurrence being requested.
        return registered_scope == scope or (released_archive and isinstance(registered_scope, list)
            and len(registered_scope) == 5 and registered_scope[:2] == scope[:2] and registered_scope[3:] == scope[3:])

    def locked(uid):
        if not re.fullmatch(r"[a-f0-9]{32}", uid):
            abort(404)
        return lock_event(database, uid)

    @app.get("/operator/live-events")
    def live_event_dashboard():
        return render_template("live_events.html", public_url=public_url, rating_labels=content_rating.DESCRIPTORS)

    @app.get("/operator/live-events/<uid>/<int:opens>/studio")
    def event_studio_page(uid, opens):
        return render_template("record_studio.html", broadcast_endpoint=url_for("start_event_capture", uid=uid, opens=opens),
                               scene_endpoint=url_for("studio_scene_deck", uid=uid, opens=opens),
                               health_endpoint=url_for("event_health", uid=uid, opens=opens),
                               speaker_endpoint=url_for("event_speakers", event=uid, opens=opens),
                               engagement_endpoint=url_for("operator_event_engagement", uid=uid, opens=opens),
                               script_endpoint=url_for("studio_event_scripts", uid=uid, opens=opens),
                               speaker_public_url=public_url, **studio_scripts.CONTROLS)

    @app.route("/api/live-events", methods=["GET", "POST"])
    def list_events():
        staff()
        first, last = calendar_window()
        with tx() as con:
            if request.method == "GET":
                entries = [summary(con, row, json.loads(row["settings"]), first, last) for row in con.execute(
                    "SELECT * FROM live_event ORDER BY created DESC"
                )]
                return dict(events=entries, overlaps=overlapping(entries), host_ready=event_peer.host_available())
            body = request.get_json()
            event = edit(body)
            setup = validate_setup(body["starting_options"]) if "starting_options" in body else None
            reference = body.get("reuse_event")
            if reference is not None:
                if not isinstance(reference, str) or not re.fullmatch(r"[a-f0-9]{32}", reference):
                    abort(422, "Choose a stored event to reuse.")
                _, previous = read(con, reference)
                if setup is None:
                    setup = reuse_setup(con, reference, previous)
                if "embed_policy" in previous:
                    event.setdefault("embed_policy", previous["embed_policy"])
                if "regional_policy" in previous:
                    event.setdefault("regional_policy", previous["regional_policy"])
                if "content_rating" in previous:
                    event.setdefault("content_rating", previous["content_rating"])
            enforce_sharing(con, settings=sharing_settings(event))
            check_territories(event)
            access = requested_access(event, body)
            event.setdefault("archive_replay", True)
            event["cueporch_delivery"] = True
            if setup:
                if setup["captions"]["enabled"]:
                    event_captions.require_generation(con, ai_actor)
                event["setup"] = {key: setup[key] for key in ("method", "captions", "chat", "qa")}
            uid = uuid.uuid4().hex
            slug = body.get("slug") or uid
            if not isinstance(slug, str) or not SLUG.fullmatch(slug):
                abort(422, "Choose a link name with letters, numbers and hyphens.")
            if con.execute("SELECT 1 FROM live_event WHERE slug=?", (slug,)).fetchone():
                abort(409, "This event link name is already used.")
            approval = publishing_action(con, action, uid, event)
            con.execute("INSERT INTO live_event VALUES(?,?,?,?,?,?)", (uid, slug, uuid.uuid4().hex,
                        json.dumps(event), approval, datetime.now(timezone.utc).isoformat()))
            store_access(con, uid, access)
            if setup:
                apply_setup(con, uid, event, setup, reference)
            row, _ = read(con, uid)
            return summary(con, row, event, first, last), 201

    @app.get("/api/live-events/<uid>/setup")
    def event_setup(uid):
        staff()
        with tx() as con:
            _, event = read(con, uid)
            return reuse_setup(con, uid, event)

    @app.post("/api/live-events/regions/import")
    def event_region_import():
        staff()
        request.max_content_length = regional_availability.MAX_BYTES * 2
        body = request.get_json()
        text = body.get("csv") if isinstance(body, dict) else None
        if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > regional_availability.MAX_BYTES:
            abort(422, "Choose a UTF-8 territory CSV under 256 KiB.")
        try:
            return regional_availability.from_csv(text.lstrip("\ufeff"))
        except (CSVError, ValueError) as error:
            abort(422, str(error))

    @app.get("/api/live-events/<uid>/regions/export")
    def event_region_export(uid):
        staff()
        with tx() as con:
            _, event = read(con, uid)
            response = make_response(regional_availability.csv_text(territories(event)))
        response.mimetype = "text/csv"
        response.headers["Content-Disposition"] = 'attachment; filename="cueporch-event-regions.csv"'
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.put("/api/live-events/<uid>")
    def revise_event(uid):
        staff()
        first, last = calendar_window()
        body = request.get_json()
        changed = edit(body)
        try:
            with locked(uid), tx() as con:
                row, prior = read(con, uid)
                if body.get("revision") != row["revision"]:
                    abort(409, "This event changed. Open it again before saving.")
                if prior.get("cueporch_delivery"):
                    changed["cueporch_delivery"] = True
                if "archive_replay" in prior:
                    changed.setdefault("archive_replay", prior["archive_replay"])
                if "embed_policy" in prior:
                    changed.setdefault("embed_policy", prior["embed_policy"])
                if "regional_policy" in prior:
                    changed.setdefault("regional_policy", prior["regional_policy"])
                if "content_rating" in prior:
                    changed.setdefault("content_rating", prior["content_rating"])
                if "setup" in prior:
                    changed["setup"] = prior["setup"]
                if "preferred_method" in body:
                    if body["preferred_method"] not in ("browser", "encoder"):
                        abort(422, "Choose a preferred method for the event.")
                    changed["setup"] = prior.get("setup", {}) | {"method": body["preferred_method"]}
                enforce_sharing(con, settings=sharing_settings(changed), prior=sharing_settings(prior))
                if territories(changed) != territories(prior):
                    check_territories(changed)
                previous_access = access_settings(con, uid)
                access = requested_access(changed, body, previous_access)
                if changed == prior and access == previous_access:
                    return summary(con, row, prior, first, last)
                if not editable(con, uid):
                    abort(409, "Withdraw prepared occurrences before changing this event.")
                supersede_publication(con, row["publication"])
                approval = publishing_action(con, action, uid, changed)
                con.execute("UPDATE live_event SET revision=?,settings=?,publication=? WHERE id=?",
                            (uuid.uuid4().hex, json.dumps(changed), approval, uid))
                store_access(con, uid, access)
                row, _ = read(con, uid)
                if prior["schedule"] != changed["schedule"]:
                    for program in con.execute("SELECT * FROM live_program WHERE event=?", (uid,)).fetchall():
                        if find_open(changed, program["opens"]) is None:
                            con.execute("DELETE FROM live_program WHERE revision=?", (program["revision"],))
                            programs.clean(program)
                return summary(con, row, changed, first, last)
        except Timeout:
            abort(409, "Another operation is preparing this event.")

    @app.route("/api/live-events/<uid>/registration", methods=["GET", "PUT"])
    def event_registration(uid):
        staff()
        try:
            with locked(uid), tx() as con:
                read(con, uid)
                if request.method == "PUT":
                    body = request.get_json()
                    proposed = library_registration.validation(body.get("settings") if isinstance(body, dict) else None)
                    active = library_registration.active(con, "event", uid) is not None
                    if proposed["enabled"] != active and not editable(con, uid):
                        abort(409, "Set whether viewers register before preparing the encoder.")
                return library_registration.operator_form(con, "event", uid)
        except Timeout:
            abort(409, "Another operation is preparing this event.")

    @app.get("/api/live-events/<uid>/registration/export")
    def export_event_registration(uid):
        staff()
        with tx() as con:
            read(con, uid)
            return library_registration.export(con, "event", uid)

    def find_open(event, opens):
        try:
            instant = datetime.fromtimestamp(opens, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
        return next((value for value in event_schedule.occurrences(event["schedule"], instant, instant + timedelta(seconds=1))
                     if value["id"] == opens), None)

    def selected_open(event, opens):
        candidate = find_open(event, opens)
        if not candidate:
            abort(404)
        return candidate

    def operator_chat(con, uid, opens):
        _, event = read(con, uid)
        selected_open(event, opens)

    stop_scripts = studio_scripts.register(app, tx, database, action, ai_actor, operator_chat)

    def library_recording_access(con, uid, opens):
        _, event = read(con, uid)
        selected_open(event, opens)
        return event, occurrence(con, uid, opens)

    event_library_recordings.register(app, tx, staff, library_recording_access)

    def speaker_access(con, uid, opens):
        _, event = read(con, uid)
        slot = selected_open(event, opens)
        delivery = occurrence(con, uid, opens)
        if datetime.fromisoformat(slot["end"]) <= datetime.now(timezone.utc) or (
                delivery and delivery["delivery"] in ("withdrawing", "withdrawn", "replay")):
            abort(404)
        return slot

    def caption_access(con, uid, opens):
        delivery = occurrence(con, uid, opens)
        if not delivery or delivery["delivery"] != "ready":
            return False
        row = con.execute("SELECT settings FROM live_event WHERE id=?", (uid,)).fetchone()
        slot = find_open(json.loads(row["settings"]), opens) if row else None
        return bool(slot and datetime.fromisoformat(slot["end"]) > datetime.now(timezone.utc))

    def audience_chat(con, slug, opens):
        row, event = read(con, slug, True)
        slot = selected_open(event, opens)
        delivery = occurrence(con, row["id"], opens)
        if publication(con, row, event) != "eligible" or (delivery and delivery["delivery"] in ("withdrawing", "withdrawn")):
            abort(404)
        embed = request.headers.get("X-CuePorch-Chat-Embed") == "yes"
        if event["visibility"] in RESTRICTED_AUDIENCES and not audience_allowed(row, embed):
            abort(404)
        enforce_territories(event)
        form = library_registration.active(con, "event", row["id"])
        scope = [row["id"], row["revision"], opens, form["revision"] if form else None]
        if form:
            on_playlist = (delivery and delivery["delivery"] == "replay"
                           and event_playlist.settings(con, row["id"])["enabled"]
                           and event["schedule"]["repeat"] != "none")
            featured = None
            if not on_playlist and delivery and delivery["delivery"] == "replay" and event_player.settings(con, row["id"])["show_latest"]:
                featured = event_playlist.latest_released(con, row["id"])
            carried_grant = on_playlist or (featured and featured["opens"] == opens)
            if not viewer_enrolled(request.headers.get("X-CuePorch-Event-Viewer", ""), scope + [embed], carried_grant):
                abort(404)
        now = datetime.now(timezone.utc)
        active = now < datetime.fromisoformat(slot["end"]) and (not delivery or delivery["delivery"] != "replay")
        return scope, active

    audience_session = event_chat.register(app, tx, staff, signing_key, operator_chat, audience_chat)
    event_engagement.register(app, tx, staff, audience_session, operator_chat, audience_chat)
    event_scenes.register(app, tx, staff, root, read_works, operator_chat)
    event_slide_decks.register(app, tx, staff, database, operator_chat)
    event_speakers.register(app, tx, staff, speaker_access, database, start_voice, voice_audio)
    event_captions.register(app, tx, staff, operator_chat, audience_chat, ai_actor)
    captions = event_captions.LiveCaptionJobs(tx, action, media, caption_access, database)
    event_replay_captions.register(app, tx, staff, action, operator_chat, audience_chat)
    replay_captions = event_replay_captions.ReplayCaptionJobs(app, tx, media, database)

    def can_simulcast(con, uid, event):
        return (event["visibility"] == "public" and territories(event)["mode"] == "anywhere"
                and library_registration.active(con, "event", uid) is None)

    def destination_access(con, uid, opens):
        _, event = read(con, uid)
        selected_open(event, opens)
        return can_simulcast(con, uid, event)

    def outgoing_programs(con):
        now = datetime.now(timezone.utc)
        candidates = {}
        rows = con.execute("""SELECT o.*,e.settings FROM live_occurrence o JOIN live_event e ON e.id=o.event
            JOIN action a ON a.id=e.publication WHERE o.delivery='ready' AND o.opens<=? AND a.status='eligible'
            AND EXISTS (SELECT 1 FROM live_simulcast_destination d WHERE d.event=o.event AND d.enabled=1)""",
                           (int(now.timestamp()),))
        for row in rows:
            uid, opens = row["event"], row["opens"]
            event = json.loads(row["settings"])
            slot = find_open(event, opens)
            if slot and datetime.fromisoformat(slot["end"]) > now and can_simulcast(con, uid, event):
                targets = con.execute("SELECT * FROM live_simulcast_destination WHERE event=? AND enabled=1", (uid,)).fetchall()
                candidates[uid, opens] = (row["origin"], row["remote"], targets, None)
        return candidates

    outputs = event_simulcast.Simulcast(app, tx, media, database, signing_key, outgoing_programs)
    event_simulcast.register(app, tx, staff, destination_access, outputs, sharing_access, enforce_sharing)

    def released_previews(con):
        now = datetime.now(timezone.utc)
        candidates = {}
        for row in con.execute("""SELECT p.*,o.origin,e.settings FROM live_encoder_preview p
                JOIN live_occurrence o ON o.event=p.event AND o.opens=p.opens JOIN live_event e ON e.id=p.event
                WHERE o.delivery='ready' AND p.source IS NOT NULL"""):
            uid, opens = row["event"], row["opens"]
            slot = find_open(json.loads(row["settings"]), opens)
            if slot and datetime.fromisoformat(slot["end"]) > now:
                receiver = dict(id="audience", server=row["server"], secret=row["key"])
                candidates[uid, opens] = (row["origin"], row["source"], [receiver], row["cutoff"])
        return candidates

    preview_outputs = event_simulcast.Simulcast(
        app, tx, media, database, signing_key, released_previews, directory="event-preview-output")

    def provision_event(uid, opens, browser=False, automatic=False, preview=None):
        try:
            with tx() as con:
                row, event = read(con, uid)
                shift = selected_open(event, opens)
                if datetime.fromisoformat(shift["end"]) <= datetime.now(timezone.utc):
                    abort(409, "This event has ended.")
                if not automatic and event_program.program(con, uid, opens):
                    abort(409, "Use the scheduled program controls for this occurrence.")
                visible = publication(con, row, event)
                if visible in ("waiting", "rejected"):
                    abort(409, "This event's publication is waiting for approval or was declined.")
                saved = occurrence(con, uid, opens)
                broadcast = visible == "eligible" and can_simulcast(con, uid, event)
                bridge = con.execute("SELECT source FROM live_encoder_preview WHERE event=? AND opens=?",
                                     (uid, opens)).fetchone()
                state, expected = (saved["delivery"], saved["origin"]) if saved else (None, None)
                if preview is None:
                    preview = state == "preview" or (state in (None, "staged") and not automatic and encoder_preview_enabled(con, uid))
                if state in ("withdrawing", "withdrawn"):
                    abort(409, "This occurrence is being withdrawn or has been withdrawn.")
                if state == "replay":
                    abort(409, "This occurrence already has a finished recording.")
                if state == "preview" and not preview:
                    abort(409, "This encoder is private. Release it from Encoder preview.")
                if state == "ready" and preview and not (bridge and bridge["source"]):
                    abort(409, "This occurrence already has a live delivery channel.")
            with event_peer.connected_host(expected) as host:
                if state == "ready" and bridge and bridge["source"]:
                    if browser:
                        abort(409, "The original external encoder is already feeding this broadcast.")
                    return dict(event_peer.encoder(host, bridge["source"]), public=broadcast, preview=False)
                if saved:
                    remote = saved["remote"]
                else:
                    source = event | {"save_replay": False} if preview else event
                    remote = event_peer.stage_remote(host, f"CuePorch {'preview' if preview else 'event'} {uid} {opens}", source)
                    with tx() as con:
                        con.execute("INSERT INTO live_occurrence VALUES(?,?,?,?,?)",
                                    (uid, opens, host.origin, remote, "staged"))
                if state not in ("ready", "preview"):
                    event_peer.release_remote(host, remote, event, broadcast and not preview and not event.get("cueporch_delivery"))
                    with tx() as con:
                        if preview:
                            con.execute("INSERT OR IGNORE INTO live_encoder_preview(event,opens) VALUES(?,?)", (uid, opens))
                        con.execute("UPDATE live_occurrence SET delivery=? WHERE event=? AND opens=?",
                                    ("preview" if preview else "ready", uid, opens))
                if browser and event_peer.observe(host, remote)["state"] != "waiting_for_encoder":
                    abort(409, "This event already has a live or finished signal.")
                return dict(event_peer.encoder(host, remote), public=broadcast and not preview, preview=preview)
        except event_peer.UnsupportedDvrWindow as error:
            abort(422, str(error))
        except (ValueError, requests.RequestException, KeyError):
            abort(502, "Could not prepare the event on the connected video host.")

    @app.post("/api/live-events/<uid>/<int:opens>/encoder")
    def prepare_external_encoder(uid, opens):
        staff()
        body = request.get_json() if request.is_json else {}
        if not isinstance(body, dict) or type(body.get("preview", False)) is not bool:
            abort(422, "Choose whether to preview the encoder privately.")
        try:
            with locked(uid):
                return provision_event(uid, opens, preview=body.get("preview"))
        except Timeout:
            abort(409, "Another operation is preparing this event.")

    @app.put("/api/live-events/<uid>/encoder-settings")
    def encoder_preferences(uid):
        staff()
        body = request.get_json()
        if not isinstance(body, dict) or type(body.get("preview")) is not bool:
            abort(422, "Choose whether to preview external encoders for this event.")
        with tx() as con:
            read(con, uid)
            con.execute("INSERT OR REPLACE INTO live_encoder_preference VALUES(?,?)", (uid, body["preview"]))
        return dict(preview=body["preview"])

    @app.route("/api/live-events/<uid>/<int:opens>/program", methods=["GET", "PUT"])
    def event_program_settings(uid, opens):
        staff()
        previous = None
        try:
            with locked(uid), tx() as con:
                _, event = read(con, uid)
                selected_open(event, opens)
                record = event_program.program(con, uid, opens)
                editable_program = opens > datetime.now(timezone.utc).timestamp() and occurrence(con, uid, opens) is None
                if request.method == "PUT":
                    if not editable_program:
                        abort(409, "Choose a future occurrence without a prepared encoder.")
                    body = request.get_json()
                    if not isinstance(body, dict):
                        abort(422, "Choose the program videos.")
                    state, previous = programs.save(con, uid, opens, body.get("revision"), body.get("sources"), event["visibility"])
                else:
                    state = event_program.export(record)
                selected = {(item["work"], item["cut"]) for item in state["sources"]}
                choices = []
                for work in read_works():
                    if not event_program.permitted(con, work, event["visibility"]):
                        continue
                    name = title_for(con, work)
                    choices.append(dict(work=work["id"], cut=video_version_key(work), title=name))
                    for cut in work.get("versions_history", []):
                        key = video_version_key(cut)
                        if (work["id"], key) in selected:
                            choices.append(dict(work=work["id"], cut=key, title=name + " (retained cut)"))
        except Timeout:
            abort(409, "Another operation is preparing this event.")
        except ValueError as error:
            abort(422, str(error))
        if previous:
            programs.clean(previous)
        return dict(state, editable=editable_program, choices=sorted(choices, key=lambda entry: entry["title"].casefold()))

    def qualify_program(con, uid, opens):
        row, event = read(con, uid)
        selected_open(event, opens)
        return publication(con, row, event) in ("private", "eligible") and event_peer.host_available()

    programs.start(locked, qualify_program, provision_event)

    def direct_window(uid, connection):
        with tx() as con:
            _, event = read(con, uid)
        seconds = event.get("dvr_seconds", 120)
        if event["latency"] == "low" and not connection["preview"] and 4 <= seconds <= 120:
            return seconds
        return None

    @app.post("/api/live-events/<uid>/<int:opens>/browser")
    def start_event_capture(uid, opens):
        staff()
        try:
            with locked(uid):
                connection = provision_event(uid, opens, browser=True, preview=False)
                return dict(token=studio.start(uid, opens, connection, direct_window(uid, connection))), 201
        except Timeout:
            abort(409, "Another operation is preparing this event.")
        except BusyError as error:
            abort(409, str(error))
        except (OSError, ValueError):
            abort(502, "Could not start the browser's encoder.")

    @app.put("/api/live-events/<uid>/<int:opens>/browser/<token>/<int:sequence>")
    def receive_event_capture(uid, opens, token, sequence):
        staff()
        room = studio.get(uid, opens, token)
        if not room:
            abort(404)
        request.max_content_length = CHUNK_BYTES
        if request.mimetype != "video/webm":
            abort(415, "The browser encoder needs WebM media.")
        try:
            room.add(sequence, request.get_data(cache=False))
        except BusyError:
            abort(429, "The video host is catching up.")
        except ValueError as error:
            abort(409, str(error))
        return "", 204

    @app.delete("/api/live-events/<uid>/<int:opens>/browser/<token>")
    def finish_event_capture(uid, opens, token):
        staff()
        room = studio.get(uid, opens, token)
        if room:
            room.stop.set()
        return "", 204

    @app.route("/api/live-events/<uid>/<int:opens>/srt", methods=["POST", "DELETE"])
    def event_srt(uid, opens):
        staff()
        if request.method == "DELETE":
            studio.close_srt(uid, opens)
            return "", 204
        body = request.get_json()
        if not isinstance(body, dict) or type(body.get("preview", False)) is not bool:
            abort(422, "Choose a receiving interface and port.")
        try:
            endpoint = srt_endpoint(body.get("interface"), body.get("port"))
        except ValueError as error:
            abort(422, str(error))
        try:
            with locked(uid):
                prior = studio.srt_credentials(uid, opens, endpoint)
                if prior:
                    with tx() as con:
                        delivery = occurrence(con, uid, opens)
                        return dict(prior, preview=bool(delivery and delivery["delivery"] == "preview"))
                connection = provision_event(uid, opens, browser=True, preview=body.get("preview"))
                return dict(studio.srt(uid, opens, connection, endpoint, direct_window(uid, connection)),
                            preview=connection["preview"])
        except Timeout:
            abort(409, "Another operation is preparing this event.")
        except BusyError as error:
            abort(409, str(error))
        except (OSError, ValueError):
            abort(502, "Could not start the SRT listener.")

    @app.get("/api/live-events/<uid>/<int:opens>/health")
    def event_health(uid, opens):
        staff()
        with tx() as con:
            _, event = read(con, uid)
            selected_open(event, opens)
            target = occurrence(con, uid, opens)
            managed = private_delivery(con, event, uid, opens) or library_registration.active(con, "event", uid)
        local = studio.health(uid, opens)
        if request.args.get("source") == "relay":
            return dict(encoder=local)
        if target is None or target["delivery"] not in DELIVERED:
            return dict(state=target["delivery"] if target else "scheduled", encoder=local)
        try:
            with event_peer.connected_host(target["origin"]) as host:
                result = event_peer.observe(host, target["remote"])
                if result["state"] in ("live", "waiting_for_encoder"):
                    result["delivery_settings"] = event_peer.live_settings(host, target["remote"])
                if managed:
                    phase = result["state"]
                    if phase == "live" or (phase == "replay" and target["delivery"] == "replay"):
                        with tx() as con:
                            result["viewers"] = event_analytics.current(con, uid, opens, phase)
                    else:
                        result["viewers"] = 0
                return dict(result, encoder=local)
        except (ValueError, requests.RequestException, KeyError):
            abort(502, "Could not read stream health from the connected video host.")

    @app.delete("/api/live-events/<uid>/<int:opens>/delivery")
    def withdraw_event(uid, opens):
        staff()
        try:
            with locked(uid):
                with tx() as con:
                    _, event = read(con, uid)
                    selected_open(event, opens)
                    target = occurrence(con, uid, opens)
                    current_program = event_program.program(con, uid, opens)
                    if current_program and current_program["state"] in (*event_program.PENDING, *event_program.ACTIVE):
                        con.execute("UPDATE live_program SET state='cancelled' WHERE revision=?", (current_program["revision"],))
                        programs.clean(current_program)
                    if target is None:
                        con.execute("INSERT INTO live_occurrence VALUES(?,?,?,?,'withdrawn')", (uid, opens, "", ""))
                        return "", 204
                    if target["delivery"] == "withdrawn":
                        return "", 204
                    con.execute("UPDATE live_occurrence SET delivery='withdrawing' WHERE event=? AND opens=?",
                                (uid, opens))
                studio.withdraw(uid, opens)
                with event_peer.connected_host(target["origin"]) as host:
                    video = host.call("GET", "/api/v1/videos/" + target["remote"]).json()
                    if video["privacy"]["id"] != 3:
                        host.set_public(target["remote"], False)
                with tx() as con:
                    con.execute("UPDATE live_occurrence SET delivery='withdrawn' WHERE event=? AND opens=?",
                                (uid, opens))
                return "", 204
        except Timeout:
            abort(409, "Another operation is preparing this event.")
        except (ValueError, requests.RequestException, KeyError):
            abort(502, "The CuePorch player is closed. Retry to withdraw the copy from the video host.")

    @app.get("/api/live-events/<uid>/<int:opens>/analytics")
    def event_audience_analytics(uid, opens):
        staff()
        with tx() as con:
            _, event = read(con, uid)
            selected_open(event, opens)
            target = occurrence(con, uid, opens)
            phase = {"ready": "live", "replay": "replay"}.get(target["delivery"]) if target else None
            return event_analytics.summarize(con, uid, opens, phase)

    def hls_manifest(delivery, raw, host_private):
        manifest = next(entry["playlistUrl"] for entry in raw["streamingPlaylists"] if entry["type"] == 1)
        directory, filename = root_path(delivery["origin"], manifest, delivery["remote"], host_private)
        return HLS_PREFIXES.index(directory.rsplit("/", 1)[0] + "/"), filename

    @app.route("/api/live-events/<uid>/<int:opens>/preview", methods=["GET", "POST"])
    def encoder_preview(uid, opens):
        staff()

        def target():
            with tx() as con:
                row, event = read(con, uid)
                slot = selected_open(event, opens)
                delivery = occurrence(con, uid, opens)
                return row["revision"], event, slot, delivery, publication(con, row, event)

        try:
            if request.method == "POST":
                with locked(uid):
                    _, event, slot, delivery, visible = target()
                    if delivery and delivery["delivery"] == "ready":
                        return dict(state="released", address=None)
                    if not delivery or delivery["delivery"] != "preview":
                        abort(409, "Prepare the encoder for private preview first.")
                    if visible not in ("eligible", "private") or datetime.fromisoformat(slot["end"]) <= datetime.now(timezone.utc):
                        abort(409, "This occurrence cannot start. Check its schedule and publication status.")
                    origin, source = delivery["origin"], delivery["remote"]
                    with media.read(origin, "/api/v1/videos/" + source) as response:
                        observed = event_peer.status(response.json())
                        host_time = parsedate_to_datetime(response.headers["Date"])
                    if observed["public"]:
                        abort(409, "Restore the private setting on the connected video host before releasing.")
                    if observed["state"] != "live":
                        abort(409, "Wait for a live signal before releasing the encoder.")
                    if host_time.tzinfo is None:
                        raise ValueError("The video host time is unavailable.")
                    # New audience ingest starts after the source host clock at release.
                    # The HTTP date has one-second precision, so discard the current second as well.
                    cutoff = host_time.timestamp() + 1
                    with event_peer.connected_host(origin) as host:
                        remote = event_peer.stage_remote(host, f"CuePorch audience {uid} {opens}", event)
                        server, _, key = ingest_address(event_peer.encoder(host, remote)).rpartition("/")
                    protected_key = preview_outputs.cipher.encrypt(key.encode())
                    with tx() as con:
                        con.execute("UPDATE live_encoder_preview SET source=?,cutoff=?,server=?,key=? WHERE event=? AND opens=?",
                                    (source, cutoff, server, protected_key, uid, opens))
                        con.execute("UPDATE live_occurrence SET delivery='ready',remote=? WHERE event=? AND opens=?", (remote, uid, opens))
                    return dict(state="released", address=None)

            revision, _, _, delivery, _ = target()
            if not delivery or delivery["delivery"] != "preview":
                return dict(state="inactive", address=None)
            raw = media.metadata(delivery["origin"], delivery["remote"])
            observed = event_peer.status(raw)
            if observed["state"] != "live" or observed["public"]:
                return dict(state="waiting" if observed["state"] == "waiting_for_encoder" else "ended", address=None)
            gateway, filename = hls_manifest(delivery, raw, True)
            token = preview_files.dumps([uid, revision, opens, delivery["remote"], gateway])
            return dict(state="signal", address=url_for("encoder_preview_file", token=token, filename=filename))
        except Timeout:
            abort(409, "Another operation is preparing this event.")
        except (ValueError, requests.RequestException, KeyError, StopIteration):
            abort(502, "Could not read the private encoder feed.")

    @app.get("/encoder-preview/<token>/<filename>")
    def encoder_preview_file(token, filename):
        try:
            uid, revision, opens, remote, gateway = preview_files.loads(token, max_age=30 * 60)
            if not LIVE_FILE.fullmatch(filename):
                abort(404)
            with tx() as con:
                row, _ = read(con, uid)
                delivery = occurrence(con, uid, opens)
                if row["revision"] != revision or not delivery or delivery["delivery"] != "preview" or delivery["remote"] != remote:
                    abort(404)
            return media.media(delivery["origin"], remote, gateway, filename, request.headers.get("Range"))
        except (BadSignature, ValueError, requests.RequestException):
            abort(404)

    def viewer_playback(event, delivery, scope, native_video, host_private, analytics_scope):
        address = analytics_token = None
        status = "Scheduled"
        try:
            if native_video:
                raw = media.metadata(delivery["origin"], delivery["remote"])
                observed = event_peer.status(raw)
                available = (observed["public"] != host_private and (observed["state"] == "live" or
                             (observed["state"] == "replay" and delivery["delivery"] == "replay")))
                if available:
                    gateway, filename = hls_manifest(delivery, raw, host_private)
                    replay = observed["state"] == "replay"
                    session = files.dumps(scope + [delivery["remote"], gateway, replay])
                    address = url_for("event_media_file", token=session, filename=filename)
                    status = "Replay" if replay else "Live"
                duration = raw.get("duration")
            else:
                observed = event_peer.public_status(delivery["origin"], delivery["remote"])
                duration = observed["duration"]
                replay = observed["state"] == "replay"
                if observed["public"] and (observed["state"] == "live" or (replay and delivery["delivery"] == "replay")):
                    address = f"{delivery['origin']}/videos/embed/{delivery['remote']}"
                    status = "Replay" if replay else "Live"
            if address and analytics_scope is not None:
                duration_ms = (round(duration * 1000) if replay and type(duration) in (int, float)
                               and math.isfinite(duration) and .001 <= duration <= 7 * 86400 else None)
                analytics_token = measurement.dumps(scope + [delivery["remote"], replay, uuid.uuid4().hex,
                                                              [*analytics_scope, duration_ms]])
                if not native_video:
                    address += "?api=1"
            if observed["state"] == "waiting_for_encoder":
                status = "Waiting for the encoder"
            elif observed["state"] in ("preparing_replay", "ended"):
                status = "Preparing the replay" if event["save_replay"] else "This event has ended"
            elif host_private and observed["state"] == "replay" and not address:
                status = "The replay is awaiting release"
        except (ValueError, requests.RequestException, KeyError, StopIteration):
            status = "The stream is unavailable"
        return address, analytics_token, status

    def event_frame(row, event, embed, status_request):
        if not embed:
            return "'none'", None
        policy = event.get("embed_policy", UNRESTRICTED_EMBED)
        if policy["mode"] == "disabled":
            abort(404)
        if policy["mode"] == "anywhere":
            return "*", None
        scope = [row["id"], row["revision"]]
        supplied = request.form.get("frame") or request.headers.get("X-CuePorch-Event-Frame", "")
        if request.method == "POST" or status_request:
            if not valid_ticket(frames, supplied, scope, 12 * 3600):
                abort(404)
        else:
            if (request.headers.get("Sec-Fetch-Dest") not in (None, "iframe", "frame")
                    or not allowed_ancestor(request.referrer or "", policy["domains"])):
                abort(404)
            supplied = frames.dumps(scope)
        return frame_policy(policy["domains"]), supplied

    def audience_allowed(row, embed, token=None):
        if token is None:
            token = request.headers.get("X-CuePorch-Event-Access", "")
        return valid_ticket(audiences, token, [row["id"], row["revision"], embed], 12 * 3600)

    def viewer_permission(con, row, event, embed, json_status):
        mode = event["visibility"]
        if mode not in RESTRICTED_AUDIENCES:
            return None, None, False
        key, password_hash = access_settings(con, row["id"])
        received = request.form.get("event_access") or request.headers.get("X-CuePorch-Event-Access", "")
        allowed = audience_allowed(row, embed, received)
        if mode == "embed_only":
            if not embed or (not allowed and (json_status or request.method != "GET"
                    or request.headers.get("Sec-Fetch-Dest") not in ("iframe", "frame"))):
                abort(404)
        elif mode == "unlisted":
            if not key or not hmac.compare_digest(request.args.get("key", "").encode(), key.encode()):
                abort(404)
        elif not allowed:
            if json_status or not password_hash:
                abort(404)
            if request.method != "POST" or not try_password(con, signing_key, "event:" + row["id"],
                                                             request.form.get("password", ""), password_hash):
                return None, None, True
        return received if allowed else audiences.dumps([row["id"], row["revision"], embed]), key, False

    def viewer(slug, embed):
        json_status = request.accept_mimetypes.best == "application/json"
        grant = None
        enrollment = None
        form_error = None
        archives = None
        latest = latest_slot = None
        latest_captioned = False
        with tx() as con:
            row, event = read(con, slug, True)
            if publication(con, row, event) != "eligible":
                abort(404)
            ancestors, frame_grant = event_frame(row, event, embed, json_status)
            access, link_key, locked = viewer_permission(con, row, event, embed, json_status)
            enforce_territories(event)
            if locked:
                incorrect = request.method == "POST"
                view = render_template("event_password.html", unlock_token=frame_grant, incorrect=incorrect)
                return audience_response(view, ancestors, 403 if incorrect else 200, restricted=True)
            artwork_access = dict(access=access, framed="yes" if embed else None) if access else {}
            context_key = [row["id"], row["revision"], embed]
            now = datetime.now(timezone.utc)
            upcoming = list(event_schedule.occurrences(event["schedule"], now - timedelta(days=1), now + timedelta(days=366)))
            if "occurrence" in request.args:
                opens = request.args.get("occurrence", type=int)
                if opens is None:
                    abort(404)
                shown = selected_open(event, opens)
            else:
                shown = next((value for value in upcoming if datetime.fromisoformat(value["end"]) > now), None)
                if shown is None and upcoming:
                    shown = upcoming[-1]
                if shown is None:
                    saved = con.execute("SELECT opens FROM live_occurrence WHERE event=? AND delivery IN ('ready','replay') ORDER BY opens DESC LIMIT 1",
                                        (row["id"],)).fetchone()
                    if saved:
                        shown = selected_open(event, saved["opens"])
            delivery = occurrence(con, row["id"], shown["id"]) if shown else None
            withdrawn = delivery and delivery["delivery"] in ("withdrawing", "withdrawn")
            form = library_registration.active(con, "event", row["id"]) if shown else None
            options = event_playlist.settings(con, row["id"])
            playlist_enabled = options["enabled"] and event["schedule"]["repeat"] != "none"
            captioned = bool(shown and not withdrawn and (not delivery or delivery["delivery"] != "replay") and
                             event_captions.active_track(con, row["id"], shown["id"]))
            replay_captioned = bool(delivery and delivery["delivery"] == "replay" and
                                    event_replay_captions.read(con, row["id"], shown["id"]))
            scope = [row["id"], row["revision"], shown["id"], form["revision"] if form else None, embed] if shown else None
            if form:
                just_unlocked = event["visibility"] == "password" and "password" in request.form and "enrollment" not in request.form
                if request.method == "POST" and not withdrawn and not just_unlocked:
                    if not valid_ticket(forms, request.form.get("enrollment", ""), scope, 1200):
                        abort(403, "Open the registration form again.")
                    form_error = library_registration.enroll(con, "event", row["id"], form, request.form, request.args)
                    if not form_error:
                        grant = guests.dumps(scope)
                elif token := request.headers.get("X-CuePorch-Event-Viewer"):
                    on_playlist = playlist_enabled and (withdrawn or (delivery and delivery["delivery"] == "replay"))
                    if viewer_enrolled(token, scope, on_playlist):
                        grant = token
                if not grant and not json_status and not withdrawn:
                    enrollment = forms.dumps(scope)
            if playlist_enabled and (not form or grant):
                archives = event_playlist.archive_playlist(con, row["id"], options, event["schedule"])
            artwork = event_player.settings(con, row["id"])
            appearance = event_player.appearance(artwork, event)
            managed = private_delivery(con, event, row["id"], shown["id"]) if shown else False
            latest_managed = False
            if shown and not withdrawn and artwork["show_latest"] and (not form or grant):
                saved = event_playlist.latest_released(con, row["id"])
                if saved and saved["opens"] != shown["id"]:
                    latest = saved
                    latest_slot = selected_open(event, latest["opens"])
                    latest_managed = private_delivery(con, event, row["id"], latest["opens"])
                    latest_captioned = bool(event_replay_captions.read(con, row["id"], latest["opens"]))
            poster = url_for("event_artwork", slug=slug, opens=shown["id"], kind="poster", digest=artwork["poster"], **artwork_access) if (
                shown and not withdrawn and artwork["poster"]) else None
            logo = url_for("event_artwork", slug=slug, opens=shown["id"], kind="logo", digest=artwork["logo"], **artwork_access) if (
                shown and not withdrawn and artwork["logo"]) else None
            viewers = event_analytics.current(con, row["id"], shown["id"], "live") if (
                artwork["show_viewers"] and delivery and delivery["delivery"] == "ready" and (not form or grant)) else None
            next_broadcast = shown
            if artwork["show_schedule"] and shown and (datetime.fromisoformat(shown["end"]) <= now
                    or (delivery and delivery["delivery"] == "replay")):
                canceled = {record["opens"] for record in con.execute("SELECT opens FROM live_occurrence WHERE event=?"
                            " AND delivery IN ('withdrawing','withdrawn')", (row["id"],))}
                next_broadcast = next((item for item in upcoming if item["id"] > now.timestamp()
                                       and item["id"] not in canceled), None)
            pre_live = event_player.lobby(artwork, event, next_broadcast, poster) if not withdrawn else None
        address = None
        analytics_token = None
        transport = "hosted"
        reporting_scope = reporting_token = None
        if library_analytics.permitted() and request.args.get("dnt", "").lower() not in ("1", "true"):
            origin = library_analytics.request_context(analytics_proxies)
            supplied = request.headers.get("X-CuePorch-Event-Context") or request.form.get("event_context")
            if supplied:
                try:
                    event_id, revision, framed, prior_origin = measurement_context.loads(supplied, max_age=12 * 3600)
                    if [event_id, revision, framed] == context_key:
                        origin = prior_origin
                except (BadSignature, ValueError):
                    pass
            reporting_token = measurement_context.dumps([*context_key, origin])
            reporting_scope = ["embed" if embed else "watch", *origin]
        protected = bool(form)
        host_private = protected or managed
        native_video = host_private or captioned or replay_captioned or appearance["customize"] or viewers is not None
        can_participate = shown and not withdrawn and (not protected or grant)
        playing = shown
        preview_replay = False
        status = "Scheduled"
        if withdrawn:
            status = "This occurrence was withdrawn"
        if protected and not grant and not withdrawn:
            status = "Registration required"
        elif delivery and delivery["delivery"] in DELIVERED and shown["id"] <= now.timestamp():
            source = studio.playback(row["id"], shown["id"]) if (
                delivery["delivery"] == "ready" and event["latency"] == "low" and not captioned) else None
            if source:
                transport = f"local:{source.identity}"
                session = files.dumps(scope + [delivery["remote"], transport, False])
                address = url_for("event_media_file", token=session, filename="0.m3u8")
                native_video, status = True, "Live"
                if reporting_scope is not None:
                    analytics_token = measurement.dumps(scope + [delivery["remote"], False, uuid.uuid4().hex,
                                                                  [*reporting_scope, None]])
            else:
                address, analytics_token, status = viewer_playback(
                    event, delivery, scope, native_video, host_private, reporting_scope)
        if not address and latest:
            replay_scope = [*scope[:2], latest["opens"], *scope[3:]]
            private_replay = protected or latest_managed
            native_replay = private_replay or latest_captioned or appearance["customize"]
            replay_address, replay_measurement, replay_status = viewer_playback(
                event, latest, replay_scope, native_replay, private_replay, reporting_scope)
            if replay_address and replay_status == "Replay":
                address, analytics_token, status = replay_address, replay_measurement, replay_status
                native_video, playing, preview_replay = native_replay, latest_slot, True
        caption_endpoint = None
        if can_participate:
            if status == "Replay" and native_video:
                caption_endpoint = url_for("audience_replay_captions", slug=slug, opens=playing["id"])
            elif captioned:
                caption_endpoint = url_for("viewer_event_captions", slug=slug, opens=shown["id"])
        if address:
            pre_live = None
        branding = dict(logo=logo if address else None, byline=artwork["byline"] if address else "",
                        live_badge=status == "Live" and artwork["show_live_badge"],
                        viewers=viewers if status == "Live" else None)
        view = (dict(status=status, address=address, protected=protected, native_video=native_video, lobby_preview=pre_live,
                     transport=transport,
                     captions=caption_endpoint, analytics_token=analytics_token, playlist=archives, occurrence=playing,
                     preview_replay=preview_replay, appearance=appearance, branding=branding)
                if json_status
                else render_template("live_event.html", event=event, occurrence=playing, audience_occurrence=shown,
                                     address=address, status=status, embed=embed, protected=protected, transport=transport,
                                     native_video=native_video, caption_endpoint=caption_endpoint, lobby_preview=pre_live,
                                     preview_replay=preview_replay, appearance=appearance, branding=branding,
                                     analytics_token=analytics_token, event_context=reporting_token, playlist=archives,
                                     viewer_grant=grant, registration=form if enrollment else None,
                                     unlock_token=frame_grant, event_access=access,
                                     current_event_url=viewer_url(row, event, embed, link_key),
                                     content_labels=content_rating.viewer_labels(event.get("content_rating", {})),
                                     enrollment=enrollment, registration_error=form_error,
                                     chat_endpoint=url_for("audience_event_chat", slug=slug, opens=shown["id"])
                                     if can_participate else None,
                                     engagement_endpoint=url_for("audience_event_engagement", slug=slug, opens=shown["id"])
                                     if can_participate else None))
        return audience_response(view, ancestors, 422 if form_error else 200, bool(access))

    def audience_response(view, ancestors, status, restricted):
        response = make_response(view, status)
        response.headers["Vary"] = "Accept"
        response.headers["Content-Security-Policy"] = "frame-ancestors " + ancestors
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        if restricted:
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.route("/events/<slug>", methods=["GET", "POST"])
    def event_page(slug):
        return viewer(slug, False)

    @app.route("/events/<slug>/embed", methods=["GET", "POST"])
    def event_embed(slug):
        return viewer(slug, True)

    @app.get("/events/<slug>/<int:opens>/<any(poster,logo):kind>/<digest>")
    def event_artwork(slug, opens, kind, digest):
        with tx() as con:
            row, event = read(con, slug, True)
            selected_open(event, opens)
            delivery = occurrence(con, row["id"], opens)
            if (publication(con, row, event) != "eligible" or event_player.settings(con, row["id"])[kind] != digest
                    or (delivery and delivery["delivery"] in ("withdrawing", "withdrawn"))):
                abort(404)
            if event["visibility"] in RESTRICTED_AUDIENCES and not audience_allowed(
                    row, request.args.get("framed") == "yes", request.args.get("access", "")):
                abort(404)
            enforce_territories(event)
            image = con.execute("SELECT mime,content FROM review_logo WHERE digest=?", (digest,)).fetchone()
        if not image:
            abort(404)
        response = make_response(image["content"])
        response.mimetype = image["mime"]
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def delivered(con, uid, revision, opens, form_revision, remote, replay):
        row, event = read(con, uid)
        delivery = occurrence(con, uid, opens)
        form = library_registration.active(con, "event", uid)
        if (row["revision"] != revision or publication(con, row, event) != "eligible" or
                not delivery or delivery["remote"] != remote or delivery["delivery"] not in DELIVERED or
                replay != (delivery["delivery"] == "replay") or
                (form["revision"] if form else None) != form_revision):
            abort(404)
        enforce_territories(event)
        return delivery

    @app.post("/api/event-playback")
    def event_playback_sample():
        try:
            uid, revision, opens, form_revision, _, remote, replay, nonce, *reporting = measurement.loads(
                request.headers.get("X-CuePorch-Playback", ""), max_age=12 * 3600
            )
        except (BadSignature, ValueError):
            abort(404)
        if library_analytics.permitted():
            with tx() as con:
                delivered(con, uid, revision, opens, form_revision, remote, replay)
                event_analytics.record(con, uid, opens, "replay" if replay else "live", nonce, request.get_json(),
                                       report_scope=reporting[0] if reporting else None, viewer_key=viewer_key)
        return "", 204

    @app.get("/event-media/<token>/<filename>")
    def event_media_file(token, filename):
        try:
            uid, revision, opens, form_revision, _, remote, gateway, replay = files.loads(token, max_age=12 * 3600)
        except (BadSignature, ValueError):
            abort(404)
        if not replay and not LIVE_FILE.fullmatch(filename):
            abort(404)
        local = isinstance(gateway, str) and gateway.startswith("local:")
        with tx() as con:
            delivery = delivered(con, uid, revision, opens, form_revision, remote, replay)
            if local and (replay or event_captions.active_track(con, uid, opens)):
                abort(404)
        try:
            if local:
                source = studio.playback(uid, opens, gateway.removeprefix("local:"))
                if not source:
                    abort(404)
                response = local_hls(source.path, filename)
            else:
                response = media.media(delivery["origin"], remote, gateway, filename, request.headers.get("Range"))
        except (OSError, ValueError, requests.RequestException):
            abort(404)
        return media_bandwidth.measure(response, tx, uid, "vod" if replay else "live")


def valid_ticket(signer, token, scope, age):
    try:
        return signer.loads(token, max_age=age) == scope
    except BadSignature:
        return False
