"""Institution event schedules and the operator's live production controls."""

from datetime import date, datetime, time, timedelta, timezone
import json
import re
import uuid

from filelock import FileLock, Timeout
from flask import abort, make_response, render_template, request, url_for
import requests

import event_peer
import event_schedule
from library_interactivity import text_field


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event(
 id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, revision TEXT NOT NULL,
 settings TEXT NOT NULL, publication TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_occurrence(
 event TEXT NOT NULL, opens INTEGER NOT NULL, origin TEXT NOT NULL, remote TEXT NOT NULL,
 delivery TEXT NOT NULL, PRIMARY KEY(event,opens));
"""
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


def lock_event(database, uid, timeout=1):
    lock_root = database.parent / "event-production"
    lock_root.mkdir(exist_ok=True)
    return FileLock(str(lock_root / (uid + ".lock")), timeout=timeout)


def publish_replays(tx, database, origin, connect, action):
    with tx() as con:
        candidates = con.execute(
            """SELECT o.event,o.opens,o.remote FROM live_occurrence o JOIN live_event e ON e.id=o.event
               JOIN action a ON a.id=e.publication WHERE o.origin=? AND o.delivery='ready' AND a.status='eligible'
               AND json_extract(e.settings,'$.save_replay')=1 AND o.opens<=?""",
            (origin, datetime.now(timezone.utc).timestamp()),
        ).fetchall()
    for item in candidates:
        uid, opens, remote = item
        try:
            with lock_event(database, uid, timeout=0):
                with tx() as con:
                    saved = occurrence(con, uid, opens)
                    if saved["delivery"] != "ready":
                        continue
                    _, event = read(con, uid)
                host = connect(origin)
                if event_peer.observe(host, remote)["state"] != "replay":
                    continue
                with tx() as con:
                    decision = action(con, f"live-replay-{uid}-{opens}", "replay",
                                      dict(title=event["title"], event=uid, occurrence=opens))
                if decision == "eligible":
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
    try:
        schedule = event_schedule.validate(raw.get("schedule"))
    except ValueError as error:
        abort(422, str(error))
    return dict(title=title, description=description, type=kind, latency=latency,
                visibility=visibility, save_replay=replay, schedule=schedule)


def summary(con, row, event, first, last):
    visible = publication(con, row, event)
    sessions = []
    for upcoming in event_schedule.occurrences(event["schedule"], first, last):
        target = occurrence(con, row["id"], upcoming["id"])
        upcoming["delivery"] = target["delivery"] if target else "scheduled"
        sessions.append(upcoming)
    return dict(id=row["id"], slug=row["slug"], revision=row["revision"], event=event,
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


def register(app, tx, staff, action, database, public_url):
    def locked(uid):
        if not re.fullmatch(r"[a-f0-9]{32}", uid):
            abort(404)
        return lock_event(database, uid)

    @app.get("/operator/live-events")
    def live_event_dashboard():
        return render_template("live_events.html", public_url=public_url)

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
            uid = uuid.uuid4().hex
            slug = body.get("slug") or uid
            if not isinstance(slug, str) or not SLUG.fullmatch(slug):
                abort(422, "Choose a link name with letters, numbers and hyphens.")
            if con.execute("SELECT 1 FROM live_event WHERE slug=?", (slug,)).fetchone():
                abort(409, "This event link name is already used.")
            approval = None
            if event["visibility"] == "public":
                approval = "live-event-" + uid
                action(con, approval, "publication", dict(title=event["title"], event=uid, visibility="public"))
            con.execute("INSERT INTO live_event VALUES(?,?,?,?,?,?)", (uid, slug, uuid.uuid4().hex,
                        json.dumps(event), approval, datetime.now(timezone.utc).isoformat()))
            row, _ = read(con, uid)
            return summary(con, row, event, first, last), 201

    def selected_open(event, opens):
        try:
            instant = datetime.fromtimestamp(opens, timezone.utc)
        except (ValueError, OverflowError, OSError):
            abort(404)
        candidate = next((value for value in event_schedule.occurrences(event["schedule"], instant, instant + timedelta(seconds=1))
                          if value["id"] == opens), None)
        if not candidate:
            abort(404)
        return candidate

    @app.post("/api/live-events/<uid>/<int:opens>/encoder")
    def provision_event(uid, opens):
        staff()
        try:
            with locked(uid):
                with tx() as con:
                    row, event = read(con, uid)
                    shift = selected_open(event, opens)
                    if datetime.fromisoformat(shift["end"]) <= datetime.now(timezone.utc):
                        abort(409, "This event has ended.")
                    visible = publication(con, row, event)
                    if visible in ("waiting", "rejected"):
                        abort(409, "This event's publication is waiting for approval or was declined.")
                    saved = occurrence(con, uid, opens)
                    state, expected = (saved["delivery"], saved["origin"]) if saved else (None, None)
                    if state in ("withdrawing", "withdrawn"):
                        abort(409, "This occurrence is being withdrawn or has been withdrawn.")
                    if state == "replay":
                        abort(409, "This occurrence already has a finished recording.")
                with event_peer.connected_host(expected) as host:
                    if saved:
                        remote = saved["remote"]
                    else:
                        remote = event_peer.stage_remote(host, f"CuePorch event {uid} {opens}", event)
                        with tx() as con:
                            con.execute("INSERT INTO live_occurrence VALUES(?,?,?,?,?)",
                                        (uid, opens, host.origin, remote, "staged"))
                    if state != "ready":
                        event_peer.release_remote(host, remote, event, visible == "eligible")
                        with tx() as con:
                            con.execute("UPDATE live_occurrence SET delivery='ready' WHERE event=? AND opens=?",
                                        (uid, opens))
                    return dict(event_peer.encoder(host, remote), public=visible == "eligible")
        except Timeout:
            abort(409, "Another operation is preparing this event.")
        except (ValueError, requests.RequestException, KeyError):
            abort(502, "Could not prepare the event on the connected video host.")

    @app.get("/api/live-events/<uid>/<int:opens>/health")
    def event_health(uid, opens):
        staff()
        with tx() as con:
            _, event = read(con, uid)
            selected_open(event, opens)
            target = occurrence(con, uid, opens)
        if target is None or target["delivery"] not in DELIVERED:
            return dict(state=target["delivery"] if target else "scheduled")
        try:
            with event_peer.connected_host(target["origin"]) as host:
                return event_peer.observe(host, target["remote"])
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
                    if target is None or target["delivery"] == "withdrawn":
                        return "", 204
                    con.execute("UPDATE live_occurrence SET delivery='withdrawing' WHERE event=? AND opens=?",
                                (uid, opens))
                with event_peer.connected_host(target["origin"]) as host:
                    host.set_public(target["remote"], False)
                with tx() as con:
                    con.execute("UPDATE live_occurrence SET delivery='withdrawn' WHERE event=? AND opens=?",
                                (uid, opens))
                return "", 204
        except Timeout:
            abort(409, "Another operation is preparing this event.")
        except (ValueError, requests.RequestException, KeyError):
            abort(502, "The CuePorch player is closed. Retry to withdraw the copy from the video host.")

    def viewer(slug, embed):
        with tx() as con:
            row, event = read(con, slug, True)
            if publication(con, row, event) != "eligible":
                abort(404)
            now = datetime.now(timezone.utc)
            upcoming = list(event_schedule.occurrences(event["schedule"], now - timedelta(days=1), now + timedelta(days=366)))
            if opens := request.args.get("occurrence", type=int):
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
        address = None
        status = "Scheduled"
        if delivery and delivery["delivery"] in ("withdrawing", "withdrawn"):
            status = "This occurrence was withdrawn"
        if delivery and delivery["delivery"] in DELIVERED and shown["id"] <= now.timestamp():
            try:
                observed = event_peer.public_status(delivery["origin"], delivery["remote"])
                if observed["public"] and observed["state"] in ("live", "replay"):
                    address = f"{delivery['origin']}/videos/embed/{delivery['remote']}"
                    status = "Live" if observed["state"] == "live" else "Replay"
                elif observed["state"] == "waiting_for_encoder":
                    status = "Waiting for the encoder"
                elif observed["state"] == "preparing_replay":
                    status = "Preparing the replay"
            except (ValueError, requests.RequestException, KeyError):
                status = "The stream is unavailable"
        response = make_response(render_template("live_event.html", event=event, occurrence=shown,
                                                address=address, status=status, embed=embed))
        response.headers["Content-Security-Policy"] = "frame-ancestors " + ("*" if embed else "'none'")
        return response

    @app.get("/events/<slug>")
    def event_page(slug):
        return viewer(slug, False)

    @app.get("/events/<slug>/embed")
    def event_embed(slug):
        return viewer(slug, True)
