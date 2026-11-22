"""Operator-managed landing pages for programs of related live events."""

from datetime import datetime, timedelta, timezone
import json
import uuid
from zoneinfo import ZoneInfo

from flask import abort, make_response, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

import event_schedule
import event_series_presentation
import library_events
from library_interactivity import text_field

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_series_page (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    revision TEXT NOT NULL,
    settings TEXT NOT NULL,
    publication TEXT,
    created TEXT NOT NULL
);
"""

DEFAULTS = dict(
    title="Untitled event series",
    description="",
    visibility="private",
    events=[],
    presentation=event_series_presentation.DEFAULTS,
)

VISIBILITIES = ("private", "public")


def series_row(con, value, by_slug=False):
    field = "slug" if by_slug else "id"
    row = con.execute(f"SELECT * FROM live_series_page WHERE {field}=?", (value,)).fetchone()
    if not row:
        abort(404)
    return row


def series_status(con, row, settings):
    if settings["visibility"] == "private" or not row["publication"]:
        return "private"
    status = con.execute(
        "SELECT status FROM action WHERE id=?", (row["publication"],)
    ).fetchone()
    return status["status"] if status else "private"


def validate(con, body, prior_presentation=None):
    if not isinstance(body, dict) or set(body) - set(DEFAULTS):
        abort(422, "Invalid event series settings.")
    title = text_field(body.get("title", DEFAULTS["title"]), "a title", 128, required=True)
    description = text_field(body.get("description", DEFAULTS["description"]), "a description", 5000)
    visibility = body.get("visibility", DEFAULTS["visibility"])
    if visibility not in VISIBILITIES:
        abort(422, "Choose a visibility of private or public.")
    events = body.get("events", DEFAULTS["events"])
    if not isinstance(events, list) or any(not isinstance(item, str) for item in events):
        abort(422, "Select valid events.")
    events = list(dict.fromkeys(events))
    if events:
        marks = ",".join("?" * len(events))
        known = {item["id"] for item in con.execute(
            f"SELECT id FROM live_event WHERE id IN ({marks})", events
        ).fetchall()}
        if known != set(events):
            abort(422, "Select known events.")
    presentation = event_series_presentation.validate(
        con, body.get("presentation", prior_presentation)
    )
    return dict(
        title=title,
        description=description,
        visibility=visibility,
        events=events,
        presentation=presentation,
    )


def choice_list(con):
    items = []
    for row in con.execute("SELECT id, settings FROM live_event ORDER BY id").fetchall():
        settings = json.loads(row["settings"])
        items.append(dict(id=row["id"], title=settings["title"], visibility=settings["visibility"]))
    items.sort(key=lambda item: (item["title"], item["id"]))
    return items


def summary(con, row):
    settings = DEFAULTS | json.loads(row["settings"])
    status = series_status(con, row, settings)
    return dict(
        id=row["id"],
        slug=row["slug"],
        revision=row["revision"],
        settings=settings,
        publication=status,
        created=row["created"],
        watch=url_for("event_series_view", slug=row["slug"]) if status == "eligible" else None,
    )

def page_events(con, settings, preview=False):
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=366)
    upcoming, recordings = [], []
    ids = settings.get("events") or []
    if not ids:
        return dict(upcoming=upcoming, recordings=recordings)
    marks = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT * FROM live_event WHERE id IN ({marks})", tuple(ids)
    ).fetchall()
    for row in rows:
        event = json.loads(row["settings"])
        public = event["visibility"] == "public"
        visible = library_events.publication(con, row, event)
        if not preview and not (public and visible == "eligible"):
            continue
        tzname = event["schedule"]["timezone"]
        zone = ZoneInfo(tzname)
        title = event.get("title") or row["slug"]
        deliveries = {
            r["opens"]: r["delivery"]
            for r in con.execute(
                "SELECT opens, delivery FROM live_occurrence WHERE event=?",
                (row["id"],),
            ).fetchall()
        }
        for occ in event_schedule.occurrences(event["schedule"], now, horizon):
            delivery = deliveries.get(occ["id"], "scheduled")
            if delivery in ("withdrawing", "withdrawn", "replay"):
                continue
            href = (
                library_events.viewer_url(row, event, False, opens=occ["id"])
                if public and visible == "eligible" else None
            )
            upcoming.append(dict(
                slug=row["slug"], title=title, href=href,
                opens=occ["id"],
                label=occ["local"].replace("T", " ") + " " + tzname,
            ))
        for opens in sorted(
            (o for o, d in deliveries.items() if d == "replay"), reverse=True
        ):
            href = (
                library_events.viewer_url(row, event, False, opens=opens)
                if public and visible == "eligible" else None
            )
            local = datetime.fromtimestamp(opens, timezone.utc).astimezone(zone)
            recordings.append(dict(
                slug=row["slug"], title=title, href=href, opens=opens,
                label=local.strftime("%Y-%m-%d %H:%M %Z"),
            ))
    upcoming.sort(key=lambda x: (x["opens"], x["title"], x["slug"]))
    recordings.sort(key=lambda x: (-x["opens"], x["title"], x["slug"]))
    return dict(upcoming=upcoming, recordings=recordings)

def register(app, tx, staff, action, signing_key, enforce_sharing):
    preview_serializer = URLSafeTimedSerializer(
        signing_key, salt="cueporch-event-series-preview"
    )

    @app.get("/operator/event-series")
    def event_series_editor():
        return render_template("event_series_editor.html")

    @app.route("/api/event-series", methods=["GET", "POST"])
    def all_event_series():
        staff()
        with tx() as con:
            if request.method == "GET":
                return dict(
                    series=[summary(con, row)
                            for row in con.execute("SELECT * FROM live_series_page ORDER BY created DESC")],
                    events=choice_list(con)
                )
            uid = uuid.uuid4().hex
            con.execute("INSERT INTO live_series_page VALUES(?,?,?,?,NULL,?)",
                        (uid, uid, uuid.uuid4().hex, json.dumps(DEFAULTS), datetime.now(timezone.utc).isoformat()))
            row = series_row(con, uid)
            return summary(con, row), 201

    @app.post("/api/event-series/<uid>/preview")
    def preview_event_series(uid):
        staff()
        with tx() as con:
            row = series_row(con, uid)
            ticket = preview_serializer.dumps([uid, row["revision"], "operator"])
            return {"preview": url_for("event_series_view", slug=row["slug"], preview=ticket)}

    @app.route("/api/event-series/<uid>", methods=["GET", "PUT", "DELETE"])
    def event_series_item(uid):
        staff()
        with tx() as con:
            row = series_row(con, uid)
            if request.method == "GET":
                return summary(con, row)
            request.max_content_length = 512 * 1024
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or body.get("revision") != row["revision"]:
                abort(409)
            if request.method == "DELETE":
                library_events.supersede_publication(con, row["publication"])
                con.execute("DELETE FROM live_series_page WHERE id=?", (row["id"],))
                return ("", 204)
            prior = DEFAULTS | json.loads(row["settings"])
            values = validate(con, body.get("settings"), prior["presentation"])
            enforce_sharing(
                con,
                settings=dict(visibility=values["visibility"], embed="disabled", domains=[]),
                prior=dict(visibility=prior.get("visibility"), embed="disabled", domains=[]),
            )
            new_slug = body.get("slug")
            if not isinstance(new_slug, str) or not library_events.SLUG.fullmatch(new_slug):
                abort(400)
            clash = con.execute(
                "SELECT 1 FROM live_series_page WHERE slug=? AND id<>?",
                (new_slug, row["id"]),
            ).fetchone()
            if clash is not None:
                abort(409)
            if values == prior and new_slug == row["slug"]:
                return summary(con, row)
            library_events.supersede_publication(con, row["publication"])
            publication = None
            if values["visibility"] == "public":
                new_action_id = f"live-series-{uid}-{uuid.uuid4().hex}"
                action(
                    con,
                    new_action_id,
                    "publication",
                    dict(title=values["title"], series=uid, visibility="public"),
                )
                publication = new_action_id
            new_revision = uuid.uuid4().hex
            con.execute(
                "UPDATE live_series_page SET slug=?, revision=?, settings=?, publication=? WHERE id=?",
                (new_slug, new_revision, json.dumps(values), publication, row["id"]),
            )
            row = series_row(con, uid)
            return summary(con, row)

    @app.get("/event-series/<slug>")
    def event_series_view(slug):
        with tx() as con:
            row = series_row(con, slug, by_slug=True)
            settings = DEFAULTS | json.loads(row["settings"])
            token = request.args.get("preview")
            internal = False

            if token is not None:
                try:
                    internal = preview_serializer.loads(token, max_age=600) == [
                        row["id"],
                        row["revision"],
                        "operator",
                    ]
                except BadSignature:
                    abort(404)
                if not internal:
                    abort(404)
            elif series_status(con, row, settings) != "eligible":
                abort(404)

            payload = page_events(con, settings, preview=internal)
            payload["presentation"] = event_series_presentation.render(
                con, settings["presentation"]
            )

        resp = make_response(
            render_template(
                "event_series_view.html",
                settings=settings,
                preview=internal,
                **payload,
            )
        )
        resp.headers["Cache-Control"] = "private, no-store"
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"

        if internal:
            resp.headers["X-Robots-Tag"] = "noindex"
            resp.headers["Referrer-Policy"] = "no-referrer"

        return resp
