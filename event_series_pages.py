"""Operator-managed landing pages for programs of related live events."""

from datetime import datetime, timedelta, timezone
import json
import os
import uuid
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from flask import abort, make_response, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

import event_schedule
import event_series_hosts
import event_series_locale
import event_series_presentation
import library_events
import library_privacy
from browser_account_security import private, secure_site
from library_interactivity import text_field
from team_accounts import current as current_team_member

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_series_page (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    revision TEXT NOT NULL,
    settings TEXT NOT NULL,
    publication TEXT,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_series_access (
    series TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL
);
"""

DEFAULTS = dict(
    title="Untitled event series",
    description="",
    visibility="private",
    custom_host="",
    upcoming_mode="all",
    localization=event_series_locale.DEFAULTS,
    events=[],
    hidden_recordings=[],
    presentation=event_series_presentation.DEFAULTS,
)

VISIBILITIES = ("private", "public", "team", "password")
UPCOMING_MODES = ("all", "next", "grouped")


def password_hash(con, uid):
    row = con.execute(
        "SELECT password_hash FROM live_series_access WHERE series=?", (uid,)
    ).fetchone()
    return row["password_hash"] if row else None


def requested_password(body, values, previous=None):
    password = body.get("view_password", "")
    if not isinstance(password, str) or len(password) > 256:
        abort(422, "Enter a password of up to 256 characters.")
    if values["visibility"] != "password":
        return None
    if not (password or previous):
        abort(422, "Enter a password for series viewers.")
    if password and (not previous or not check_password_hash(previous, password)):
        return generate_password_hash(password)
    return previous


def store_password(con, uid, digest):
    if digest:
        con.execute(
            "INSERT OR REPLACE INTO live_series_access(series,password_hash) VALUES(?,?)",
            (uid, digest),
        )
    else:
        con.execute("DELETE FROM live_series_access WHERE series=?", (uid,))


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


def hidden_recording_refs(con, submitted, selected, previous):
    if not isinstance(submitted, list):
        abort(422, "Select valid hidden recordings.")
    limit = 2 ** 53
    selected_set = set(selected)
    prior = set()
    for item in (previous or []):
        if (isinstance(item, dict) and set(item) == {"event", "opens"}
                and isinstance(item.get("event"), str)
                and isinstance(item.get("opens"), int)
                and not isinstance(item.get("opens"), bool)):
            prior.add((item["event"], item["opens"]))
    known = set()
    if selected_set:
        marks = ",".join("?" * len(selected_set))
        rows = con.execute(
            f"SELECT event, opens FROM live_occurrence "
            f"WHERE delivery='replay' AND event IN ({marks})",
            tuple(selected_set),
        ).fetchall()
        known = {(r["event"], r["opens"]) for r in rows}
    result = set()
    for item in submitted:
        if (not isinstance(item, dict) or set(item) != {"event", "opens"}
                or not isinstance(item.get("event"), str)
                or not isinstance(item.get("opens"), int)
                or isinstance(item.get("opens"), bool)
                or item["opens"] <= -limit or item["opens"] >= limit):
            abort(422, "Select valid hidden recordings.")
        ref = (item["event"], item["opens"])
        if ref[0] not in selected_set:
            abort(422, "Hidden recordings must reference selected events.")
        if ref not in prior and ref not in known:
            abort(422, "Hidden recordings must reference replay recordings.")
        result.add(ref)
    return [dict(event=e, opens=o) for e, o in sorted(result)]


def write_series(con, row, slug, values, action):
    """Persist a new live_series_page revision: supersede the prior publication,
    mint a fresh publication action for non-private visibility, write the new
    slug/revision/settings/publication, and return the refreshed row."""
    uid = row["id"]
    library_events.supersede_publication(con, row["publication"])
    publication = None
    if values["visibility"] != "private":
        new_action_id = f"live-series-{uid}-{uuid.uuid4().hex}"
        action(
            con,
            new_action_id,
            "publication",
            dict(title=values["title"], series=uid, visibility=values["visibility"]),
        )
        publication = new_action_id
    new_revision = uuid.uuid4().hex
    con.execute(
        "UPDATE live_series_page SET slug=?, revision=?, settings=?, publication=? WHERE id=?",
        (slug, new_revision, json.dumps(values), publication, row["id"]),
    )
    return series_row(con, uid)


def save_translation(con, row, settings, language, translated, action):
    prior = settings.get("localization", event_series_locale.DEFAULTS)
    translations = prior["translations"] | {language: translated | {"published": True}}
    localized = event_series_locale.validate(dict(base=prior["base"], translations=translations), prior)
    return write_series(con, row, row["slug"], settings | dict(localization=localized), action)


def validate(con, body, prior, allowed_hosts):
    if not isinstance(body, dict) or set(body) - set(DEFAULTS):
        abort(422, "Invalid event series settings.")
    title = text_field(body.get("title", DEFAULTS["title"]), "a title", 128, required=True)
    description = text_field(body.get("description", DEFAULTS["description"]), "a description", 5000)
    visibility = body.get("visibility", DEFAULTS["visibility"])
    if visibility not in VISIBILITIES:
        abort(422, "Choose private draft, public, team only, or password protected.")
    previous_host = prior["custom_host"] if prior["custom_host"] in allowed_hosts else ""
    custom_host = event_series_hosts.requested_host(
        body.get("custom_host", previous_host), allowed_hosts, visibility
    )
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
    prior_hidden = prior["hidden_recordings"]
    if "hidden_recordings" not in body:
        requested_hidden = [h for h in prior_hidden if h["event"] in events]
    else:
        requested_hidden = body["hidden_recordings"]
    hidden_recordings = hidden_recording_refs(con, requested_hidden, events, prior_hidden)

    upcoming_mode = body.get("upcoming_mode", prior["upcoming_mode"])
    if upcoming_mode not in UPCOMING_MODES:
        abort(422, "Choose all dates, next date per event, or dates grouped by event.")
    presentation = event_series_presentation.validate(
        con, body.get("presentation", prior["presentation"])
    )
    localization = event_series_locale.validate(
        body.get("localization", prior["localization"]), prior["localization"]
    )
    return dict(
        title=title,
        description=description,
        visibility=visibility,
        custom_host=custom_host,
        upcoming_mode=upcoming_mode,
        localization=localization,
        events=events,
        hidden_recordings=hidden_recordings,
        presentation=presentation,
    )


def choice_list(con):
    items = []
    for row in con.execute("SELECT id, settings FROM live_event ORDER BY id").fetchall():
        settings = json.loads(row["settings"])
        items.append(dict(id=row["id"], title=settings["title"], visibility=settings["visibility"]))
    items.sort(key=lambda item: (item["title"], item["id"]))
    return items


def replay_choices(con):
    items = []
    rows = con.execute(
        "SELECT e.id, e.settings, o.opens "
        "FROM live_occurrence o JOIN live_event e ON e.id=o.event "
        "WHERE o.delivery='replay' "
        "ORDER BY o.opens DESC, e.id"
    ).fetchall()
    for row in rows:
        settings = json.loads(row["settings"])
        tz = ZoneInfo(settings["schedule"]["timezone"])
        opens = row["opens"]
        items.append(dict(
            event=row["id"],
            opens=opens,
            title=settings.get("title") or row["id"],
            label=datetime.fromtimestamp(opens, tz).strftime("%Y-%m-%d %H:%M %Z"),
        ))
    return items


def read_alias(con, hostname):
    row = con.execute(
        "SELECT * FROM live_series_page WHERE json_extract(settings,'$.custom_host')=?",
        (hostname,),
    ).fetchone()
    if row is None:
        abort(404)
    return row


def require_unique_host(con, uid, custom_host):
    if not custom_host:
        return
    dup = con.execute(
        "SELECT 1 FROM live_series_page AS same "
        "WHERE json_extract(same.settings,'$.custom_host')=? AND same.id <> ?",
        (custom_host, uid),
    ).fetchone()
    if dup:
        abort(409)


def watch_url(row, settings, status, allowed_hosts):
    if status != "eligible":
        return None
    host = settings.get("custom_host") if settings else None
    visibility = settings.get("visibility") if settings else None
    if host and host in allowed_hosts and visibility in ("public", "password"):
        return f"https://{host}/"
    return url_for("event_series_view", slug=row["slug"])


def summary(con, row, allowed_hosts):
    settings = DEFAULTS | json.loads(row["settings"])
    status = series_status(con, row, settings)
    return dict(
        id=row["id"],
        slug=row["slug"],
        revision=row["revision"],
        settings=settings,
        publication=status,
        has_password=bool(password_hash(con, row["id"])),
        created=row["created"],
        watch=watch_url(row, settings, status, allowed_hosts),
    )


def page_events(con, settings, preview=False, audience="public", link_origin=""):
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=366)
    upcoming, recordings = [], []
    ids = settings.get("events") or []
    if not ids:
        return dict(
            upcoming=[], next_upcoming=[], upcoming_groups=[], recordings=[]
        )
    hidden = {(h["event"], h["opens"]) for h in settings["hidden_recordings"]}
    marks = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT * FROM live_event WHERE id IN ({marks})", tuple(ids)
    ).fetchall()
    for row in rows:
        event = json.loads(row["settings"])
        mode = event["visibility"]
        visible = library_events.publication(con, row, event)
        can_link = (
            visible == "eligible" and (mode == "public" or (mode == "password" and audience == "password"))
        ) or (mode == "private" and audience == "team")
        if not preview and not can_link:
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
                library_events.viewer_url(row, event, False, opens=occ["id"]) if can_link else None
            )
            upcoming.append(dict(
                slug=row["slug"], title=title, href=link_origin + href if href else None,
                opens=occ["id"],
                timezone=tzname,
                label=occ["local"].replace("T", " ") + " " + tzname,
            ))
        for opens in sorted(
            (o for o, d in deliveries.items() if d == "replay" and (row["id"], o) not in hidden),
            reverse=True,
        ):
            href = (
                library_events.viewer_url(row, event, False, opens=opens) if can_link else None
            )
            local = datetime.fromtimestamp(opens, timezone.utc).astimezone(zone)
            recordings.append(dict(
                slug=row["slug"], title=title, href=link_origin + href if href else None, opens=opens,
                timezone=tzname,
                label=local.strftime("%Y-%m-%d %H:%M %Z"),
            ))
    upcoming.sort(key=lambda x: (x["opens"], x["title"], x["slug"]))
    next_upcoming, seen, groups = [], set(), {}
    for item in upcoming:
        if item["slug"] not in seen:
            seen.add(item["slug"])
            next_upcoming.append(item)
        groups.setdefault(item["slug"], {"title": item["title"], "items": []})["items"].append(item)
    recordings.sort(key=lambda x: (-x["opens"], x["title"], x["slug"]))
    return dict(
        upcoming=upcoming,
        next_upcoming=next_upcoming,
        upcoming_groups=list(groups.values()),
        recordings=recordings,
    )


def register(app, tx, staff, action, signing_key, enforce_sharing, team_origin):
    preview_serializer = URLSafeTimedSerializer(
        signing_key, salt="cueporch-event-series-preview"
    )
    series_passwords = URLSafeTimedSerializer(signing_key, salt="cueporch-event-series-access")
    allowed_hosts = event_series_hosts.configured_hosts(
        team_origin, os.environ.get("CUEPORCH_EVENT_SERIES_HOSTS", "")
    )
    event_series_hosts.guard(app, allowed_hosts, lambda: event_series_view(None))

    @app.get("/operator/event-series")
    def event_series_editor():
        return render_template(
            "event_series_editor.html", languages=event_series_locale.LANGUAGES,
            allowed_hosts=allowed_hosts,
        )

    @app.route("/api/event-series", methods=["GET", "POST"])
    def all_event_series():
        staff()
        with tx() as con:
            if request.method == "GET":
                return dict(
                    series=[summary(con, row, allowed_hosts)
                            for row in con.execute("SELECT * FROM live_series_page ORDER BY created DESC")],
                    events=choice_list(con),
                    recordings=replay_choices(con),
                )
            uid = uuid.uuid4().hex
            con.execute("INSERT INTO live_series_page VALUES(?,?,?,?,NULL,?)",
                        (uid, uid, uuid.uuid4().hex, json.dumps(DEFAULTS), datetime.now(timezone.utc).isoformat()))
            row = series_row(con, uid)
            return summary(con, row, allowed_hosts), 201

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
                return summary(con, row, allowed_hosts)
            request.max_content_length = 512 * 1024
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or body.get("revision") != row["revision"]:
                abort(409)
            if request.method == "DELETE":
                library_events.supersede_publication(con, row["publication"])
                store_password(con, uid, None)
                con.execute("DELETE FROM live_series_page WHERE id=?", (row["id"],))
                return ("", 204)
            prior = DEFAULTS | json.loads(row["settings"])
            values = validate(con, body.get("settings"), prior, allowed_hosts)
            require_unique_host(con, uid, values["custom_host"])
            previous_digest = password_hash(con, uid)
            digest = requested_password(body, values, previous_digest)
            enforce_sharing(
                con,
                settings=dict(
                    visibility=("private" if values["visibility"] == "team" else values["visibility"]),
                    embed="disabled",
                    domains=[],
                ),
                prior=dict(
                    visibility=("private" if prior.get("visibility") == "team" else prior.get("visibility")),
                    embed="disabled",
                    domains=[],
                ),
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
            if values == prior and new_slug == row["slug"] and digest == previous_digest:
                return summary(con, row, allowed_hosts)
            updated = write_series(con, row, new_slug, values, action)
            store_password(con, uid, digest)
            return summary(con, updated, allowed_hosts)

    @app.route("/event-series/<slug>", methods=["GET", "POST"])
    def event_series_view(slug):
        ticket = None
        alias = event_series_hosts.current_host(allowed_hosts) if slug is None else None
        if slug is None and alias is None:
            abort(404)
        with tx() as con:
            row = read_alias(con, alias) if alias else series_row(con, slug, by_slug=True)
            settings = DEFAULTS | json.loads(row["settings"])
            token = request.args.get("preview")
            if alias and (token is not None or settings["visibility"] not in ("public", "password")):
                abort(404)
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

            def locked_series(incorrect):
                language = event_series_locale.resolve(
                    settings["localization"], settings, {}, preview=False)[0]
                response = private(render_template(
                    "event_series_password.html", incorrect=incorrect,
                    language=language, labels=event_series_locale.labels(language)),
                    403 if incorrect else 200)
                response.headers["Content-Language"] = language
                response.headers["Referrer-Policy"] = "no-referrer"
                return response

            audience = "public" if internal else settings["visibility"]
            if audience == "team":
                secure = secure_site(team_origin, required=False)
                if secure is None or not current_team_member(con, secure):
                    abort(404)
            elif audience == "password":
                digest = password_hash(con, row["id"])
                if not digest:
                    abort(404)
                secure = secure_site(f"https://{alias}" if alias else team_origin, required=False)
                scope = [row["id"], row["revision"]]
                if request.method == "POST":
                    request.max_content_length = 4096
                    if not library_privacy.try_password(
                        con, signing_key, "series:" + row["id"], request.form.get("password", ""), digest
                    ):
                        return locked_series(True)
                    if secure is not None:
                        ticket = (series_passwords.dumps(scope), secure)
                elif secure is None or not library_events.valid_ticket(
                    series_passwords, request.cookies.get("cueporch_series_access", ""), scope, 12 * 3600
                ):
                    return locked_series(False)

            payload = page_events(
                con,
                settings,
                preview=internal,
                audience=audience,
                link_origin=team_origin if alias else "",
            )
            language, options, viewer_settings, payload["presentation"] = event_series_locale.resolve(
                settings["localization"],
                settings,
                event_series_presentation.render(con, settings["presentation"]),
                preview=internal,
            )
            viewer_settings["upcoming_mode"] = settings["upcoming_mode"]
            language_links = [
                choice | {"href": (
                    "/?" + urlencode({"lang": choice["code"], **({"preview": token} if internal else {})})
                    if request.path == "/"
                    else url_for(
                        "event_series_view", slug=slug, lang=choice["code"], preview=token if internal else None
                    )
                )}
                for choice in options
            ]

        resp = make_response(
            render_template(
                "event_series_view.html",
                settings=viewer_settings,
                preview=internal,
                language=language,
                labels=event_series_locale.labels(language),
                language_links=language_links,
                **payload,
            )
        )
        resp.headers["Content-Language"] = language
        resp.headers["Cache-Control"] = "private, no-store"
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"

        if internal or audience != "public":
            resp.headers["X-Robots-Tag"] = "noindex"
            resp.headers["Referrer-Policy"] = "no-referrer"

        if ticket is not None:
            resp.set_cookie(
                "cueporch_series_access",
                ticket[0],
                path=request.path,
                samesite="Lax",
                httponly=True,
                secure=ticket[1],
                max_age=43200,
            )

        return resp
