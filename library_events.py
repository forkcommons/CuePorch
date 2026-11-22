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
from flask import abort, make_response, render_template, request, send_file, url_for
from itsdangerous import BadSignature, URLSafeSerializer, URLSafeTimedSerializer
import requests
from werkzeug.exceptions import Conflict, UnprocessableEntity
from werkzeug.security import check_password_hash, generate_password_hash

from browser_account_security import private, same_origin, secure_site
from event_media import Gateway, HLS_PREFIXES, LIVE_FILE, dash_hls, local_hls, root_path
from event_capture import BusyError, CHUNK_BYTES, Studio, ingest_address, srt_endpoint
from event_interpreters import InterpreterInputs
import event_peer
import event_rtmp
import event_dash_buffer
import event_dash_format
import event_analytics
import event_attendance
import event_chat
import event_captions
import event_engagement
import event_hubspot_sync
import event_salesforce_sync
import event_salesforce_registration
import event_salesforce_engagement
import salesforce_crm
import event_hubspot_outbound
import event_contact_delivery
import event_mailchimp_engagement
import event_mailchimp_registration
import event_marketo_engagement
import marketo_crm
import event_hubspot_engagement
import hubspot_crm
import event_library_recordings
import event_imported_replay
import event_replay_spool
import event_mail
import event_player
import event_landing
import event_landing_speakers
import event_playlist
import event_program
import event_questions
import event_registrants
import event_replay_captions
import event_schedule
import event_scenes
import event_simulcast
import event_slide_decks
import event_speakers
import event_webhooks
from catalog import video_version_key
import content_rating
from library_actions import title_for
import library_analytics
from library_interactivity import text_field
from library_languages import language_tag
from library_privacy import allowed_ancestor, frame_policy, normalize_domain, try_password
import library_registration
import mail_relay
import media_bandwidth
import regional_availability
import registration_csv
import studio_voice
import studio_scripts
from team_accounts import current as current_team_member
from team_accounts import note


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
       + event_imported_replay.SCHEMA
       + event_player.SCHEMA + event_playlist.SCHEMA + event_landing.SCHEMA + event_landing_speakers.SCHEMA + event_slide_decks.SCHEMA + event_attendance.SCHEMA
       + event_mail.SCHEMA + event_registrants.SCHEMA + event_hubspot_sync.SCHEMA + event_hubspot_outbound.SCHEMA
       + event_hubspot_engagement.SCHEMA + event_marketo_engagement.SCHEMA
       + event_mailchimp_engagement.SCHEMA + event_salesforce_engagement.SCHEMA)
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


