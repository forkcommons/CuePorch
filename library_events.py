"""Institution event schedules and the operator's live production controls."""

from datetime import date, datetime, time, timedelta, timezone
import json
import re
import uuid

from filelock import FileLock, Timeout
from flask import abort, make_response, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
import requests

from event_media import Gateway, HLS_PREFIXES, LIVE_FILE, root_path
from event_capture import BusyError, CHUNK_BYTES, Studio, srt_endpoint
import event_peer
import event_analytics
import event_chat
import event_captions
import event_engagement
import event_library_recordings
import event_playlist
import event_program
import event_replay_captions
import event_schedule
import event_scenes
import event_simulcast
import event_slide_decks
import event_speakers
from catalog import video_version_key
from library_actions import title_for
from library_interactivity import text_field
import library_registration


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event(
 id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, revision TEXT NOT NULL,
 settings TEXT NOT NULL, publication TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_occurrence(
 event TEXT NOT NULL, opens INTEGER NOT NULL, origin TEXT NOT NULL, remote TEXT NOT NULL,
 delivery TEXT NOT NULL, PRIMARY KEY(event,opens));
""" + (event_program.SCHEMA + event_chat.SCHEMA + event_engagement.SCHEMA + event_scenes.SCHEMA
       + event_speakers.SCHEMA + event_captions.SCHEMA + event_analytics.SCHEMA
       + event_replay_captions.SCHEMA + event_simulcast.SCHEMA + event_library_recordings.SCHEMA
       + event_playlist.SCHEMA + event_slide_decks.SCHEMA)
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")
TYPES = ("live", "webinar", "team", "learning", "custom")
PUBLIC_MODES = ("public", "private")
DELIVERED = ("ready", "replay")


def read(con, value, by_slug=False):
    field = "slug" if by_slug else "id"
    row = con.execute(f"SELECT * FROM live_event WHERE {field}=?", (value,)).fetchone()
    if not row:
        abort(404)
    return row, json.loads(row["settings"])


def publication(con, row, event):
    if event["visibility"] != "public":
        return "private"
    status = con.execute("SELECT status FROM action WHERE id=?", (row["publication"],)).fetchone()
    return status["status"] if status else "private"


def occurrence(con, uid, opens):
    return con.execute("SELECT * FROM live_occurrence WHERE event=? AND opens=?", (uid, opens)).fetchone()


def publishing_action(con, action, uid, event):
    if event["visibility"] != "public":
        return None
    key = f"live-event-{uid}-{uuid.uuid4().hex}"
    action(con, key, "publication", dict(title=event["title"], event=uid, visibility="public"))
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
               (o.delivery='ready' OR (o.delivery='replay' AND EXISTS (
                   SELECT 1 FROM live_caption_setting s WHERE s.event=o.event AND s.opens=o.opens AND s.enabled=1
               ) AND NOT EXISTS (
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
                host = connect(origin)
                if event_peer.observe(host, remote)["state"] != "replay":
                    continue
                with tx() as con:
                    decision = action(con, f"live-replay-{uid}-{opens}", "replay",
                                      dict(title=event["title"], event=uid, occurrence=opens))
                    if decision in ("eligible", "waiting"):
                        event_replay_captions.queue(con, action, uid, opens)
                if decision == "eligible":
                    if not registered and not event.get("cueporch_delivery"):
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
    if kind not in TYPES or latency not in tuple(event_peer.LATENCY) or visibility not in PUBLIC_MODES:
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
    return value


def summary(con, row, event, first, last):
    visible = publication(con, row, event)
    sessions = []
    for upcoming in event_schedule.occurrences(event["schedule"], first, last):
        target = occurrence(con, row["id"], upcoming["id"])
        program = event_program.program(con, row["id"], upcoming["id"])
        upcoming["delivery"] = target["delivery"] if target else "scheduled"
        upcoming["program"] = program["state"] if program else None
        sessions.append(upcoming)
    return dict(id=row["id"], slug=row["slug"], revision=row["revision"], event=event, editable=editable(con, row["id"]),
                publication=visible, occurrences=sessions,
                watch=url_for("event_page", slug=row["slug"]) if visible == "eligible" else None,
                embed=url_for("event_embed", slug=row["slug"]) if visible == "eligible" else None)


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


def register(app, tx, staff, action, database, public_url, signing_key, root, read_works, institution, remember_work):
    media = Gateway()
    studio = Studio(database)
    programs = event_program.Programs(app, tx, root, database, read_works, studio)
    recordings = event_library_recordings.Recordings(app, tx, media, database, root, institution, remember_work)

    def stop():
        outputs.close()
        programs.close()
        studio.close()
        captions.close()
        replay_captions.close()
        recordings.close()

    app.extensions["stop_event_capture"] = stop
    forms = URLSafeTimedSerializer(signing_key, salt="cueporch-event-enrollment")
    guests = URLSafeTimedSerializer(signing_key, salt="cueporch-event-viewer")
    files = URLSafeTimedSerializer(signing_key, salt="cueporch-event-media")
    measurement = URLSafeTimedSerializer(signing_key, salt="cueporch-event-playback")
    event_playlist.register(app, tx, staff, read)

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
        return render_template("live_events.html", public_url=public_url)

    @app.get("/operator/live-events/<uid>/<int:opens>/studio")
    def event_studio_page(uid, opens):
        return render_template("record_studio.html", broadcast_endpoint=url_for("start_event_capture", uid=uid, opens=opens),
                               scene_endpoint=url_for("studio_scene_deck", uid=uid, opens=opens),
                               health_endpoint=url_for("event_health", uid=uid, opens=opens),
                               speaker_endpoint=url_for("event_speakers", event=uid, opens=opens),
                               engagement_endpoint=url_for("operator_event_engagement", uid=uid, opens=opens),
                               speaker_public_url=public_url)

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
            event.setdefault("archive_replay", True)
            event["cueporch_delivery"] = True
            uid = uuid.uuid4().hex
            slug = body.get("slug") or uid
            if not isinstance(slug, str) or not SLUG.fullmatch(slug):
                abort(422, "Choose a link name with letters, numbers and hyphens.")
            if con.execute("SELECT 1 FROM live_event WHERE slug=?", (slug,)).fetchone():
                abort(409, "This event link name is already used.")
            approval = publishing_action(con, action, uid, event)
            con.execute("INSERT INTO live_event VALUES(?,?,?,?,?,?)", (uid, slug, uuid.uuid4().hex,
                        json.dumps(event), approval, datetime.now(timezone.utc).isoformat()))
            row, _ = read(con, uid)
            return summary(con, row, event, first, last), 201

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
                if changed == prior:
                    return summary(con, row, prior, first, last)
                if not editable(con, uid):
                    abort(409, "Withdraw prepared occurrences before changing this event.")
                supersede_publication(con, row["publication"])
                approval = publishing_action(con, action, uid, changed)
                con.execute("UPDATE live_event SET revision=?,settings=?,publication=? WHERE id=?",
                            (uuid.uuid4().hex, json.dumps(changed), approval, uid))
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
        form = library_registration.active(con, "event", row["id"])
        scope = [row["id"], row["revision"], opens, form["revision"] if form else None]
        embed = request.headers.get("X-CuePorch-Chat-Embed") == "yes"
        if form:
            on_playlist = (delivery and delivery["delivery"] == "replay"
                           and event_playlist.settings(con, row["id"])["enabled"]
                           and event["schedule"]["repeat"] != "none")
            if not viewer_enrolled(request.headers.get("X-CuePorch-Event-Viewer", ""), scope + [embed], on_playlist):
                abort(404)
        now = datetime.now(timezone.utc)
        active = now < datetime.fromisoformat(slot["end"]) and (not delivery or delivery["delivery"] != "replay")
        return scope, active

    audience_session = event_chat.register(app, tx, staff, signing_key, operator_chat, audience_chat)
    event_engagement.register(app, tx, staff, audience_session, operator_chat, audience_chat)
    event_scenes.register(app, tx, staff, root, read_works, operator_chat)
    event_slide_decks.register(app, tx, staff, database, operator_chat)
    event_speakers.register(app, tx, staff, speaker_access, database)
    event_captions.register(app, tx, staff, operator_chat, audience_chat)
    captions = event_captions.LiveCaptionJobs(tx, action, media, caption_access, database)
    event_replay_captions.register(app, tx, staff, action, operator_chat, audience_chat)
    replay_captions = event_replay_captions.ReplayCaptionJobs(app, tx, media, database)

    def can_simulcast(con, uid, event):
        return event["visibility"] == "public" and library_registration.active(con, "event", uid) is None

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
                candidates[uid, opens] = (row["origin"], row["remote"], targets)
        return candidates

    outputs = event_simulcast.Simulcast(app, tx, media, database, signing_key, outgoing_programs)
    event_simulcast.register(app, tx, staff, destination_access, outputs)

    def provision_event(uid, opens, browser=False, automatic=False):
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
                registered = library_registration.active(con, "event", uid) is not None
                state, expected = (saved["delivery"], saved["origin"]) if saved else (None, None)
                if state in ("withdrawing", "withdrawn"):
                    abort(409, "This occurrence is being withdrawn or has been withdrawn.")
                if state == "replay":
                    abort(409, "This occurrence already has a finished recording.")
            with event_peer.connected_host(expected) as host:
                broadcast = visible == "eligible" and not registered
                if saved:
                    remote = saved["remote"]
                else:
                    remote = event_peer.stage_remote(host, f"CuePorch event {uid} {opens}", event)
                    with tx() as con:
                        con.execute("INSERT INTO live_occurrence VALUES(?,?,?,?,?)",
                                    (uid, opens, host.origin, remote, "staged"))
                if state != "ready":
                    event_peer.release_remote(host, remote, event, broadcast and not event.get("cueporch_delivery"))
                    with tx() as con:
                        con.execute("UPDATE live_occurrence SET delivery='ready' WHERE event=? AND opens=?",
                                    (uid, opens))
                if browser and event_peer.observe(host, remote)["state"] != "waiting_for_encoder":
                    abort(409, "This event already has a live or finished signal.")
                return dict(event_peer.encoder(host, remote), public=broadcast)
        except event_peer.UnsupportedDvrWindow as error:
            abort(422, str(error))
        except (ValueError, requests.RequestException, KeyError):
            abort(502, "Could not prepare the event on the connected video host.")

    @app.post("/api/live-events/<uid>/<int:opens>/encoder")
    def prepare_external_encoder(uid, opens):
        staff()
        try:
            with locked(uid):
                return provision_event(uid, opens)
        except Timeout:
            abort(409, "Another operation is preparing this event.")

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

    @app.post("/api/live-events/<uid>/<int:opens>/browser")
    def start_event_capture(uid, opens):
        staff()
        try:
            with locked(uid):
                connection = provision_event(uid, opens, browser=True)
                return dict(token=studio.start(uid, opens, connection)), 201
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
        if not isinstance(body, dict):
            abort(422, "Choose a receiving interface and port.")
        try:
            endpoint = srt_endpoint(body.get("interface"), body.get("port"))
        except ValueError as error:
            abort(422, str(error))
        try:
            with locked(uid):
                prior = studio.srt_credentials(uid, opens, endpoint)
                if prior:
                    return prior
                connection = provision_event(uid, opens, browser=True)
                return studio.srt(uid, opens, connection, endpoint)
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
            managed = event.get("cueporch_delivery") or library_registration.active(con, "event", uid)
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

    def viewer(slug, embed):
        json_status = request.accept_mimetypes.best == "application/json"
        grant = None
        enrollment = None
        form_error = None
        archives = None
        with tx() as con:
            row, event = read(con, slug, True)
            if publication(con, row, event) != "eligible":
                abort(404)
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
                if request.method == "POST" and not withdrawn:
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
        address = None
        analytics_token = None
        protected = bool(form)
        host_private = protected or event.get("cueporch_delivery", False)
        native_video = host_private or captioned or replay_captioned
        can_participate = shown and not withdrawn and (not protected or grant)
        status = "Scheduled"
        if withdrawn:
            status = "This occurrence was withdrawn"
        if protected and not grant and not withdrawn:
            status = "Registration required"
        elif delivery and delivery["delivery"] in DELIVERED and shown["id"] <= now.timestamp():
            try:
                if native_video:
                    raw = media.metadata(delivery["origin"], delivery["remote"])
                    observed = event_peer.status(raw)
                    available = (observed["public"] != host_private and (observed["state"] == "live" or
                                 (observed["state"] == "replay" and delivery["delivery"] == "replay")))
                    if available:
                        manifest = next(entry["playlistUrl"] for entry in raw["streamingPlaylists"] if entry["type"] == 1)
                        directory, filename = root_path(delivery["origin"], manifest, delivery["remote"], host_private)
                        gateway = HLS_PREFIXES.index(directory.rsplit("/", 1)[0] + "/")
                        session = files.dumps(scope + [delivery["remote"], gateway, observed["state"] == "replay"])
                        address = url_for("event_media_file", token=session, filename=filename)
                        if event_analytics.permitted():
                            analytics_token = measurement.dumps(
                                scope + [delivery["remote"], observed["state"] == "replay", uuid.uuid4().hex]
                            )
                        status = "Live" if observed["state"] == "live" else "Replay"
                else:
                    observed = event_peer.public_status(delivery["origin"], delivery["remote"])
                if not native_video and observed["public"] and observed["state"] in ("live", "replay"):
                    address = f"{delivery['origin']}/videos/embed/{delivery['remote']}"
                    status = "Live" if observed["state"] == "live" else "Replay"
                elif observed["state"] == "waiting_for_encoder":
                    status = "Waiting for the encoder"
                elif observed["state"] in ("preparing_replay", "ended"):
                    status = "Preparing the replay" if event["save_replay"] else "This event has ended"
                elif host_private and observed["state"] == "replay" and not address:
                    status = "The replay is awaiting release"
            except (ValueError, requests.RequestException, KeyError, StopIteration):
                status = "The stream is unavailable"
        caption_endpoint = None
        if can_participate:
            if status == "Replay" and native_video:
                caption_endpoint = url_for("audience_replay_captions", slug=slug, opens=shown["id"])
            elif captioned:
                caption_endpoint = url_for("viewer_event_captions", slug=slug, opens=shown["id"])
        view = (dict(status=status, address=address, protected=protected, native_video=native_video,
                     captions=caption_endpoint, analytics_token=analytics_token, playlist=archives, occurrence=shown)
                if json_status
                else render_template("live_event.html", event=event, occurrence=shown,
                                     address=address, status=status, embed=embed, protected=protected,
                                     native_video=native_video, caption_endpoint=caption_endpoint,
                                     analytics_token=analytics_token, playlist=archives,
                                     viewer_grant=grant, registration=form if enrollment else None,
                                     enrollment=enrollment, registration_error=form_error,
                                     chat_endpoint=url_for("audience_event_chat", slug=slug, opens=shown["id"])
                                     if can_participate else None,
                                     engagement_endpoint=url_for("audience_event_engagement", slug=slug, opens=shown["id"])
                                     if can_participate else None))
        response = make_response(view, 422 if form_error else 200)
        response.headers["Vary"] = "Accept"
        response.headers["Content-Security-Policy"] = "frame-ancestors " + ("*" if embed else "'none'")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.route("/events/<slug>", methods=["GET", "POST"])
    def event_page(slug):
        return viewer(slug, False)

    @app.route("/events/<slug>/embed", methods=["GET", "POST"])
    def event_embed(slug):
        return viewer(slug, True)

    def delivered(con, uid, revision, opens, form_revision, remote, replay):
        row, event = read(con, uid)
        delivery = occurrence(con, uid, opens)
        form = library_registration.active(con, "event", uid)
        if (row["revision"] != revision or publication(con, row, event) != "eligible" or
                not delivery or delivery["remote"] != remote or delivery["delivery"] not in DELIVERED or
                replay != (delivery["delivery"] == "replay") or
                (form["revision"] if form else None) != form_revision):
            abort(404)
        return delivery

    @app.post("/api/event-playback")
    def event_playback_sample():
        try:
            uid, revision, opens, form_revision, _, remote, replay, nonce = measurement.loads(
                request.headers.get("X-CuePorch-Playback", ""), max_age=12 * 3600
            )
        except (BadSignature, ValueError):
            abort(404)
        if event_analytics.permitted():
            with tx() as con:
                delivered(con, uid, revision, opens, form_revision, remote, replay)
                event_analytics.record(con, uid, opens, "replay" if replay else "live", nonce, request.get_json())
        return "", 204

    @app.get("/event-media/<token>/<filename>")
    def event_media_file(token, filename):
        try:
            uid, revision, opens, form_revision, _, remote, gateway, replay = files.loads(token, max_age=12 * 3600)
        except (BadSignature, ValueError):
            abort(404)
        if not replay and not LIVE_FILE.fullmatch(filename):
            abort(404)
        with tx() as con:
            delivery = delivered(con, uid, revision, opens, form_revision, remote, replay)
        try:
            return media.media(delivery["origin"], remote, gateway, filename, request.headers.get("Range"))
        except (ValueError, requests.RequestException):
            abort(404)


def valid_ticket(signer, token, scope, age):
    try:
        return signer.loads(token, max_age=age) == scope
    except BadSignature:
        return False