def find_open(event, opens):
    try:
        instant = datetime.fromtimestamp(opens, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return next((value for value in event_schedule.occurrences(event["schedule"], instant, instant + timedelta(seconds=1))
                 if value["id"] == opens), None)


def observe_delivery(tx, database, origin, connect):
    now = datetime.now(timezone.utc)
    with tx() as con:
        hooks = event_webhooks.subscribed(con, ("live-event-started", "live-event-ended"))
        entries = con.execute("""SELECT o.event,o.opens,o.remote,e.settings,f.state AS observed,
                                      p.generation AS marketo_generation,
                                      q.generation AS mailchimp_generation,
                                      s.generation AS salesforce_generation
                               FROM live_occurrence o
                               JOIN live_event e ON e.id=o.event
                               LEFT JOIN live_webhook_feed f
                                 ON f.event=o.event AND f.occurrence=o.opens
                               LEFT JOIN live_event_contact_plan p
                                 ON p.provider='marketo' AND p.event=o.event AND p.opens=o.opens
                                AND p.event_revision=e.revision
                                AND json_extract(p.destination,'$.engagement')=1
                                AND EXISTS (SELECT 1 FROM live_event_marketo_engagement m
                                            WHERE m.event=o.event AND m.opens=o.opens
                                              AND m.generation=p.generation AND m.state='queued')
                                AND NOT EXISTS (SELECT 1 FROM live_event_marketo_completion c
                                                WHERE c.generation=p.generation)
                               LEFT JOIN live_event_contact_plan q
                                 ON q.provider='mailchimp' AND q.event=o.event AND q.opens=o.opens
                                AND q.event_revision=e.revision
                                AND json_extract(q.destination,'$.engagement')=1
                                AND EXISTS (SELECT 1 FROM live_event_mailchimp_engagement m
                                            WHERE m.event=o.event AND m.opens=o.opens
                                              AND m.generation=q.generation AND m.state='queued')
                                AND NOT EXISTS (SELECT 1 FROM live_event_mailchimp_completion c
                                                WHERE c.generation=q.generation)
                               LEFT JOIN live_event_contact_plan s
                                 ON s.provider='salesforce' AND s.event=o.event AND s.opens=o.opens
                                AND s.event_revision=e.revision
                                AND json_extract(s.destination,'$.engagement')=1
                                AND EXISTS (SELECT 1 FROM live_event_salesforce_engagement m
                                            WHERE m.event=o.event AND m.opens=o.opens
                                              AND m.generation=s.generation AND m.state='queued')
                                AND NOT EXISTS (SELECT 1 FROM live_event_salesforce_completion c
                                                WHERE c.generation=s.generation)
                               WHERE o.origin=? AND o.opens<=?
                                 AND ((? AND o.delivery='ready' AND (f.state IS NULL OR f.state='live'))
                                      OR ((p.generation IS NOT NULL OR q.generation IS NOT NULL
                                           OR s.generation IS NOT NULL)
                                          AND o.delivery IN ('ready','replay')))""",
                            (origin, now.timestamp(), hooks)).fetchall()
    for item in entries:
        uid, opens = item["event"], item["opens"]
        slot = find_open(json.loads(item["settings"]), opens)
        if not slot:
            continue
        try:
            with lock_event(database, uid, timeout=0):
                marketo_generation = item["marketo_generation"]
                mailchimp_generation = item["mailchimp_generation"]
                salesforce_generation = item["salesforce_generation"]
                any_generation = any(generation is not None for generation in (marketo_generation,mailchimp_generation,salesforce_generation))
                with tx() as con:
                    current = occurrence(con, uid, opens)
                    if not current or current["remote"] != item["remote"]:
                        continue
                    if current["delivery"] != "ready" and not (
                        any_generation and current["delivery"] == "replay"
                    ):
                        continue
                if any_generation:
                    observed = event_peer.observe(connect(origin), item["remote"], include_duration=True)
                else:
                    observed = event_peer.observe(connect(origin), item["remote"])
                state = observed["state"]
                duration = observed.get("duration")
                with tx() as con:
                    current = occurrence(con, uid, opens)
                    if (hooks and current and current["delivery"] == "ready"
                            and item["observed"] in (None, "live")):
                        if state == "waiting_for_encoder" and datetime.fromisoformat(slot["end"]) <= now:
                            if item["observed"] == "live":
                                event_webhooks.observe(con, uid, opens, "ended")
                            else:
                                con.execute("INSERT OR IGNORE INTO live_webhook_feed VALUES(?,?,'closed')", (uid, opens))
                        else:
                            event_webhooks.observe(con, uid, opens, state)
                    if (any_generation and state in ("ended", "replay")
                            and current and current["remote"] == item["remote"]
                            and current["delivery"] in ("ready", "replay")):
                        row = con.execute("SELECT revision FROM live_event WHERE id=?", (uid,)).fetchone()
                        revision = row[0] if row else None
                        for provider, module, generation in (
                            ("marketo", event_marketo_engagement, marketo_generation),
                            ("mailchimp", event_mailchimp_engagement, mailchimp_generation),
                            ("salesforce", event_salesforce_engagement, salesforce_generation),
                        ):
                            if generation is None:
                                continue
                            plan = event_contact_delivery.get(con, provider, uid, opens)
                            if (not plan or plan["generation"] != generation
                                    or plan["event_revision"] != revision):
                                continue
                            if (type(duration) in (int, float)
                                    and math.isfinite(duration)
                                    and duration > 0):
                                observed_duration = duration
                            else:
                                observed_duration = max(
                                    1,
                                    min(now.timestamp(), datetime.fromisoformat(slot["end"]).timestamp()) - opens,
                                )
                            module.complete(con, generation, now.timestamp(), observed_duration)
        except (Timeout, ValueError, requests.RequestException, KeyError):
            continue


def private_delivery(con, event, uid, opens):
    return bool(event.get("cueporch_delivery") or event["visibility"] == "private"
                or event["visibility"] in RESTRICTED_AUDIENCES
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
    local_only = origin is None
    replay_root = database.parent / "interpreted-replays"
    if local_only and not replay_root.is_dir():
        return
    with tx() as con:
        candidates = con.execute(
            """SELECT o.event,o.opens,o.remote FROM live_occurrence o JOIN live_event e ON e.id=o.event
               LEFT JOIN action a ON a.id=e.publication WHERE (? OR o.origin=?)
               AND (a.status='eligible' OR (a.id IS NULL AND json_extract(e.settings,'$.visibility')='private'))
               AND json_extract(e.settings,'$.save_replay')=1 AND o.opens<=? AND
               (o.delivery='ready' OR (o.delivery='replay' AND COALESCE((
                   SELECT enabled FROM live_caption_setting s WHERE s.event=o.event AND s.opens=o.opens
               ),json_extract(e.settings,'$.setup.captions.enabled'),0)=1 AND NOT EXISTS (
                   SELECT 1 FROM live_replay_captions c WHERE c.event=o.event AND c.opens=o.opens
               )))""",
            (local_only, origin, datetime.now(timezone.utc).timestamp()),
        ).fetchall()
    for item in candidates:
        uid, opens, remote = item
        try:
            with lock_event(database, uid, timeout=0):
                with tx() as con:
                    saved = occurrence(con, uid, opens)
                    if (saved is None or saved["remote"] != remote or saved["delivery"] not in DELIVERED
                            or (not local_only and saved["origin"] != origin)):
                        continue
                    row, event = read(con, uid)
                    if not event["save_replay"] or (event["visibility"] != "private" and
                                                    publication(con, row, event) != "eligible"):
                        continue
                    managed = private_delivery(con, event, uid, opens)
                    local = managed and event_replay_spool.available(replay_root, uid, opens, remote)
                    if local_only and not local:
                        continue
                    if saved["delivery"] == "replay":
                        event_replay_captions.queue(con, action, uid, opens)
                        continue
                    registered = library_registration.active(con, "event", uid) is not None
                host = None
                if not local:
                    host = connect(origin)
                    if event_peer.observe(host, remote)["state"] != "replay":
                        continue
                with tx() as con:
                    decision = action(con, f"live-replay-{uid}-{opens}", "replay",
                                      dict(title=event["title"], event=uid, occurrence=opens))
                    if decision in ("eligible", "waiting"):
                        event_replay_captions.queue(con, action, uid, opens)
                if decision == "eligible":
                    if host is not None and not registered and not managed:
                        host.set_public(remote, True)
                    with tx() as con:
                        con.execute("UPDATE live_occurrence SET delivery='replay' WHERE event=? AND opens=?", (uid, opens))
                        if local and con.execute("""SELECT 1 FROM live_webhook_feed
                            WHERE event=? AND occurrence=? AND state='live'""", (uid, opens)).fetchone():
                            event_webhooks.observe(con, uid, opens, "ended")
                        event_webhooks.enqueue(con, "live-event-archive-available", uid, opens, data=dict(clip=remote))
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


def viewer_url(row, event, embed, key=None, opens=None):
    if event["visibility"] == "embed_only" and not embed:
        return None
    return url_for("event_embed" if embed else "event_page", slug=row["slug"], key=key, occurrence=opens)


def summary(con, row, event, first, last):
    visible = publication(con, row, event)
    watchable = visible == "eligible" or event["visibility"] == "private"
    can_embed = watchable and event.get("embed_policy", UNRESTRICTED_EMBED)["mode"] != "disabled"
    key, _ = access_settings(con, row["id"]) if event["visibility"] == "unlisted" else (None, None)
    sessions = []
    for upcoming in event_schedule.occurrences(event["schedule"], first, last):
        target = occurrence(con, row["id"], upcoming["id"])
        program = event_program.program(con, row["id"], upcoming["id"])
        upcoming["delivery"] = target["delivery"] if target else "scheduled"
        if target and not target["origin"] and event_imported_replay.read(con, row["id"], upcoming["id"]):
            upcoming["imported_replay"] = True
        upcoming["program"] = program["state"] if program else None
        sessions.append(upcoming)
    return dict(id=row["id"], slug=row["slug"], revision=row["revision"], event=event, editable=editable(con, row["id"]),
                preview_encoder=encoder_preview_enabled(con, row["id"]),
                publication=visible, occurrences=sessions,
                watch=viewer_url(row, event, False, key) if watchable else None,
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
             sharing_access, enforce_sharing, ai_actor, hubspot, *, mailchimp, salesforce, youtube_live_connection,
             facebook_live_connection, facebook_profile_live_connection, linkedin_live_connection,
             linkedin_member_live_connection, marketo=None):
    marketo_revision = marketo.revision(signing_key) if marketo is not None else None
    media = Gateway()
    studio = Studio(database)
    rtmp_config = event_rtmp.configured()
    interpreters = InterpreterInputs(database)
    app.extensions["event_interpreter_inputs"] = interpreters
    event_rtmp.register(app, rtmp_config, studio, interpreters)
    programs = event_program.Programs(app, tx, root, database, read_works, studio)
    def replay_source(con, event, work, digest=None):
        if (event["save_replay"] and work and work["provider"] == "vimeo"
                and event_program.permitted(con, work, event["visibility"])):
            return event_imported_replay.source(root, work, digest)
        return None

    def imported_source(con, event, target, uid, opens):
        if target is None or target["origin"]:
            return None
        linked = event_imported_replay.read(con, uid, opens)
        if not linked or linked["remote"] != target["remote"]:
            return None
        work = next((item for item in read_works() if item["id"] == linked["work"]), None)
        return replay_source(con, event, work, linked["digest"])

    def advance_imported(con, uid, opens, remote):
        if event_imported_replay.advance(con, uid, opens, remote):
            event_replay_captions.queue(con, action, uid, opens)

    def publish_local_replays():
        publish_replays(tx, database, None, None, action)
        with tx() as con:
            waiting = con.execute("""SELECT r.event,r.opens,r.remote FROM live_imported_replay r
                JOIN action a ON a.id=r.approval AND a.status='eligible'
                JOIN live_occurrence o ON o.event=r.event AND o.opens=r.opens AND o.remote=r.remote
                WHERE o.delivery='archive_pending'""").fetchall()
        for uid, opens, remote in waiting:
            try:
                with lock_event(database, uid, timeout=0), tx() as con:
                    row, event = read(con, uid)
                    target = occurrence(con, uid, opens)
                    if (publication(con, row, event) in ("eligible", "private")
                            and target and target["remote"] == remote and target["delivery"] == "archive_pending"
                            and imported_source(con, event, target, uid, opens)):
                        advance_imported(con, uid, opens, remote)
            except Timeout:
                continue

    recordings = event_library_recordings.Recordings(
        app, tx, media, database, root, institution, remember_work, publish_local_replays,
    )
    stop_voice, start_voice, voice_audio = studio_voice.register(app, staff)

    def stop():
        app.extensions["account_reset_delivery"].stop()
        if stop_webhooks := app.extensions.get("stop_event_webhooks"):
            stop_webhooks()
        stop_hubspot()
        stop_hubspot_outbound()
        stop_marketo()
        stop_mailchimp()
        stop_marketo_sync()
        stop_salesforce_sync()
        stop_salesforce_outbound()
        stop_mail()
        app.extensions["stop_linkedin_live"]()
        outputs.close()
        preview_outputs.close()
        programs.close()
        interpreters.close()
        studio.close()
        captions.close()
        replay_captions.close()
        recordings.close()
        stop_voice()
        stop_scripts()

    app.extensions["stop_event_capture"] = stop
    forms = URLSafeTimedSerializer(signing_key, salt="cueporch-event-enrollment")
    attendance_links = URLSafeSerializer(signing_key, salt="cueporch-event-attendance")
    mail_origin = library_analytics.source_origin(public_url)
    stop_mail = event_mail.register(app, tx, database, mail_origin, staff, read, attendance_links)
    mail_configured = bool(mail_relay.configured(mail_origin))
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
    event_landing.register(app, tx, staff, read)
    event_landing_speakers.register(app, tx, staff)

    def enforce_territories(event):
        if not regional_availability.permits(territories(event), analytics_proxies):
            response = make_response("This event is unavailable in your region.", 403)
            response.headers["Content-Type"] = "text/plain; charset=utf-8"
            response.headers["Cache-Control"] = "no-store"
            abort(response)

    def check_territories(event):
        if territories(event)["mode"] != "anywhere" and not analytics_proxies:
            abort(422, "Connect a trusted region proxy before setting territories.")

    def viewer_enrollment(con, token, scope, released_archive):
        try:
            registered_scope = guests.loads(token, max_age=12 * 3600)
        except BadSignature:
            return False, None, None
        if isinstance(registered_scope, list) and len(registered_scope) == 6:
            joined = registered_scope[5]
            admitted = registered_scope[:5] == scope and event_attendance.active(con, joined, scope)
            return admitted, joined if admitted else None, None
        sharing = None
        if isinstance(registered_scope, list) and len(registered_scope) == 7:
            candidate = registered_scope[6]
            registered_scope = registered_scope[:5]
            if registered_scope == scope and event_registrants.sharing_active(con, candidate, scope[0], scope[2]):
                sharing = candidate
        # A submitted registration covers the other released entries of the same event playlist.
        # Media, captions and engagement continue to check the exact occurrence being requested.
        carried = (released_archive and isinstance(registered_scope, list) and len(registered_scope) == 5
                   and registered_scope[:2] == scope[:2] and registered_scope[3:] == scope[3:])
        return bool(registered_scope == scope or carried), None, sharing

    def locked(uid):
        if not re.fullmatch(r"[a-f0-9]{32}", uid):
            abort(404)
        return lock_event(database, uid)

    @app.get("/operator/live-events")
    def live_event_dashboard():
        return render_template("live_events.html", public_url=public_url, rating_labels=content_rating.DESCRIPTORS)

    @app.get("/operator/live-events/<uid>/<int:opens>/studio")
    def event_studio_page(uid, opens):
        return render_template("record_studio.html", record_only=request.args.get("mode") == "record",
                               broadcast_endpoint=url_for("start_event_capture", uid=uid, opens=opens),
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
                return dict(events=entries, overlaps=overlapping(entries), host_ready=event_peer.host_available(),
                            direct_rtmp=bool(rtmp_config))
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
            revision = uuid.uuid4().hex
            con.execute("INSERT INTO live_event VALUES(?,?,?,?,?,?)", (uid, slug, revision,
                        json.dumps(event), approval, datetime.now(timezone.utc).isoformat()))
            event_webhooks.enqueue(con, "live-event-updated", uid, marker=revision, data=dict(change="created", revision=revision))
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

    @app.get("/api/live-events/<uid>")
    def event_details(uid):
        staff()
        first, last = calendar_window()
        with tx() as con:
            row, event = read(con, uid)
            result = summary(con, row, event, first, last)
            key = access_settings(con, uid)[0] if event["visibility"] == "unlisted" else None
            for session in result["occurrences"]:
                if session["delivery"] == "replay":
                    session["watch"] = viewer_url(row, event, False, key, session["id"]) if result["watch"] else None
                    session["embed"] = viewer_url(row, event, True, key, session["id"]) if result["embed"] else None
        return result

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
                revision = uuid.uuid4().hex
                con.execute("UPDATE live_event SET revision=?,settings=?,publication=? WHERE id=?",
                            (revision, json.dumps(changed), approval, uid))
                if changed == (prior | {"title": changed["title"], "description": changed["description"]}) and access == previous_access:
                    con.execute("UPDATE live_destination_series SET event_revision=? WHERE event=? AND event_revision=?",
                                (revision, uid, row["revision"]))
                    con.execute("UPDATE facebook_live_series SET event_revision=? WHERE event=? AND event_revision=?",
                                (revision, uid, row["revision"]))
                    con.execute("UPDATE facebook_live_auto SET event_revision=? WHERE event=? AND event_revision=? AND state='queued'",
                                (revision, uid, row["revision"]))
                event_webhooks.enqueue(con, "live-event-updated", uid, marker=revision, data=dict(change="updated", revision=revision))
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

    def selected_open(event, opens):
        candidate = find_open(event, opens)
        if not candidate:
            abort(404)
        return candidate

    event_registrants.register(app, tx, staff, read, selected_open)

    def registration_intake(con, uid, opens):
        row, event = read(con, uid)
        slot = selected_open(event, opens)
        delivery = occurrence(con, uid, opens)
        form = library_registration.active(con, "event", uid)
        if (not form or event["visibility"] not in event_mail.WATCHABLE
                or (delivery and delivery["delivery"] in ("withdrawing", "withdrawn", "replay"))):
            abort(409, "Choose a current watchable occurrence with registration enabled.")
        if event_attendance.expires_at(slot) <= datetime.now(timezone.utc).timestamp():
            abort(409, "This occurrence has ended.")
        return row, slot, form

    def enroll_attendee(con, row, slot, form, body, *, source):
        uid, opens = row["id"], slot["id"]
        added = False

        def remember(email, answers):
            nonlocal added
            _, added = event_registrants.record(con, uid, opens, email, answers, source)

        registrant, created = event_attendance.enroll(
            con, uid, row["revision"], slot, form, body, on_record=remember)
        if mail_configured:
            _, rules = event_mail.settings(con, uid)
            event_mail.schedule(con, registrant["id"], opens, registrant["expires_at"], rules,
                                int(datetime.now(timezone.utc).timestamp()), confirmation=created and source == "api")
        return registrant, created, added

    @app.post("/api/live-events/<uid>/<int:opens>/registrants")
    def add_event_registrant(uid, opens):
        staff()
        body = request.get_json()
        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)
            registrant, created, _ = enroll_attendee(con, row, slot, form, body, source="api")
        address = url_for("event_attendance_join", slug=row["slug"])
        return dict(registrant, join=address + "#" + attendance_links.dumps(registrant["id"])), 201 if created else 200

    @app.post("/api/live-events/<uid>/<int:opens>/registration/import")
    def import_event_registrants(uid, opens):
        secure_site(mail_origin)
        actor = staff()
        request.max_content_length = registration_csv.MAX_BYTES + 32768
        headers, rows = registration_csv.read_file(request.files.get("attendees"))
        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)
            available = registration_csv.destinations(form)
            if request.form.get("preview") == "1":
                return private(dict(headers=headers, fields=available, mapping=registration_csv.suggested(available, headers),
                                    rows=len(rows), revision=form["revision"]))
            if request.form.get("revision", form["revision"]) != form["revision"]:
                abort(409, "The registration form changed. Reopen the CSV mapping.")
            assigned = registration_csv.mapping(request.form.get("mapping"), available, headers)
            contacts = {}
            for line, cells in rows:
                fields = registration_csv.attendee_fields(line, cells, assigned)
                contacts[fields["email"].casefold()] = (line, fields)
            added = 0
            for line, fields in contacts.values():
                try:
                    _, _, enrolled = enroll_attendee(con, row, slot, form, fields, source="csv")
                    added += int(enrolled)
                except (Conflict, UnprocessableEntity) as error:
                    abort(error.code, f"CSV line {line}: {error.description}")
            note(con, actor, "live_event_attendees_imported", uid, f"{opens}, {len(contacts)} attendees")
        return private(dict(added=added, existing=len(contacts) - added, duplicates=len(rows) - len(contacts), occurrence=opens))

    def enroll_hubspot_contacts(con, row, slot, form, contacts, *, job=None):
        added = 0
        existing = 0
        duplicates = 0
        seen = set()
        for contact in contacts:
            email = (contact.get("email") or "").strip()
            key = email.casefold()
            if not key or key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if job:
                tracked = con.execute(
                    "SELECT 1 FROM live_event_hubspot_member WHERE event=? AND opens=? AND connection=? AND segment=? AND contact_id=?",
                    (job["event"], job["opens"], job["connection"], job["segment"], contact["contact_id"]),
                ).fetchone()
                if tracked:
                    existing += 1
                    continue
            hit = con.execute(
                "SELECT id FROM live_event_registrant WHERE event=? AND opens=? AND email_key=?",
                (row["id"], slot["id"], key),
            ).fetchone()
            if hit:
                existing += 1
                continue
            fields = {
                "email": email,
                "first_name": contact.get("first_name", ""),
                "last_name": contact.get("last_name", ""),
            }
            attendee, _, enrolled = enroll_attendee(
                con, row, slot, form, fields,
                source="hubspot-scheduled" if job else "hubspot",
            )
            if enrolled:
                added += 1
            if enrolled and job:
                registrant = con.execute(
                    "SELECT id FROM live_event_registrant WHERE event=? AND opens=? AND email_key=?",
                    (row["id"], slot["id"], key),
                ).fetchone()
                event_hubspot_sync.track_member(
                    con, job, contact["contact_id"], registrant["id"], attendee["id"]
                )
        return dict(added=added, existing=existing, duplicates=duplicates)

    @app.post("/api/live-events/<uid>/<int:opens>/registration/hubspot")
    def live_event_hubspot_registration(uid, opens):
        import hubspot_crm
        from browser_account_security import json_mutation
        actor = staff()
        body = json_mutation(secure_site(mail_origin), "X-CuePorch-Integration")
        connection = hubspot
        revision = connection.revision()
        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)
            saved = (row["revision"], form["revision"])
        contacts, skipped = hubspot_crm.contacts(connection, revision, body.get("segment"))
        with tx() as con:
            connection.authorize(con, revision)
            row, slot, form = registration_intake(con, uid, opens)
            if (row["revision"], form["revision"]) != saved:
                abort(409, "The event or registration form changed. Reopen this occurrence and try again.")
            result = enroll_hubspot_contacts(con, row, slot, form, contacts)
            note(con, actor, "live_event_attendees_imported", uid, f"{opens}, {len(contacts)} HubSpot contacts")
        return private(dict(result, skipped=skipped, occurrence=opens))

    def enroll_marketo_members(con, row, slot, form, members, mapping, *, source):
        contacts = (marketo_crm.static_list_attendee(member, mapping) for member in members)
        return enroll_crm_contacts(con, row, slot, form, contacts, source=source)

    def enroll_crm_contacts(con, row, slot, form, contacts, *, source):
        added = 0
        existing = 0
        duplicates = 0
        skipped = 0
        seen = set()
        for fields in contacts:
            if fields is None:
                skipped += 1
                continue
            key = fields["email"].casefold()
            if key in seen:
                duplicates += 1
                continue
            found = con.execute(
                'SELECT 1 FROM live_event_registrant WHERE event=? AND opens=? AND email_key=?',
                (row["id"], slot["id"], key),
            ).fetchone()
            if found is not None:
                existing += 1
                seen.add(key)
                continue
            values = dict(fields)
            for k, v in values.items():
                if isinstance(v, bool):
                    values[k] = "yes" if v else "no"
            error, _ = library_registration.answers_from(form, values)
            if error:
                skipped += 1
                continue
            _, _, enrolled = enroll_attendee(con, row, slot, form, fields, source=source)
            if enrolled:
                added += 1
            seen.add(key)
        return dict(added=added, existing=existing, duplicates=duplicates, skipped=skipped)

    def marketo_import_target(body, form):
        list_id = body.get("list")
        mapping = body.get("mapping", {})
        if isinstance(list_id, bool) or not isinstance(list_id, int) or list_id <= 0:
            abort(422)
        if not isinstance(mapping, dict):
            abort(422)
        marketo_crm.static_list(marketo, list_id)
        if mapping:
            try:
                mapping = marketo_crm.validate_mapping(form, mapping, marketo_crm.lead_fields(marketo))
            except ValueError:
                abort(422)
        return list_id, mapping

    @app.post("/api/live-events/<uid>/<int:opens>/registration/marketo")
    def import_marketo_registration(uid, opens):
        actor = staff()
        from browser_account_security import json_mutation
        body = json_mutation(secure_site(mail_origin), "X-CuePorch-Integration")
        if marketo is None:
            abort(404)

        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)
            event_revision = row["revision"]
            form_revision = form["revision"]
        list_id, mapping = marketo_import_target(body, form)
        fields = [entry["name"] for entry in mapping.values()]
        members = marketo_crm.static_list_members(marketo, list_id, fields=fields)
        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)
            if row["revision"] != event_revision or form["revision"] != form_revision:
                abort(409)
            result = enroll_marketo_members(con, row, slot, form, members, mapping, source="marketo")
            if result.get("added"):
                note(con, actor, "live_event_attendees_imported", uid, f"{opens}, {result['added']} Marketo attendees")
        return private(dict(result, occurrence=opens))


    def validate_hubspot_plan(con, job):
        row, slot, form = registration_intake(con, job["event"], job["opens"])
        if (
            row["revision"] != job["event_revision"]
            or form["revision"] != job["form_revision"]
        ):
            abort(409, "The event or registration form changed.")
        return row, slot, form


    def authorize_marketo_sync(con, revision):
        if marketo_revision is None or revision != marketo_revision:
            abort(409)


    def fetch_marketo_sync(job):
        target = json.loads(job["segment"])
        mapping = target["mapping"]
        members = marketo_crm.static_list_members(
            marketo,
            target["list"],
            fields=[entry["name"] for entry in mapping.values()],
        )
        return members, mapping


    def apply_marketo_sync(con, job, members, mapping):
        row, slot, form = validate_hubspot_plan(con, job)
        result = enroll_marketo_members(
            con,
            row,
            slot,
            form,
            members,
            mapping,
            source="marketo-scheduled",
        )
        if result["added"]:
            note(
                con,
                job["actor"],
                "live_event_attendees_imported",
                job["event"],
                f"{job['opens']}, {result['added']} Marketo attendees",
            )
        return dict(result, occurrence=job["opens"])

    def marketo_sync_receipt(job):
        receipt = event_hubspot_sync.receipt(job)
        if job:
            target = json.loads(receipt.pop("segment"))
            receipt["list"] = target["list"]
            receipt["mapping"] = {
                key: descriptor["name"]
                for key, descriptor in target["mapping"].items()
            }
            if job["connection"] != marketo_revision:
                receipt.update(
                    enabled=False,
                    state="problem",
                    problem="Marketo configuration changed. Enable synchronization again.",
                )
        return dict(receipt, available=marketo is not None)

    def apply_hubspot_plan(con, job, contacts, skipped, membership):
        row, slot, form = validate_hubspot_plan(con, job)
        active = membership["active"]
        removed, restored = event_hubspot_sync.reconcile_members(con, job, membership["ids"]) if active else (0, 0)
        result = enroll_hubspot_contacts(con, row, slot, form, contacts, job=job if active else None)
        if result.get("added"):
            note(
                con,
                job["actor"],
                "live_event_attendees_imported",
                job["event"],
                f"{job['opens']}, {len(contacts)} HubSpot contacts",
            )
        return dict(result, skipped=skipped, occurrence=job["opens"], removed=removed, restored=restored)

    def save_marketo_sync(uid, opens, actor, body):
        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)
        list_id, mapping = marketo_import_target(body, form)
        with tx() as con:
            current_row, current_slot, current_form = registration_intake(con, uid, opens)
            if current_row["revision"] != row["revision"] or current_form["revision"] != form["revision"]:
                abort(409)
            now = int(datetime.now(timezone.utc).timestamp())
            closes = event_attendance.expires_at(current_slot)
            if now >= min(closes, opens + 7200):
                abort(409)
            zone = json.loads(current_row["settings"])["schedule"]["timezone"]
            first_due = max(now, event_hubspot_sync.local_midnight(opens, zone))
            return event_hubspot_sync.set_sync(con, uid, opens, segment=json.dumps({"list": list_id, "mapping": mapping}), connection=marketo_revision, actor=actor, event_revision=current_row["revision"], form_revision=current_form["revision"], closes=closes, zone=zone, now=now, first_due=first_due, provider="marketo")

    @app.route("/api/live-events/<uid>/<int:opens>/registration/marketo/sync", methods=["GET", "PUT", "DELETE"])
    def schedule_marketo_registration(uid, opens):
        actor = staff()
        secure = secure_site(mail_origin)
        if request.method == "GET":
            same_origin(secure)
            with tx() as con:
                row, event = read(con, uid)
                selected_open(event, opens)
                return private(marketo_sync_receipt(event_hubspot_sync.get(con, uid, opens, provider="marketo")))
        from browser_account_security import json_mutation
        body = json_mutation(secure, "X-CuePorch-Integration")
        if request.method == "DELETE":
            with tx() as con:
                row, event = read(con, uid)
                selected_open(event, opens)
                event_hubspot_sync.remove(con, uid, opens, provider="marketo")
            return private(marketo_sync_receipt(None))
        if marketo is None:
            abort(404)
        return private(marketo_sync_receipt(save_marketo_sync(uid, opens, actor, body)))

    @app.route("/api/live-events/<uid>/<int:opens>/registration/hubspot/sync", methods=["GET", "PUT", "DELETE"])
    def schedule_hubspot_registration(uid, opens):
        actor = staff()
        secure = secure_site(mail_origin)
        if request.method == "GET":
            same_origin(secure)
            with tx() as con:
                row, event = read(con, uid)
                selected_open(event, opens)
                return private(event_hubspot_sync.receipt(event_hubspot_sync.get(con, uid, opens)))
        from browser_account_security import json_mutation
        body = json_mutation(secure, "X-CuePorch-Integration")
        if request.method == "DELETE":
            with tx() as con:
                row, event = read(con, uid)
                selected_open(event, opens)
                event_hubspot_sync.remove(con, uid, opens)
            return private(dict(enabled=False, state="off"))
        import hubspot_crm
        segment = body.get("segment")
        if not hubspot_crm.segment_id(segment):
            abort(400)
        revision = hubspot.revision()
        with tx() as con:
            hubspot.authorize(con, revision)
            row, slot, form = registration_intake(con, uid, opens)
            now = int(datetime.now(timezone.utc).timestamp())
            closes = event_attendance.expires_at(slot)
            if now >= min(closes, opens + 7200):
                abort(409)
            zone = json.loads(row["settings"])["schedule"]["timezone"]
            event_hubspot_sync.set_sync(con, uid, opens, segment=segment, connection=revision, actor=actor, event_revision=row["revision"], form_revision=form["revision"], closes=closes, zone=zone, now=now)
            return private(event_hubspot_sync.receipt(event_hubspot_sync.get(con, uid, opens)))

    @app.route("/api/live-events/<uid>/<int:opens>/registration/hubspot/outbound", methods=["GET", "PUT", "DELETE"])
    def configure_hubspot_outbound(uid, opens):
        staff()
        secure = secure_site(mail_origin)
        available = hubspot_crm.outbound_available(hubspot)
        engagement_available = hubspot_crm.engagement_available(hubspot)
        if request.method == "GET":
            same_origin(secure)
            with tx() as con:
                row, event = read(con, uid)
                selected_open(event, opens)
                return private(dict(
                    event_hubspot_outbound.receipt(con, uid, opens),
                    available=available,
                    engagement_available=engagement_available,
                ))
        from browser_account_security import json_mutation
        body = json_mutation(secure, "X-CuePorch-Integration")
        if request.method == "DELETE":
            with tx() as con:
                row, event = read(con, uid)
                selected_open(event, opens)
                event_hubspot_outbound.disable(con, uid, opens)
            return private(dict(enabled=False, state="off"))
        if not available:
            abort(400)
        engagement_event = body.get("engagement_event", "")
        if not isinstance(engagement_event, str):
            abort(400)
        if engagement_event and (not engagement_available or not event_hubspot_engagement.name_valid(engagement_event)):
            abort(400)
        segment = body.get("segment")
        subscription = body.get("subscription")
        if not hubspot_crm.segment_id(segment) or not hubspot_crm.segment_id(subscription):
            abort(400)
        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)
            event_revision = row["revision"]
            form_revision = form["revision"]
            if engagement_event and not form["attendance_analytics"]:
                abort(400)
        revision = hubspot.revision()
        if not hubspot_crm.outbound_segment(hubspot, revision, segment):
            abort(400)
        chosen = next((item for item in hubspot_crm.subscriptions(hubspot, revision) if item["id"] == subscription), None)
        if chosen is None:
            abort(400)
        unit = chosen.get("business_unit", 0)
        if not isinstance(unit, int) or isinstance(unit, bool) or unit < 0:
            abort(400)
        with tx() as con:
            hubspot.authorize(con, revision)
            row, slot, form = registration_intake(con, uid, opens)
            if row["revision"] != event_revision or form["revision"] != form_revision:
                abort(409, "The event or registration form changed.")
            plan = event_hubspot_outbound.set_destination(
                con,
                uid,
                opens,
                connection=revision,
                event_revision=row["revision"],
                form_revision=form["revision"],
                segment=segment,
                subscription=subscription,
                business_unit=unit,
                deadline=event_attendance.expires_at(slot),
            )
            if engagement_event:
                event_hubspot_engagement.configure(con, plan["generation"], engagement_event)
            return private(dict(
                event_hubspot_outbound.receipt(con, uid, opens),
                available=available,
                engagement_available=engagement_available,
            ))

    def set_marketo_destination(uid, opens, body):
        if marketo is None:
            abort(404)
        program_id = body.get('program')
        if type(program_id) is not int or program_id <= 0:
            abort(422)
        engagement = body.get('engagement', False)
        if type(engagement) is not bool:
            abort(422)
        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)
            if engagement and not form.get('attendance_analytics'):
                abort(409, description='attendance_analytics is required when engagement is enabled')
            row_revision, form_revision = row['revision'], form['revision']
        try:
            marketo_crm.program(marketo, program_id, engagement=engagement)
            mapping = body.get('mapping', {})
            mapping = marketo_crm.validate_mapping(form, mapping, marketo_crm.lead_fields(marketo))
        except ValueError:
            abort(422)
        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)
            if row['revision'] != row_revision or form['revision'] != form_revision:
                abort(409)
            event_contact_delivery.set_destination(
                con, 'marketo', uid, opens,
                provider_revision=marketo_revision,
                event_revision=row['revision'],
                form_revision=form['revision'],
                deadline=int(event_attendance.expires_at(slot)),
                destination={'program': program_id, 'mapping': mapping, 'engagement': engagement},
            )

    @app.route('/api/live-events/<uid>/<int:opens>/registration/marketo/outbound', methods=['GET', 'PUT', 'DELETE'])
    def marketo_outbound(uid, opens):
        staff()
        secure = secure_site(mail_origin)
        if request.method == 'GET':
            same_origin(secure)
        else:
            from browser_account_security import json_mutation
            body = json_mutation(secure, 'X-CuePorch-Integration')
            if request.method == 'PUT':
                set_marketo_destination(uid, opens, body)
        with tx() as con:
            row, event = read(con, uid)
            selected_open(event, opens)
            if request.method == 'DELETE':
                event_contact_delivery.disable(con, 'marketo', uid, opens)
            return private(event_contact_delivery.receipt(con, 'marketo', uid, opens, marketo_revision))

    def validate_hubspot_engagement(con, plan):
        row, event = read(con, plan["event"])
        slot = selected_open(event, plan["opens"])
        form = library_registration.active(con, "event", row["id"])
        delivery = occurrence(con, row["id"], slot["id"])
        if (row["revision"] != plan["event_revision"]
                or not form
                or form["revision"] != plan["form_revision"]
                or not form["attendance_analytics"]
                or event["visibility"] not in event_mail.WATCHABLE
                or event_attendance.expires_at(slot) != plan["deadline"]
                or (delivery and delivery["delivery"] in ("withdrawing", "withdrawn"))):
            abort(409)
        return event["title"]

    event_mailchimp_registration.register_routes(app, tx, staff, mail_origin, mailchimp, read, selected_open, registration_intake)
    stop_mailchimp = event_mailchimp_registration.start(app, tx, database, mailchimp, validate_hubspot_plan, engagement_validate=validate_hubspot_engagement)
    stop_marketo = event_contact_delivery.start_marketo(
        app, tx, database, marketo, marketo_revision, validate_hubspot_plan,
        engagement_validate=validate_hubspot_engagement,
    )
    stop_hubspot = event_hubspot_sync.start(
        app, tx, database, hubspot, validate_hubspot_plan, apply_hubspot_plan,
    )
    stop_marketo_sync = (
        event_hubspot_sync.start(
            app,
            tx,
            database,
            marketo,
            validate_hubspot_plan,
            apply_marketo_sync,
            provider="marketo",
            fetch=fetch_marketo_sync,
            authorize=authorize_marketo_sync,
        )
        if marketo is not None
        else lambda: None
    )
    salesforce_apply = event_salesforce_sync.register(
        app, tx, staff, mail_origin, salesforce, read, selected_open, registration_intake, enroll_crm_contacts,
    )

    def fetch_salesforce_sync(job):
        return (salesforce_crm.campaign_members(salesforce, job["connection"], job["segment"]),)

    def apply_salesforce_sync(con, job, fields):
        row, slot, form = validate_hubspot_plan(con, job)
        return salesforce_apply(con, row, slot, form, fields, job["actor"], "salesforce-scheduled")

    stop_salesforce_sync = event_hubspot_sync.start(
        app, tx, database, salesforce, validate_hubspot_plan, apply_salesforce_sync,
        provider="salesforce", fetch=fetch_salesforce_sync,
    ) if salesforce.configured else lambda: None

    event_salesforce_registration.register_routes(
        app, tx, staff, mail_origin, salesforce, read, selected_open, registration_intake,
        validate_hubspot_engagement)
    stop_salesforce_outbound = event_salesforce_registration.start(
        app, tx, database, salesforce, validate_hubspot_plan,
        engagement_validate=validate_hubspot_engagement,
    ) if salesforce.configured else lambda: None

    stop_hubspot_outbound = event_hubspot_outbound.start(
        app, tx, database, hubspot, validate_hubspot_plan,
        engagement_validate=validate_hubspot_engagement if hubspot_crm.engagement_available(hubspot) else None,
    ) if hubspot_crm.outbound_available(hubspot) else lambda: None

    @app.get("/api/live-events/<uid>/registrants/<registrant>")
    def event_registrant_details(uid, registrant):
        staff()
        with tx() as con:
            read(con, uid)
            found = con.execute("SELECT * FROM live_event_registrant WHERE event=? AND id=?", (uid, registrant)).fetchone()
            if found:
                return event_registrants.describe(found, con)
            found = con.execute("""SELECT r.email,r.registered,r.answers FROM live_webhook_registrant a
                                   JOIN viewer_registrant r ON r.subject=a.event AND r.email_key=a.email_key
                                   WHERE a.id=? AND a.event=? AND r.kind='event'""", (registrant, uid)).fetchone()
            if not found:
                abort(404)
            return dict(id=registrant, email=found["email"], registered=found["registered"], answers=json.loads(found["answers"]))

    @app.delete("/api/live-events/<uid>/<int:opens>/registrants/<registrant>/join")
    def revoke_event_join(uid, opens, registrant):
        staff()
        with tx() as con:
            _, event = read(con, uid)
            selected_open(event, opens)
            event_attendance.revoke(con, uid, opens, registrant)
        return "", 204

    @app.route("/events/<slug>/join", methods=["GET", "POST"])
    def event_attendance_join(slug):
        origin = library_analytics.source_origin(public_url)
        secure = secure_site(origin)
        if request.method in ("GET", "HEAD"):
            return private(render_template("event_attendance_join.html"))
        same_origin(secure)
        body = request.get_json()
        supplied = body.get("token") if isinstance(body, dict) else None
        joined = event_attendance.decode(attendance_links, supplied) if isinstance(supplied, str) and len(supplied) <= 150 else None
        if not joined:
            abort(404)
        with tx() as con:
            row, event = read(con, slug, True)
            if event["visibility"] in ("private", "embed_only") or publication(con, row, event) != "eligible":
                abort(404)
            enforce_territories(event)
            form = library_registration.active(con, "event", row["id"])
            entry = con.execute("SELECT opens,expires FROM live_attendance_link WHERE id=?", (joined,)).fetchone()
            if not form or not entry:
                abort(404)
            opens = entry["opens"]
            scope = [row["id"], row["revision"], opens, form["revision"], False]
            selected_open(event, opens)
            delivery = occurrence(con, row["id"], opens)
            if not event_attendance.active(con, joined, scope) or (delivery and delivery["delivery"] in ("withdrawing", "withdrawn")):
                abort(404)
            key = access_settings(con, row["id"])[0] if event["visibility"] == "unlisted" else None
            watch = viewer_url(row, event, False, key, opens)
        response = private(dict(watch=watch))
        remaining = max(0, entry["expires"] - int(datetime.now(timezone.utc).timestamp()))
        response.set_cookie("cueporch_event_" + row["id"], supplied, max_age=remaining, httponly=True,
                            secure=secure, samesite="Lax", path=viewer_url(row, event, False))
        return response

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

    def require_event_audience(con, row, event):
        visible = publication(con, row, event)
        if visible == "eligible":
            return
        if visible == "private" and event["visibility"] == "private":
            secure = secure_site(mail_origin, required=False)
            if secure is not None and current_team_member(con, secure):
                return
        abort(404)

    def audience_chat(con, slug, opens):
        row, event = read(con, slug, True)
        slot = selected_open(event, opens)
        delivery = occurrence(con, row["id"], opens)
        require_event_audience(con, row, event)
        if delivery and delivery["delivery"] in ("withdrawing", "withdrawn"):
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
            admitted, _, _ = viewer_enrollment(con, request.headers.get("X-CuePorch-Event-Viewer", ""),
                                                scope + [embed], carried_grant)
            if not admitted:
                abort(404)
        now = datetime.now(timezone.utc)
        active = now < datetime.fromisoformat(slot["end"]) and (not delivery or delivery["delivery"] != "replay")
        return scope, active

    audience_session = event_chat.register(app, tx, staff, signing_key, operator_chat, audience_chat)
    def outbound_activity_sharing(con, scope, *, include_marketo=True):
        if not library_analytics.permitted():
            return None

        admitted, _, sharing = viewer_enrollment(
            con,
            request.headers.get("X-CuePorch-Event-Viewer", ""),
            scope + [request.headers.get("X-CuePorch-Chat-Embed") == "yes"],
            False,
        )

        if not admitted or not sharing:
            return None

        salesforce_revision = event_salesforce_registration.connection_revision(con, salesforce)
        if salesforce_revision:
            matched = con.execute(
                "SELECT 1 FROM live_event_salesforce_engagement j "
                "JOIN live_event_contact_plan p ON p.generation=j.generation "
                "WHERE j.sharing=? AND j.state='queued' AND p.provider='salesforce' "
                "AND p.provider_revision=? AND p.event=? AND p.event_revision=? "
                "AND p.opens=? AND p.form_revision=? "
                "AND json_extract(p.destination, '$.engagement_details')=1",
                (sharing, salesforce_revision, *scope),
            ).fetchone()
            if matched:
                return sharing
        if marketo_revision and include_marketo:
            row = con.execute(
                "SELECT 1 FROM live_event_marketo_engagement j "
                "JOIN live_event_contact_plan p ON p.generation=j.generation "
                "WHERE j.sharing=? AND j.state='queued' AND p.provider='marketo' "
                "AND p.provider_revision=? AND p.event=? AND p.event_revision=? "
                "AND p.opens=? AND p.form_revision=?",
                (sharing, marketo_revision, *scope),
            ).fetchone()
            if row:
                return sharing

        mailchimp_connection = mailchimp.connection(con)
        mailchimp_revision = mailchimp_connection["revision"] if mailchimp_connection else None
        if mailchimp_revision:
            row = con.execute(
                "SELECT 1 FROM live_event_mailchimp_engagement j "
                "JOIN live_event_contact_plan p ON p.generation=j.generation "
                "WHERE j.sharing=? AND j.state='queued' AND p.provider='mailchimp' "
                "AND p.provider_revision=? AND p.event=? AND p.event_revision=? "
                "AND p.opens=? AND p.form_revision=?",
                (sharing, mailchimp_revision, *scope),
            ).fetchone()
            if row:
                return sharing

        return None

    event_engagement.register(
        app,
        tx,
        staff,
        audience_session,
        operator_chat,
        audience_chat,
        share_activity=outbound_activity_sharing,
        share_upvotes=lambda con, scope: outbound_activity_sharing(
            con, scope, include_marketo=False
        ),
    )
    event_scenes.register(app, tx, staff, root, read_works, operator_chat)
    event_slide_decks.register(app, tx, staff, database, operator_chat)
    event_speakers.register(app, tx, staff, speaker_access, database, start_voice, voice_audio)
    event_captions.register(app, tx, staff, operator_chat, audience_chat, ai_actor)
    captions = event_captions.LiveCaptionJobs(tx, action, media, caption_access, database)
    event_replay_captions.register(app, tx, staff, action, operator_chat, audience_chat)
    def imported_caption_source(con, uid, opens, remote):
        _, event = read(con, uid)
        target = occurrence(con, uid, opens)
        if target and target["remote"] == remote and target["delivery"] == "replay":
            asset = imported_source(con, event, target, uid, opens)
            if asset:
                return asset["path"]
        return None

    replay_captions = event_replay_captions.ReplayCaptionJobs(app, tx, media, database, imported_caption_source)

    def can_simulcast(con, uid, event):
        return (event["visibility"] == "public" and territories(event)["mode"] == "anywhere"
                and library_registration.active(con, "event", uid) is None)

    def destination_access(con, uid, opens):
        _, event = read(con, uid)
        selected_open(event, opens)
        return can_simulcast(con, uid, event)

    def provider_creation_access(con, uid, opens):
        row, event = read(con, uid)
        slot = selected_open(event, opens)
        current = occurrence(con, uid, opens)
        if (not can_simulcast(con, uid, event) or publication(con, row, event) != "eligible"
                or datetime.fromisoformat(slot["end"]) <= datetime.now(timezone.utc)
                or (current and current["delivery"] in ("withdrawing", "withdrawn", "replay"))):
            abort(409, "Creating a provider broadcast requires an eligible public event that has not ended.")
        return row["revision"], event["title"], slot

    def outgoing_programs(con):
        now = datetime.now(timezone.utc)
        candidates = {}
        rows = con.execute("""SELECT o.*,e.settings FROM live_occurrence o JOIN live_event e ON e.id=o.event
            JOIN action a ON a.id=e.publication WHERE o.delivery='ready' AND o.opens<=? AND a.status='eligible'
            AND EXISTS (SELECT 1 FROM live_simulcast_destination d WHERE d.event=o.event AND d.enabled=1
            AND (d.opens IS NULL OR d.opens=o.opens))""", (int(now.timestamp()),))
        for row in rows:
            uid, opens = row["event"], row["opens"]
            event = json.loads(row["settings"])
            slot = find_open(event, opens)
            if slot and datetime.fromisoformat(slot["end"]) > now and can_simulcast(con, uid, event):
                targets = con.execute(
                    """SELECT d.* FROM live_simulcast_destination d
                       LEFT JOIN workspace_integration_connection p ON p.provider=d.provider AND p.revision=d.revision
                       LEFT JOIN live_simulcast_key_series s ON s.destination=d.id
                       WHERE d.event=? AND d.enabled=1 AND (d.opens IS NULL OR d.opens=?)
                       AND (d.provider IS NULL OR p.provider IS NOT NULL)
                       AND (s.destination IS NULL OR s.start_opens<=?)
                       AND NOT EXISTS (SELECT 1 FROM live_simulcast_key_skip k WHERE k.destination=d.id AND k.opens=?)""",
                    (uid, opens, opens, opens),
                ).fetchall()
                if targets:
                    candidates[uid, opens] = (row["origin"], row["remote"], targets, None)
        return candidates

    outputs = event_simulcast.Simulcast(app, tx, media, database, signing_key, outgoing_programs)
    event_simulcast.register(
        app, tx, staff, destination_access, outputs, sharing_access, enforce_sharing,
        youtube_live_connection, facebook_live_connection, facebook_profile_live_connection, linkedin_live_connection,
        linkedin_member_live_connection, provider_creation_access,
    )

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
                        if not preview:
                            event_webhooks.enqueue(con, "live-event-clip-created", uid, opens, data=dict(clip=remote))
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

    @app.route("/api/live-events/<uid>/<int:opens>/imported-replay", methods=["GET", "PUT"])
    def event_imported_recording(uid, opens):
        staff()
        try:
            with locked(uid), tx() as con:
                row, event = read(con, uid)
                slot = selected_open(event, opens)
                target = occurrence(con, uid, opens)
                linked = event_imported_replay.read(con, uid, opens)
                program = event_program.program(con, uid, opens)
                busy = program and program["state"] in (*event_program.PENDING, *event_program.ACTIVE)
                available = (event["save_replay"] and not target and not busy
                             and datetime.fromisoformat(slot["end"]) <= datetime.now(timezone.utc)
                             and publication(con, row, event) in ("private", "eligible"))
                attachment = None
                choices = []
                state = "available" if available else "unavailable"
                works = read_works() if available or linked else []
                if request.method == "PUT":
                    body = request.get_json()
                    if not isinstance(body, dict) or set(body) != {"work", "digest"}:
                        abort(422, "Choose a matching Vimeo recording from the Library.")
                    if linked is None:
                        if not available:
                            abort(409, "Choose a past occurrence with replay enabled and no existing recording.")
                        work = next((item for item in works if item["id"] == body["work"]), None)
                        asset = replay_source(con, event, work)
                        if asset is None or asset["digest"] != body["digest"]:
                            abort(409, "This recording changed or is unavailable for this event audience.")
                        remote = event_imported_replay.attach(con, action, uid, opens, event["title"], work["id"], asset)
                        advance_imported(con, uid, opens, remote)
                        linked = event_imported_replay.read(con, uid, opens)
                        target = occurrence(con, uid, opens)
                        available = False
                    elif body["work"] != linked["work"] or body["digest"] != linked["digest"]:
                        abort(409, "An imported recording is already attached to this occurrence.")
                if linked and target:
                    decision = con.execute("SELECT status FROM action WHERE id=?", (linked["approval"],)).fetchone()
                    state = target["delivery"]
                    if state == "archive_pending" and decision and decision["status"] == "rejected":
                        state = "rejected"
                    work = next((item for item in works if item["id"] == linked["work"]), None)
                    attachment = dict(work=linked["work"], digest=linked["digest"],
                                      title=title_for(con, work) if work else "Library recording")
                if available:
                    for work in works:
                        asset = replay_source(con, event, work)
                        if asset:
                            choices.append(dict(work=work["id"], digest=asset["digest"], title=title_for(con, work)))
                    choices.sort(key=lambda item: item["title"].casefold())
                return private(dict(state=state, editable=available, attachment=attachment, choices=choices))
        except Timeout:
            abort(409, "Another operation is preparing this event.")

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
        backup_port = body.get("backup_port")
        height = body.get("height", 720) if backup_port is not None else None
        try:
            endpoint = srt_endpoint(body.get("interface"), body.get("port"))
            backup = srt_endpoint(body.get("interface"), backup_port) if backup_port is not None else None
        except ValueError as error:
            abort(422, str(error))
        if backup == endpoint:
            abort(422, "Choose different UDP ports for the primary and backup encoders.")
        if backup and (type(height) is not int or height not in (360, 720, 1080, 2160)):
            abort(422, "Choose a supported output height for the backup programme.")
        endpoints = endpoint, backup
        try:
            with locked(uid):
                prior = studio.srt_credentials(uid, opens, endpoints)
                if prior:
                    if prior.get("output_height") != height:
                        abort(409, "Stop the current SRT listeners before changing the output height.")
                    with tx() as con:
                        delivery = occurrence(con, uid, opens)
                        return dict(prior, preview=bool(delivery and delivery["delivery"] == "preview"))
                end_at = None
                if backup:
                    with tx() as con:
                        _, event = read(con, uid)
                        shift = selected_open(event, opens)
                        if body.get("preview") or encoder_preview_enabled(con, uid):
                            abort(409, "Use the private encoder preview or turn off preview for this event.")
                    if direct_window(uid, {"preview": False}) is None:
                        abort(409, "Choose Low latency and a DVR window of four to 120 seconds.")
                    end_at = datetime.fromisoformat(shift["end"]).timestamp()
                connection = provision_event(uid, opens, browser=True, preview=False if backup else body.get("preview"))
                window = direct_window(uid, connection)
                recording = None
                if window is not None:
                    with tx() as con:
                        _, event = read(con, uid)
                        target = occurrence(con, uid, opens) if event["save_replay"] else None
                    if target is not None:
                        recording = event_replay_spool.reserve(
                            recordings.replay_root, uid, opens, target["remote"], allow_discontinuity=backup is not None,
                        )
                try:
                    return dict(studio.srt(uid, opens, connection, endpoints, window, height, end_at, recording=recording),
                                preview=connection["preview"])
                except (OSError, ValueError):
                    if recording is not None:
                        recording.finish()
                    raise
        except Timeout:
            abort(409, "Another operation is preparing this event.")
        except BusyError as error:
            abort(409, str(error))
        except (OSError, ValueError):
            abort(502, "Could not start the SRT listener.")

    @app.route("/api/live-events/<uid>/<int:opens>/rtmp", methods=["POST", "DELETE"])
    def event_rtmp_relay(uid, opens):
        staff()
        if not rtmp_config:
            abort(409, "This station has no low-latency RTMP relay.")
        try:
            with locked(uid):
                with tx() as con:
                    _, event = read(con, uid)
                    shift = selected_open(event, opens)
                    if request.method == "POST" and encoder_preview_enabled(con, uid):
                        abort(409, "Use the private encoder preview or turn off preview for this event.")
                if request.method == "DELETE":
                    interpreters.stop_all(uid, opens)
                    studio.close_rtmp(uid, opens)
                    return "", 204
                window = direct_window(uid, {"preview": False})
                if window is None:
                    abort(409, "Choose Low latency and a DVR window of four to 120 seconds.")
                body = request.get_json(silent=True)
                if body is None:
                    body = {}
                if (not isinstance(body, dict) or type(body.get("backup", False)) is not bool or
                        type(body.get("height", 720)) is not int or body.get("height", 720) not in (360, 720, 1080, 2160)):
                    abort(422, "Choose whether to prepare a backup encoder and a supported output height.")
                height = body.get("height", 720) if body.get("backup", False) else None
                prior = studio.rtmp_credentials(uid, opens)
                if prior:
                    if prior.get("output_height") != height:
                        abort(409, "Stop the current RTMP input before changing its encoder setup.")
                    return private(prior)
                connection = provision_event(uid, opens, browser=True, preview=False)
                end_at = datetime.fromisoformat(shift["end"]).timestamp()
                recording = None
                if event["save_replay"]:
                    with tx() as con:
                        target = occurrence(con, uid, opens)
                    recording = event_replay_spool.reserve(
                        recordings.replay_root, uid, opens, target["remote"], allow_discontinuity=height is not None,
                    )
                try:
                    return private(studio.rtmp(uid, opens, connection, rtmp_config, window, height, end_at, recording))
                except (OSError, ValueError):
                    if recording is not None:
                        recording.finish()
                    raise
        except Timeout:
            abort(409, "Another operation is preparing this event.")
        except BusyError as error:
            abort(409, str(error))
        except (OSError, ValueError):
            abort(502, "Could not start the low-latency RTMP relay.")

    @app.route("/api/live-events/<uid>/<int:opens>/interpreters", methods=["GET", "POST"])
    @app.delete("/api/live-events/<uid>/<int:opens>/interpreters/<language>")
    def event_interpreter_input(uid, opens, language=None):
        staff()
        try:
            with locked(uid):
                with tx() as con:
                    _, event = read(con, uid)
                    shift = selected_open(event, opens)
                    target = occurrence(con, uid, opens)
                if request.method == "GET":
                    return private(dict(inputs=interpreters.list(uid, opens)))
                if request.method == "DELETE":
                    interpreters.stop_input(uid, opens, language_tag(language))
                    return "", 204
                body = request.get_json(silent=True)
                if not isinstance(body, dict):
                    abort(422, "Enter interpreter details.")
                language = language_tag(body.get("language"))
                if language == "und":
                    abort(422, "Choose a language for the interpreter.")
                name = text_field(body.get("name"), "an interpreter name", 50, True)
                deadline = datetime.fromisoformat(shift["end"]).timestamp()
                parent = studio.interpreter_room(uid, opens)
                if (not rtmp_config or not parent or not target or target["delivery"] != "ready"
                        or deadline <= datetime.now(timezone.utc).timestamp()):
                    abort(409, "Prepare low-latency RTMP or SRT for this occurrence before adding an interpreter.")
                return private(interpreters.prepare(uid, opens, parent, rtmp_config, name, language, deadline).credentials())
        except Timeout:
            abort(409, "Another operation is preparing this event.")
        except BusyError as error:
            abort(409, str(error))
        except OSError:
            abort(502, "Could not prepare the interpreter input.")

    @app.route("/api/live-events/<uid>/<int:opens>/dash", methods=["POST", "DELETE"])
    def event_dash_relay(uid, opens):
        staff()
        secure = secure_site(mail_origin)
        try:
            with locked(uid):
                with tx() as con:
                    _, event = read(con, uid)
                    shift = selected_open(event, opens)
                    preview = encoder_preview_enabled(con, uid)
                if request.method == "DELETE":
                    studio.close_dash(uid, opens)
                    return "", 204
                window = event.get("dvr_seconds", 120)
                if event["latency"] != "fail-safe" or not 4 <= window <= 120:
                    abort(409, "Choose Fail-safe with a DVR window of four to 120 seconds.")
                if preview:
                    abort(409, "Use the private encoder preview or turn off preview for this event.")
                room = studio.dash_contribution(uid, opens)
                if room:
                    settings = room.credentials()
                else:
                    encoder = provision_event(uid, opens, browser=True, preview=False)
                    end_at = datetime.fromisoformat(shift["end"]).timestamp() + event_dash_buffer.TARGET_DELAY + 300
                    settings = studio.dash(uid, opens, encoder, window, end_at)
                origin = mail_origin if secure and mail_origin.startswith("https://") else (
                    f"https://{request.host}" if secure else request.host_url.rstrip("/"))
                upload = url_for("dash_ingest", uid=uid, opens=opens, receiver=settings.pop("receiver"), filename="feed.mpd")
                return private(settings | {"manifest": origin + upload})
        except Timeout:
            abort(409, "Another operation is preparing this event.")
        except BusyError as error:
            abort(409, str(error))
        except (OSError, ValueError):
            abort(502, "Could not start the Fail-safe contribution.")

    @app.route("/event-ingest/<uid>/<int:opens>/<receiver>/<filename>", methods=["PUT", "POST"])
    def dash_ingest(uid, opens, receiver, filename):
        secure_site(mail_origin)
        room = studio.dash_contribution(uid, opens)
        if not room or receiver != room.playback.identity:
            abort(404)
        authorization = request.headers.get("Authorization", "")
        if not hmac.compare_digest(authorization.encode("utf8"), ("Bearer " + room.key).encode("ascii")):
            abort(403)
        if filename != "feed.mpd" and not (event_dash_format.INIT_FILE.fullmatch(filename) or
                                           event_dash_format.MEDIA_FILE.fullmatch(filename)):
            abort(404)
        try:
            request.max_content_length = (
                event_dash_format.MAX_MANIFEST if filename == "feed.mpd" else event_dash_buffer.MAX_FRAGMENT_BYTES)
            payload = request.get_data(cache=False)
            if filename == "feed.mpd":
                room.playback.publish(payload)
            else:
                room.playback.put(filename, payload)
        except FileNotFoundError:
            abort(404)
        except event_dash_buffer.FullBuffer as error:
            return dict(error=str(error)), 507
        except event_dash_buffer.ConflictingFragment as error:
            abort(409, str(error))
        except ValueError as error:
            abort(422, str(error))
        return "", 204

    @app.get("/api/live-events/<uid>/<int:opens>/health")
    def event_health(uid, opens):
        staff()
        with tx() as con:
            _, event = read(con, uid)
            selected_open(event, opens)
            target = occurrence(con, uid, opens)
            managed = private_delivery(con, event, uid, opens) or library_registration.active(con, "event", uid)
            asset = imported_source(con, event, target, uid, opens)
        local = studio.health(uid, opens)
        if request.args.get("source") == "relay":
            return dict(encoder=local)
        if target and not target["origin"] and target["delivery"] in ("archive_pending", "replay"):
            if not asset:
                return dict(state="unavailable", encoder=local)
            released = target["delivery"] == "replay"
            with tx() as con:
                viewers = event_analytics.current(con, uid, opens, "replay") if released else 0
            return dict(state="replay" if released else "preparing_replay", public=False,
                        viewers=viewers, duration=asset["duration"], encoder=local)
        if target is None or target["delivery"] not in DELIVERED:
            return dict(state=target["delivery"] if target else "scheduled", encoder=local)
        archive = (event_replay_spool.available(recordings.replay_root, uid, opens, target["remote"])
                   if managed and event["save_replay"] else None)
        if archive is not None:
            released = target["delivery"] == "replay"
            with tx() as con:
                viewers = event_analytics.current(con, uid, opens, "replay") if released else 0
            return dict(state="replay" if released else "preparing_replay", public=False,
                        viewers=viewers, duration=archive.duration, encoder=local)
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
                    attached = event_imported_replay.read(con, uid, opens) if not target["origin"] else None
                    if attached and attached["remote"] == target["remote"]:
                        supersede_publication(con, attached["approval"])
                    con.execute("UPDATE live_occurrence SET delivery='withdrawing' WHERE event=? AND opens=?",
                                (uid, opens))
                interpreters.stop_all(uid, opens)
                studio.withdraw(uid, opens)
                if target["origin"]:
                    with event_peer.connected_host(target["origin"]) as host:
                        video = host.call("GET", "/api/v1/videos/" + target["remote"]).json()
                        if video["privacy"]["id"] != 3:
                            host.set_public(target["remote"], False)
                with tx() as con:
                    con.execute("UPDATE live_occurrence SET delivery='withdrawn' WHERE event=? AND opens=?",
                                (uid, opens))
                    if con.execute("SELECT 1 FROM live_webhook_feed WHERE event=? AND occurrence=? AND state='live'",
                                   (uid, opens)).fetchone():
                        event_webhooks.observe(con, uid, opens, "ended")
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
                        event_webhooks.enqueue(con, "live-event-clip-created", uid, opens, data=dict(clip=remote))
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

    def playback_token(scope, delivery, replay, context, duration_ms, joined, sharing):
        return measurement.dumps([*scope, delivery["remote"], replay, uuid.uuid4().hex,
                                  [*context, duration_ms], joined, sharing])

    def interpreted_archive(event, delivery, scope):
        if delivery and event["save_replay"] and delivery["delivery"] in DELIVERED:
            return event_replay_spool.available(recordings.replay_root, scope[0], scope[2], delivery["remote"])
        return None

    def viewer_playback(event, delivery, scope, native_video, host_private, analytics_scope, joined=None,
                        sharing=None, archive=None, library_asset=None):
        address = analytics_token = None
        status = "Scheduled"
        attendance = [joined] if joined else []
        if not delivery["origin"]:
            if library_asset is None or delivery["delivery"] != "replay":
                return None, None, "The replay is unavailable"
            remote = delivery["remote"]
            ticket = files.dumps(scope + [remote, "library:" + remote, True] + attendance)
            address = url_for("event_media_file", token=ticket, filename="replay.mp4")
            if analytics_scope is not None:
                duration = library_asset["duration"]
                analytics_token = playback_token(scope, delivery, True, analytics_scope,
                                                round(duration * 1000) if duration else None, joined, sharing)
            return address, analytics_token, "Replay"
        if archive is not None:
            if delivery["delivery"] != "replay":
                return None, None, "The replay is awaiting release"
            session = files.dumps(scope + [delivery["remote"], f"interpreted:{archive.identity}", True] + attendance)
            address = url_for("event_media_file", token=session, filename="master.m3u8")
            if analytics_scope is not None:
                analytics_token = playback_token(scope, delivery, True, analytics_scope,
                                                round(archive.duration * 1000), joined, sharing)
            return address, analytics_token, "Replay"
        try:
            if native_video:
                raw = media.metadata(delivery["origin"], delivery["remote"])
                observed = event_peer.status(raw)
                available = (observed["public"] != host_private and (observed["state"] == "live" or
                             (observed["state"] == "replay" and delivery["delivery"] == "replay")))
                if available:
                    gateway, filename = hls_manifest(delivery, raw, host_private)
                    replay = observed["state"] == "replay"
                    session = files.dumps(scope + [delivery["remote"], gateway, replay] + attendance)
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
                analytics_token = playback_token(scope, delivery, replay, analytics_scope, duration_ms, joined, sharing)
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
        joined = None
        sharing = None
        analytics_allowed = library_analytics.permitted() and request.args.get("dnt", "").lower() not in ("1", "true")
        enrollment = None
        form_error = None
        registration_view = None
        sign_on = None
        archives = None
        latest = latest_slot = None
        library_asset = latest_library_asset = None
        latest_captioned = False
        with tx() as con:
            row, event = read(con, slug, True)
            require_event_audience(con, row, event)
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
            offer_sharing = bool(form and form["attendance_analytics"] and analytics_allowed)
            hubspot_consent = ""
            hubspot_engagement_consent = ""
            contact_plan = None
            options = event_playlist.settings(con, row["id"])
            playlist_enabled = options["enabled"] and event["schedule"]["repeat"] != "none"
            captioned = bool(shown and not withdrawn and (not delivery or delivery["delivery"] != "replay") and
                             event_captions.active_track(con, row["id"], shown["id"]))
            replay_captioned = bool(delivery and delivery["delivery"] == "replay" and
                                    event_replay_captions.read(con, row["id"], shown["id"]))
            scope = [row["id"], row["revision"], shown["id"], form["revision"] if form else None, embed] if shown else None
            salesforce_plan = None
            salesforce_related_fields = False
            salesforce_consent = ""
            salesforce_engagement_consent = ""
            salesforce_engagement_details = False
            mailchimp_consent = ""
            mailchimp_engagement_consent = ""
            mailchimp_plan = None
            if (
                form
                and not withdrawn
                and (not delivery or delivery["delivery"] != "replay")
                and event["visibility"] in event_mail.WATCHABLE
            ):
                salesforce_plan = event_salesforce_registration.offer(
                    con, salesforce, row, shown, form, int(now.timestamp()))
                if salesforce_plan:
                    salesforce_scope = ["salesforce-outbound", scope, salesforce_plan["generation"]]
                    salesforce_consent = forms.dumps(salesforce_scope)
                    salesforce_destination = json.loads(salesforce_plan["destination"])
                    salesforce_related_fields = salesforce_destination.get("related_fields") is True
                    if offer_sharing and salesforce_destination.get("engagement") is True:
                        salesforce_engagement_details = salesforce_destination.get("engagement_details") is True
                        salesforce_engagement_scope = ["salesforce-engagement", scope, salesforce_plan["generation"]]
                        salesforce_engagement_consent = forms.dumps(salesforce_engagement_scope)
                current = mailchimp.connection(con)
                candidate = event_contact_delivery.get(con, "mailchimp", row["id"], shown["id"])
                if (
                    current
                    and candidate
                    and candidate["provider_revision"] == current["revision"]
                    and candidate["event_revision"] == row["revision"]
                    and candidate["form_revision"] == form["revision"]
                    and candidate["deadline"] > int(now.timestamp())
                ):
                    mailchimp_plan = candidate
                    mailchimp_scope = ["mailchimp-outbound", scope, candidate["generation"]]
                    mailchimp_consent = forms.dumps(mailchimp_scope)
                    if offer_sharing and json.loads(candidate["destination"]).get("engagement") is True:
                        mailchimp_engagement_scope = ["mailchimp-engagement", scope, candidate["generation"]]
                        mailchimp_engagement_consent = forms.dumps(mailchimp_engagement_scope)
            marketo_plan = None
            marketo_consent = ""
            marketo_engagement_consent = ""
            if form and not withdrawn and (not delivery or delivery['delivery'] != 'replay') and event['visibility'] in event_mail.WATCHABLE and marketo_revision:
                candidate = event_contact_delivery.get(con, 'marketo', row['id'], shown['id'])
                if (
                    candidate
                    and candidate['provider_revision'] == marketo_revision
                    and candidate['event_revision'] == row['revision']
                    and candidate['form_revision'] == form['revision']
                    and candidate['deadline'] > int(now.timestamp())
                ):
                    marketo_plan = candidate
                    mark_scope = ['marketo-outbound', scope, candidate['generation']]
                    marketo_consent = forms.dumps(mark_scope)
                    if offer_sharing and json.loads(candidate["destination"]).get("engagement") is True:
                        marketo_engagement_scope = ["marketo-engagement", scope, candidate["generation"]]
                        marketo_engagement_consent = forms.dumps(marketo_engagement_scope)

            if (form and not withdrawn and (not delivery or delivery["delivery"] != "replay")
                    and event["visibility"] in event_mail.WATCHABLE
                    and hubspot_crm.outbound_available(hubspot)):
                candidate = event_hubspot_outbound.get(con, row["id"], shown["id"])
                connection = hubspot.connection(con)
                if (candidate and connection
                        and candidate["connection"] == connection["revision"]
                        and candidate["event_revision"] == row["revision"]
                        and candidate["form_revision"] == form["revision"]
                        and candidate["deadline"] > int(now.timestamp())):
                    consent_scope = ["hubspot-outbound", scope, candidate["generation"]]
                    hubspot_consent = forms.dumps(consent_scope)
                    contact_plan = candidate
                    if (offer_sharing
                            and hubspot_crm.engagement_available(hubspot)
                            and event_hubspot_engagement.definition(con, candidate["generation"])):
                        engagement_scope = ["hubspot-engagement", scope, candidate["generation"]]
                        hubspot_engagement_consent = forms.dumps(engagement_scope)
            if form:
                just_unlocked = event["visibility"] == "password" and "password" in request.form and "enrollment" not in request.form
                if request.method == "POST" and not withdrawn and not just_unlocked:
                    if not valid_ticket(forms, request.form.get("enrollment", ""), scope, 1200):
                        abort(403, "Open the registration form again.")

                    def remember(email, answers):
                        nonlocal sharing
                        registrant, created = event_registrants.record(con, row["id"], shown["id"], email, answers, "form")
                        salesforce_agreed = bool(salesforce_plan and valid_ticket(forms, request.form.get("salesforce_consent", ""), salesforce_scope, 1200))
                        if salesforce_agreed:
                            event_salesforce_registration.enqueue(con, action, salesforce_plan, registrant, answers, int(now.timestamp()), form=form)
                        if (created and contact_plan
                                and valid_ticket(forms, request.form.get("hubspot_consent", ""), consent_scope, 1200)):
                            event_hubspot_outbound.enqueue(con, action, contact_plan, registrant, int(now.timestamp()))
                        marketo_agreed = created and marketo_plan and valid_ticket(forms, request.form.get('marketo_consent', ''), mark_scope, 1200)
                        if marketo_agreed:
                            event_contact_delivery.enqueue(con,action,marketo_plan,registrant,int(now.timestamp()))
                        mailchimp_agreed = bool(created and mailchimp_plan and valid_ticket(forms, request.form.get("mailchimp_consent",""),mailchimp_scope,1200))
                        if mailchimp_agreed:
                            event_contact_delivery.enqueue(con, action, mailchimp_plan, registrant,int(now.timestamp()))
                        engagement_agreed = bool(hubspot_engagement_consent) and valid_ticket(
                            forms, request.form.get("hubspot_engagement_consent", ""), engagement_scope, 1200)
                        marketo_engagement_agreed = bool(marketo_agreed and marketo_engagement_consent and valid_ticket(forms, request.form.get("marketo_engagement_consent", ""), marketo_engagement_scope, 1200))
                        mailchimp_engagement_agreed = bool(mailchimp_agreed and mailchimp_engagement_consent and valid_ticket(forms, request.form.get("mailchimp_engagement_consent", ""), mailchimp_engagement_scope, 1200))
                        salesforce_engagement_agreed = bool(salesforce_agreed and salesforce_engagement_consent and valid_ticket(forms, request.form.get('salesforce_engagement_consent', ''), salesforce_engagement_scope, 1200))
                        if offer_sharing and (request.form.get("share_watch_time") == "yes" or engagement_agreed or marketo_engagement_agreed or mailchimp_engagement_agreed or salesforce_engagement_agreed):
                            sharing = event_registrants.share(con, registrant)
                            if engagement_agreed:
                                event_hubspot_engagement.enqueue(con, action, contact_plan, registrant, sharing)
                            if marketo_engagement_agreed:
                                event_marketo_engagement.enqueue(con, action, marketo_plan, registrant, sharing)
                            if mailchimp_engagement_agreed:
                                event_mailchimp_engagement.enqueue(con, action, mailchimp_plan, registrant, sharing)
                            if salesforce_engagement_agreed:
                                event_salesforce_engagement.enqueue(con, action, salesforce_plan, registrant, sharing)

                    admitted, sign_on, form_error = library_registration.admit(
                        con, "event", row["id"], form, scope, shown["id"],
                        on_record=remember)
                    if admitted:
                        grant = guests.dumps([*scope, None, sharing] if sharing else scope)
                        if (mail_configured and not request.form.get("registration_action")
                                and event["visibility"] in event_mail.WATCHABLE):
                            _, rules = event_mail.settings(con, row["id"])
                            if event_mail.enabled(rules):
                                attendee, created = event_attendance.issue(
                                    con, row["id"], row["revision"], shown, form, request.form["email"].strip())
                                if attendee:
                                    event_mail.schedule(con, attendee["id"], shown["id"], attendee["expires_at"], rules,
                                                        int(now.timestamp()), confirmation=created)
                else:
                    on_playlist = playlist_enabled and (withdrawn or (delivery and delivery["delivery"] == "replay"))
                    token = request.headers.get("X-CuePorch-Event-Viewer", "")
                    admitted, joined, sharing = viewer_enrollment(con, token, scope, on_playlist)
                    if admitted:
                        grant = token
                    elif not embed and not withdrawn:
                        saved = request.cookies.get("cueporch_event_" + row["id"], "")
                        candidate = event_attendance.decode(attendance_links, saved) if len(saved) <= 150 else None
                        if event_attendance.active(con, candidate, scope):
                            joined = candidate
                            grant = guests.dumps([*scope, joined])
                if not grant and not json_status and not withdrawn:
                    enrollment = forms.dumps(scope)
                    registration_view = library_registration.presentation(con, form, sign_on)
            if playlist_enabled and not joined and (not form or grant):
                archives = event_playlist.archive_playlist(con, row["id"], options, event["schedule"])
            if shown and delivery and delivery["delivery"] == "replay":
                library_asset = imported_source(con, event, delivery, row["id"], shown["id"])
            artwork = event_player.settings(con, row["id"])
            appearance = event_player.appearance(artwork, event)
            landing = event_landing.viewer(con, row["id"], embed) if not json_status and not withdrawn else None
            managed = private_delivery(con, event, row["id"], shown["id"]) if shown else False
            latest_managed = False
            if shown and not withdrawn and not joined and artwork["show_latest"] and (not form or grant):
                saved = event_playlist.latest_released(con, row["id"])
                if saved and saved["opens"] != shown["id"]:
                    latest = saved
                    latest_slot = selected_open(event, latest["opens"])
                    latest_managed = private_delivery(con, event, row["id"], latest["opens"])
                    latest_captioned = bool(event_replay_captions.read(con, row["id"], latest["opens"]))
                    latest_library_asset = imported_source(con, event, latest, row["id"], latest["opens"])
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
        if analytics_allowed:
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
        elif delivery and delivery["delivery"] == "archive_pending":
            status = "The replay is awaiting release"
        elif delivery and delivery["delivery"] in DELIVERED and shown["id"] <= now.timestamp():
            dash = event["latency"] == "fail-safe" and studio.dash_contribution(row["id"], shown["id"])
            finished = dash and dash.playback.audience_finished()
            source = studio.playback(row["id"], shown["id"]) if (
                delivery["delivery"] == "ready" and not finished and
                (dash or (event["latency"] == "low" and not captioned))) else None
            if source:
                transport = f"local:{source.identity}"
                mixer = None if dash else interpreters.audience(row["id"], shown["id"], source)
                if mixer:
                    transport = f"{transport}:{mixer.identity}"
                attendance = [joined] if joined else []
                session = files.dumps(scope + [delivery["remote"], transport, False] + attendance)
                address = url_for("event_media_file", token=session, filename="master.m3u8" if dash or mixer else "0.m3u8")
                native_video, status = True, "Live"
                if reporting_scope is not None:
                    analytics_token = playback_token(scope, delivery, False, reporting_scope, None, joined, sharing)
            elif finished and delivery["delivery"] == "ready":
                status = "This event has ended"
                if event["save_replay"]:
                    _, _, remote_status = viewer_playback(event, delivery, scope, native_video, host_private, None)
                    status = (remote_status if remote_status == "The replay is awaiting release" else
                              "Preparing the replay")
            elif dash and delivery["delivery"] == "ready":
                status = "Buffering the Fail-safe signal"
            else:
                archive = interpreted_archive(event, delivery, scope)
                if library_asset is not None:
                    native_video, transport = True, f"library:{delivery['remote']}"
                elif archive is not None:
                    native_video, transport = True, f"replay:{archive.identity}"
                address, analytics_token, status = viewer_playback(
                    event, delivery, scope, native_video, host_private, reporting_scope, joined, sharing, archive,
                    library_asset)
        if not address and latest:
            replay_scope = [*scope[:2], latest["opens"], *scope[3:]]
            private_replay = protected or latest_managed
            archive = interpreted_archive(event, latest, replay_scope)
            native_replay = (private_replay or latest_captioned or appearance["customize"]
                             or archive is not None or latest_library_asset is not None)
            replay_address, replay_measurement, replay_status = viewer_playback(
                event, latest, replay_scope, native_replay, private_replay, reporting_scope, archive=archive,
                library_asset=latest_library_asset)
            if replay_address and replay_status == "Replay":
                address, analytics_token, status = replay_address, replay_measurement, replay_status
                native_video, playing, preview_replay = native_replay, latest_slot, True
                if latest_library_asset is not None:
                    transport = f"library:{latest['remote']}"
                elif archive is not None:
                    transport = f"replay:{archive.identity}"
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
                                     landing=landing, landing_logo=logo, landing_poster=poster,
                                     analytics_token=analytics_token, event_context=reporting_token, playlist=archives,
                                     viewer_grant=grant, registration=registration_view,
                                     offer_attendee_sharing=offer_sharing, attendee_sharing=bool(sharing),
                                     hubspot_consent=hubspot_consent,
                                     salesforce_consent=salesforce_consent,
                                     salesforce_related_fields=salesforce_related_fields,
                                     salesforce_engagement_consent=salesforce_engagement_consent,
                                     salesforce_engagement_details=salesforce_engagement_details,
                                     marketo_consent=marketo_consent,
                                     mailchimp_consent=mailchimp_consent,
                                     mailchimp_engagement_consent=mailchimp_engagement_consent,
                                     marketo_engagement_consent=marketo_engagement_consent,
                                     hubspot_engagement_consent=hubspot_engagement_consent,
                                     unlock_token=frame_grant, event_access=access,
                                     current_event_url=viewer_url(row, event, embed, link_key),
                                     content_labels=content_rating.viewer_labels(event.get("content_rating", {})),
                                     enrollment=enrollment, registration_error=form_error,
                                     chat_endpoint=url_for("audience_event_chat", slug=slug, opens=shown["id"])
                                     if can_participate else None,
                                     engagement_endpoint=url_for("audience_event_engagement", slug=slug, opens=shown["id"])
                                     if can_participate else None))
        return audience_response(view, ancestors, 422 if form_error else 200,
                                 bool(access) or event["visibility"] == "private")

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
            require_event_audience(con, row, event)
            if (event_player.settings(con, row["id"])[kind] != digest
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

    def delivered(con, uid, revision, opens, form_revision, remote, replay, embed=False, joined=None):
        row, event = read(con, uid)
        delivery = occurrence(con, uid, opens)
        form = library_registration.active(con, "event", uid)
        require_event_audience(con, row, event)
        if (row["revision"] != revision or
                not delivery or delivery["remote"] != remote or delivery["delivery"] not in DELIVERED or
                replay != (delivery["delivery"] == "replay") or
                (form["revision"] if form else None) != form_revision):
            abort(404)
        if joined is not None and not event_attendance.active(con, joined, [uid, revision, opens, form_revision, embed]):
            abort(404)
        enforce_territories(event)
        return delivery

    @app.post("/api/event-playback")
    def event_playback_sample():
        try:
            uid, revision, opens, form_revision, embed, remote, replay, nonce, *reporting = measurement.loads(
                request.headers.get("X-CuePorch-Playback", ""), max_age=12 * 3600
            )
        except (BadSignature, ValueError):
            abort(404)
        if library_analytics.permitted():
            with tx() as con:
                joined = reporting[1] if len(reporting) > 1 else None
                delivered(con, uid, revision, opens, form_revision, remote, replay, embed, joined)
                sample = request.get_json()
                event_analytics.record(con, uid, opens, "replay" if replay else "live", nonce, sample,
                                       report_scope=reporting[0] if reporting else None, viewer_key=viewer_key)
                sharing = reporting[2] if len(reporting) > 2 else None
                if sharing and sample["played"] and event_registrants.sharing_active(con, sharing, uid, opens):
                    event_registrants.link_sample(con, nonce, sharing)
        return "", 204

    @app.post("/api/event-playback/withdraw-sharing")
    def withdraw_attendee_sharing():
        same_origin(secure_site(mail_origin))
        try:
            uid, _, opens, _, _, _, sharing = guests.loads(request.headers.get("X-CuePorch-Event-Viewer", ""),
                                                         max_age=12 * 3600)
        except (BadSignature, ValueError, TypeError):
            abort(404)
        with tx() as con:
            if event_registrants.sharing_active(con, sharing, uid, opens):
                event_registrants.stop_sharing(con, sharing)
                event_hubspot_engagement.cancel(con, sharing=sharing)
                event_marketo_engagement.cancel(con, sharing=sharing)
                event_mailchimp_engagement.cancel(con, sharing=sharing)
                event_salesforce_engagement.cancel(con, sharing=sharing)
                con.execute("DELETE FROM live_event_shared_qa_vote WHERE sharing=?", (sharing,))
        return private("", 204)

    @app.get("/event-media/<token>/<filename>")
    def event_media_file(token, filename):
        try:
            uid, revision, opens, form_revision, embed, remote, gateway, replay, *attendance = files.loads(
                token, max_age=12 * 3600)
        except (BadSignature, ValueError):
            abort(404)
        local = isinstance(gateway, str) and gateway.startswith("local:")
        if not local and not replay and not LIVE_FILE.fullmatch(filename):
            abort(404)
        library_asset = None
        with tx() as con:
            delivery = delivered(con, uid, revision, opens, form_revision, remote, replay, embed,
                                 attendance[0] if attendance else None)
            if not delivery["origin"]:
                if not replay or filename != "replay.mp4" or gateway != "library:" + remote:
                    abort(404)
                _, event = read(con, uid)
                library_asset = imported_source(con, event, delivery, uid, opens)
                if library_asset is None:
                    abort(404)
            if local and (replay or (not studio.dash_contribution(uid, opens) and event_captions.active_track(con, uid, opens))):
                abort(404)
        try:
            if library_asset is not None:
                response = private(send_file(library_asset["path"], mimetype="video/mp4", conditional=True))
            elif local:
                source_id, _, mix_id = gateway.removeprefix("local:").partition(":")
                source = studio.playback(uid, opens, source_id)
                if not source:
                    abort(404)
                if mix_id:
                    mixer = interpreters.audience(uid, opens, source)
                    if mixer is None or mixer.identity != mix_id:
                        abort(404)
                    directory, manifests = mixer.resource(filename)
                    response = local_hls(directory, filename, manifests)
                elif isinstance(source, event_dash_buffer.Buffer):
                    response = dash_hls(source.resource(filename))
                else:
                    response = local_hls(source.path, filename)
            elif isinstance(gateway, str) and gateway.startswith("interpreted:"):
                archive = event_replay_spool.available(recordings.replay_root, uid, opens, remote)
                manifests = event_replay_spool.ARCHIVE_MANIFESTS
                if (not replay or archive is None or gateway != f"interpreted:{archive.identity}"
                        or (filename not in manifests and not event_replay_spool.SEGMENT.fullmatch(filename))):
                    abort(404)
                response = local_hls(archive.directory, filename, manifests, playlist_limit=8 * 1024**2)
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
