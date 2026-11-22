"""Branded galleries with their own publication and expiring viewer access."""

import csv
from datetime import datetime, timezone
from io import StringIO
import json
import math
import re
from urllib.parse import urlsplit
import uuid

from flask import Response, abort, make_response, render_template, request, send_file, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import generate_password_hash

from catalog import video_version_key
from browser_account_security import is_loopback
import folder_slack_queue
import library_analytics
import library_details
import library_list_registration
import library_mailchimp_forms
import library_peer_handoff
import library_privacy as privacy
import library_registration
import library_video_questions
import media_bandwidth
from library_appearance import brand_image, stored_image_blob
from library_interactivity import image_id, selected_ids, text_field, web_link
from library_languages import original_language, track_key, tracks_for
from library_thumbnail import selected_thumbnail, thumbnail_type
from vimeo_privacy import target_settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS video_showcase(
 id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, revision TEXT NOT NULL,
 settings TEXT NOT NULL, password TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS video_showcase_migration(
 source TEXT PRIMARY KEY, showcase TEXT NOT NULL, inventory TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS video_showcase_analytics(
 session TEXT PRIMARY KEY, showcase TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS video_showcase_analytics_by_showcase ON video_showcase_analytics(showcase);
"""
DEFAULTS = dict(
    title="Untitled showcase", description="", visibility="private", domains=[], videos=[],
    logo=None, accent="#2b7e7d", dark=False, layout="grid", size="medium", search=True,
    copy_link=True, next_video=False, loop=False, indexable=False, seo_title="", seo_description="",
    seo_keywords="", headline="", summary="", button_label="", button_url="",
    feature_image=None, feature_alignment="start", navigation=False, product_brand=False, grid_media="thumbnails",
    download=False, questions=False, likes=False, comments=False,
    roku_enabled=False, roku_provider="", roku_language="en-US", roku_genres="",
)
VISIBILITIES = ("private", "password", "public", "embed_only")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")


def import_showcases(con, records, transfer_root):
    for record in records:
        prior = con.execute("SELECT showcase FROM video_showcase_migration WHERE source=?", (record["uri"],)).fetchone()
        uid = record["id"]
        snapshot = json.dumps(record)
        if prior:
            con.execute("UPDATE video_showcase_migration SET inventory=? WHERE source=?", (snapshot, record["uri"]))
        else:
            slug = uid if not con.execute("SELECT 1 FROM video_showcase WHERE slug=?", (uid,)).fetchone() else uuid.uuid4().hex
            settings = DEFAULTS | dict(title=record["title"], description=record["description"], videos=record["members"])
            style = record.get("source_style", {})
            settings.update({
                name: style[name]
                for name in ("accent", "dark", "layout", "next_video", "loop", "copy_link", "download",
                             "navigation", "seo_title", "seo_description", "seo_keywords")
                if name in style
            })
            asset = record.get("source_logo")
            if isinstance(asset, dict) and (blob := stored_image_blob(transfer_root, asset, asset.get("sha256"))):
                con.execute("INSERT OR IGNORE INTO review_logo(digest,mime,content) VALUES(?,?,?)",
                            (asset["sha256"], *blob))
                settings["logo"] = asset["sha256"]
            con.execute("INSERT INTO video_showcase VALUES(?,?,?,?,NULL,?)",
                        (uid, slug, uuid.uuid4().hex, json.dumps(settings), datetime.now(timezone.utc).isoformat()))
            con.execute("INSERT INTO video_showcase_migration VALUES(?,?,?)", (record["uri"], uid, snapshot))


def imported_targets(con):
    return dict(con.execute("""SELECT migration.source, showcase.id
        FROM video_showcase_migration migration
        JOIN video_showcase showcase ON showcase.id=migration.showcase""").fetchall())


def get_showcase(con, value, by_slug=False):
    key = "slug" if by_slug else "id"
    row = con.execute(f"SELECT * FROM video_showcase WHERE {key}=?", (value,)).fetchone()
    if row is None:
        abort(404)
    return row


def state(row):
    return DEFAULTS | json.loads(row["settings"])


def roku_visible(con, row, settings):
    return (settings["roku_enabled"] and settings["visibility"] == "public" and not settings["domains"]
            and not library_registration.active(con, "showcase", row["id"]))


def roku_origin(public_url):
    canonical = library_analytics.source_origin(public_url)
    if canonical.startswith("https://"):
        return canonical
    local = library_analytics.source_origin(request.host_url)
    if is_loopback(urlsplit(local).hostname) and is_loopback(request.remote_addr):
        return local
    return None


def summary(con, row, public_url):
    settings = state(row)
    mode = settings["visibility"]
    origin = roku_origin(public_url) if roku_visible(con, row, settings) else None
    return dict(id=row["id"], slug=row["slug"], revision=row["revision"], settings=settings,
                has_password=bool(row["password"]), created=row["created"],
                watch=url_for("showcase_main", slug=row["slug"]) if mode in ("public", "password") else None,
                embed=url_for("showcase_frame", slug=row["slug"]) if mode != "private" else None,
                roku=origin + url_for("showcase_roku", uid=row["id"]) if origin else None)


def validate_settings(con, raw, lookup):
    if not isinstance(raw, dict):
        abort(422, "Enter valid showcase settings.")
    values = dict(DEFAULTS)
    for name, limit in (("title", 128), ("description", 5000), ("seo_title", 140),
                        ("seo_description", 200), ("seo_keywords", 500), ("headline", 140),
                        ("summary", 2000), ("button_label", 100), ("roku_provider", 100),
                        ("roku_genres", 250)):
        values[name] = text_field(raw.get(name, DEFAULTS[name]), name.replace("_", " "), limit, name == "title")
    values["button_url"] = web_link(raw.get("button_url", ""))
    if bool(values["button_label"]) != bool(values["button_url"]):
        abort(422, "Enter both a featured button label and link.")
    values["logo"] = image_id(con, raw.get("logo"))
    values["feature_image"] = image_id(con, raw.get("feature_image"))
    accent = raw.get("accent", DEFAULTS["accent"])
    if not isinstance(accent, str) or not re.fullmatch(r"#[A-Fa-f0-9]{6}", accent):
        abort(422, "Choose a six-digit theme color.")
    values["accent"] = accent.lower()
    for name in ("dark", "search", "copy_link", "next_video", "loop", "indexable", "roku_enabled",
                 "navigation", "product_brand", "download", "questions", "likes", "comments"):
        value = raw.get(name, DEFAULTS[name])
        if type(value) is not bool:
            abort(422, "Choose valid layout and playback switches.")
        values[name] = value
    for name, options in (("visibility", VISIBILITIES), ("layout", ("grid", "featured")),
                          ("size", ("small", "medium", "large", "extra-large")),
                          ("feature_alignment", ("start", "center", "end")),
                          ("grid_media", ("thumbnails", "players"))):
        value = raw.get(name, DEFAULTS[name])
        if value not in options:
            abort(422, f"Choose a valid {name}.")
        values[name] = value
    domains = raw.get("domains", [])
    if not isinstance(domains, list) or len(domains) > 50:
        abort(422, "Choose up to 50 allowed embed sites.")
    try:
        values["domains"] = sorted({privacy.normalize_domain(domain) for domain in domains})
    except (ValueError, UnicodeError):
        abort(422, "Use domain names and optional ports without paths.")
    members = raw.get("videos", [])
    if not isinstance(members, list) or any(not isinstance(item, str) or item not in lookup for item in members):
        abort(422, "Choose existing library videos.")
    values["videos"] = list(dict.fromkeys(members))
    language = text_field(raw.get("roku_language", DEFAULTS["roku_language"]), "a channel language", 35, True)
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}", language):
        abort(422, "Use a channel language tag such as en-US.")
    values["roku_language"] = language
    genres = [genre.strip().casefold().replace(" ", "-") for genre in values["roku_genres"].split(",") if genre.strip()]
    if len(genres) > 8 or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", genre) for genre in genres):
        abort(422, "Enter up to eight comma-separated genres using letters, numbers or hyphens.")
    if values["roku_enabled"] and (not values["roku_provider"] or not genres):
        abort(422, "Enter a provider and at least one genre for Roku.")
    values["roku_genres"] = ", ".join(dict.fromkeys(genres))
    return values


def content_policy(con, work, permits, showcase, embed, ancestor, internal):
    uid = work["id"]
    file = privacy.playback(work)
    if file is None or privacy.blocked(con, uid):
        return None
    pending = con.execute("SELECT 1 FROM library_privacy_change WHERE work=?", (uid,)).fetchone()
    handoff = library_peer_handoff.get(con, uid)
    if pending or (handoff and handoff["state"] == "pending"):
        return None
    row = privacy.get_privacy(con, uid)
    policy = json.loads(row["settings"]) if row else privacy.DEFAULTS
    public_route = con.execute("SELECT 1 FROM route WHERE slug=? AND kind IN ('work','meeting')", (uid,)).fetchone()
    if not row and public_route:
        policy = (target_settings(work)[0] if work["provider"] == "vimeo"
                  else dict(privacy.DEFAULTS, visibility="public", embed="anywhere"))
    if internal:
        return file, policy
    if not permits(work, policy):
        return None
    mode = policy["visibility"]
    if showcase["visibility"] == "public" and mode != "public":
        return None
    if not embed and (showcase["visibility"] == "public" or mode == "public"):
        return (file, policy) if mode == "public" else None
    if mode == "private":
        return None
    if showcase["visibility"] != "password" and mode not in ("public", "embed_only"):
        return None
    if policy["embed"] == "disabled":
        return None
    if policy["embed"] == "domains" and not privacy.allowed_ancestor(ancestor, policy["domains"]):
        return None
    return file, policy


def register(app, tx, staff, read_works, title_for, root, operator, permits, public_url, questions_enabled):
    access = URLSafeTimedSerializer(operator, salt="cueporch-showcase-access")
    media = URLSafeTimedSerializer(operator, salt="cueporch-showcase-media")
    registration = URLSafeTimedSerializer(operator, salt="cueporch-showcase-registration")
    telemetry = URLSafeTimedSerializer(operator, salt="cueporch-showcase-analytics")
    trusted_proxies = library_analytics.configured_proxies()

    def library():
        return {work["id"]: work for work in read_works() if work.get("asset_type", "video") == "video"}

    @app.get("/operator/showcases")
    def operator_showcases():
        return render_template("showcase_editor.html", public_url=public_url)

    @app.route("/api/showcases", methods=["GET", "POST"])
    def all_showcases():
        staff()
        with tx() as con:
            if request.method == "GET":
                return [summary(con, row, public_url)
                        for row in con.execute("SELECT * FROM video_showcase ORDER BY created DESC")]
            uid = uuid.uuid4().hex
            con.execute("INSERT INTO video_showcase VALUES(?,?,?,?,NULL,?)",
                        (uid, uid, uuid.uuid4().hex, json.dumps(DEFAULTS), datetime.now(timezone.utc).isoformat()))
            return summary(con, get_showcase(con, uid), public_url), 201

    @app.post("/api/showcases/selected-videos")
    def add_selected_videos():
        staff()
        request.max_content_length = 128 * 1024
        body = request.get_json()
        if not isinstance(body, dict):
            abort(422, "Select videos and showcases.")
        works = selected_ids(body.get("works"), "videos")
        destinations = selected_ids(body.get("showcases"), "showcases")
        added = 0
        with tx() as con:
            available = library()
            if any(work not in available or privacy.blocked(con, work) for work in works):
                abort(422, "Choose existing videos that are available for showcases.")
            rows = [get_showcase(con, uid) for uid in destinations]
            for row in rows:
                values = state(row)
                members = set(values["videos"])
                new = [work for work in works if work not in members]
                if new:
                    values["videos"].extend(new)
                    con.execute("UPDATE video_showcase SET revision=?,settings=? WHERE id=?",
                                (uuid.uuid4().hex, json.dumps(values), row["id"]))
                    for work in new:
                        folder_slack_queue.enqueue(con, work, "showcase")
                    added += len(new)
        return dict(showcases=len(rows), added=added)

    @app.route("/api/showcases/<uid>", methods=["GET", "PUT", "DELETE", "POST"])
    def operator_showcase(uid):
        staff()
        with tx() as con:
            row = get_showcase(con, uid)
            if request.method == "GET":
                return summary(con, row, public_url)
            if request.method == "DELETE":
                con.execute("DELETE FROM video_showcase WHERE id=?", (uid,))
                con.execute("DELETE FROM viewer_form WHERE kind='showcase' AND subject=?", (uid,))
                con.execute("DELETE FROM viewer_registrant WHERE kind='showcase' AND subject=?", (uid,))
                library_mailchimp_forms.stop(con,"showcase",uid)
                library_list_registration.stop_all(con,"showcase",uid)
                return "", 204
            if request.method == "POST":
                new_id = uuid.uuid4().hex
                values = state(row)
                values.update(title=(values["title"][:118] + " (copy)"), visibility="private", indexable=False,
                              roku_enabled=False)
                con.execute("INSERT INTO video_showcase VALUES(?,?,?,?,NULL,?)",
                            (new_id, new_id, uuid.uuid4().hex, json.dumps(values), datetime.now(timezone.utc).isoformat()))
                return summary(con, get_showcase(con, new_id), public_url), 201
            request.max_content_length = 512 * 1024
            body = request.get_json()
            if not isinstance(body, dict) or body.get("revision") != row["revision"]:
                abort(409, "This showcase changed. Reopen it before saving.")
            slug = body.get("slug")
            if not isinstance(slug, str) or not SLUG.fullmatch(slug):
                abort(422, "Use a showcase link name with letters, numbers and hyphens.")
            if con.execute("SELECT 1 FROM video_showcase WHERE slug=? AND id<>?", (slug, uid)).fetchone():
                abort(409, "This showcase link name is already used.")
            values = validate_settings(con, body.get("settings"), library())
            password = body.get("password", "")
            if not isinstance(password, str) or len(password) > 256:
                abort(422, "Enter a password of up to 256 characters.")
            hashed = (generate_password_hash(password) if password else row["password"]) if values["visibility"] == "password" else None
            if values["visibility"] == "password" and not hashed:
                abort(422, "Set a password for this showcase.")
            if values != state(row) or slug != row["slug"] or hashed != row["password"]:
                con.execute("UPDATE video_showcase SET slug=?,revision=?,settings=?,password=? WHERE id=?",
                            (slug, uuid.uuid4().hex, json.dumps(values), hashed, uid))
                previous = set(state(row)["videos"])
                for work in values["videos"]:
                    if work not in previous:
                        folder_slack_queue.enqueue(con, work, "showcase")
            return summary(con, get_showcase(con, uid), public_url)

    @app.post("/api/showcases/<uid>/preview")
    def issue_showcase_preview(uid):
        staff()
        with tx() as con:
            row = get_showcase(con, uid)
            ticket = access.dumps([uid, row["revision"], "operator"])
            return dict(preview=url_for("showcase_main", slug=row["slug"], preview=ticket))

    @app.route("/api/showcases/<uid>/registration", methods=["GET", "PUT"])
    def showcase_registration(uid):
        staff()
        with tx() as con:
            get_showcase(con, uid)
            return library_registration.operator_form(con, "showcase", uid)

    @app.get("/api/showcases/<uid>/registration/export")
    def export_showcase_registration(uid):
        staff()
        with tx() as con:
            get_showcase(con, uid)
            return library_registration.export(con, "showcase", uid)

    @app.get("/api/showcases/<uid>/analytics")
    def showcase_analytics_report(uid):
        staff()
        first, last, start, end = library_analytics.bounds()
        with tx() as con:
            get_showcase(con, uid)
            counts = library_analytics.COUNTS
            scope = """FROM video_showcase_analytics g JOIN video_analytics_session s ON s.id=g.session
                       WHERE g.showcase=? AND s.created>=? AND s.created<?"""
            args = (uid, start, end)
            totals = library_analytics.metrics(con.execute(f"SELECT {counts} {scope}", args).fetchone())
            rows = con.execute(f"""SELECT date(s.created, 'unixepoch') AS day,{counts} {scope}
                                   GROUP BY day ORDER BY day""", args)
            daily = [dict(day=row["day"], **library_analytics.metrics(row)) for row in rows]
        if request.args.get("format") == "csv":
            output = StringIO()
            fields = ["day", "impressions", "views", "finishes", "watched_seconds"]
            writer = csv.DictWriter(output, fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(daily)
            return Response(output.getvalue(), mimetype="text/csv", headers={
                "Content-Disposition": "attachment; filename=cueporch-showcase-analytics.csv",
                "Cache-Control": "private, no-store"})
        response = make_response(dict(date_from=first.isoformat(), date_to=last.isoformat(), totals=totals, daily=daily))
        response.cache_control.private = True
        response.cache_control.no_store = True
        return response

    def unlock(row):
        with tx() as con:
            return privacy.try_password(con, operator, row["id"], request.form.get("password", ""), row["password"])

    def viewer(slug, embed):
        with tx() as con:
            row = get_showcase(con, slug, by_slug=True)
        values = state(row)
        mode = values["visibility"]
        internal = False
        ancestor = request.referrer or ""
        ancestors = "'none'" if not embed else "*"
        if preview := request.args.get("preview"):
            try:
                internal = access.loads(preview, max_age=600) == [row["id"], row["revision"], "operator"]
            except BadSignature:
                abort(404)
            if not internal:
                abort(404)
            ancestors = "'self'"
        if not internal:
            if mode == "private" or (mode == "embed_only" and not embed):
                abort(404)
            if embed:
                if request.method == "POST":
                    try:
                        owner, revision, ancestor = access.loads(request.form.get("frame", ""), max_age=1200)
                    except (BadSignature, ValueError):
                        abort(403)
                    if (owner, revision) != (row["id"], row["revision"]):
                        abort(403)
                if not ancestor or (values["domains"] and not privacy.allowed_ancestor(ancestor, values["domains"])):
                    abort(403, "This showcase is available only on an allowed website.")
                if values["domains"]:
                    ancestors = privacy.frame_policy(values["domains"])
        if not embed:
            ancestor = request.host_url
        origin = library_analytics.source_origin(ancestor)
        if embed and origin == "Unknown":
            abort(403)
        enrollment = request.form.get("enrollment", "")
        locked = not internal and not enrollment and mode == "password" and (request.method != "POST" or not unlock(row))
        entries = []
        frame = access.dumps([row["id"], row["revision"], origin]) if embed else None
        logo = None
        feature_image = None
        form = None
        form_token = None
        form_error = None
        registration_view = None
        if not locked:
            with tx() as con:
                current = get_showcase(con, row["id"])
                if current["revision"] != row["revision"]:
                    abort(409, "Reopen the showcase. Its settings have changed.")
                form = None if internal else library_registration.active(con, "showcase", row["id"])
                form_revision = form["revision"] if form else None
                scope = [row["id"], row["revision"], form_revision, origin, embed]
                admitted = False
                sign_on = None
                if enrollment:
                    try:
                        issued = registration.loads(enrollment, max_age=1200)
                    except BadSignature:
                        abort(403, "Open the registration form again.")
                    if not form or scope != issued:
                        abort(403, "Open the registration form again.")
                    admitted, sign_on, form_error = library_registration.admit(con, "showcase", row["id"], form, scope)
                if form and not admitted:
                    form_token = registration.dumps(scope)
                    registration_view = library_registration.presentation(con, form, sign_on, mailchimp_target=("showcase", row["id"], scope))
                logo = brand_image(con, values["logo"])
                if not form_token:
                    feature_image = brand_image(con, values["feature_image"])
                    context = (library_analytics.request_context(trusted_proxies)
                               if not internal and library_analytics.permitted() else None)
                    works = library()
                    for uid in values["videos"]:
                        work = works.get(uid)
                        if not work:
                            continue
                        member_form = None if internal else library_registration.active(con, "video", uid)
                        if member_form and member_form["timing"] == "before":
                            continue
                        member_revision = member_form["revision"] if member_form else None
                        allowed = content_policy(con, work, permits, values, embed, origin, internal)
                        if not allowed:
                            continue
                        _, sharing = allowed
                        policy = privacy.get_privacy(con, uid)
                        token = media.dumps([row["id"], row["revision"], uid, video_version_key(work),
                                             policy["revision"] if policy else None, internal, embed, origin, form_revision, member_revision])
                        member_registration = (
                            dict(timing=member_form["timing"], time_seconds=member_form["time_seconds"],
                                 url=url_for("showcase_member_registration", token=token))
                            if member_form else None
                        )
                        details = library_details.details_for(con, work)
                        sample = telemetry.dumps([token, uuid.uuid4().hex, context]) if context else None
                        questions = (question_session(con, work, token, "embed" if embed else "watch")
                                     if not internal and values["questions"] else None)
                        community = not embed and not internal and sharing["visibility"] == "public"
                        entries.append(dict(id=uid, title=title_for(con, work),
                                            description=details.get("description", ""),
                                            media=url_for("showcase_file", token=token),
                                            download=url_for("showcase_file", token=token, download=1)
                                            if not internal and values["download"] and sharing.get("download") else None,
                                            analytics=url_for("showcase_analytics_sample", sample=sample) if sample else None,
                                            questions=questions,
                                            registration=member_registration,
                                            likes=community and values["likes"],
                                            comments=community and values["comments"],
                                            poster=url_for("showcase_file", token=token, poster=1)
                                            if selected_thumbnail(con, work) else None,
                                            captions=[dict(language=track.get("language") or "und", key=track_key(track),
                                                           url=url_for("showcase_file", token=token, track=index))
                                                      for index, track in enumerate(tracks_for(work))]))
        selection = request.args.get("video")
        playing = next((index for index, entry in enumerate(entries) if entry["id"] == selection), 0)
        response = make_response(render_template("showcase_view.html", settings=values, entries=entries, logo=logo,
                                                playing=playing,
                                                feature_image=feature_image,
                                                locked=locked, wrong=locked and request.method == "POST",
                                                unlock_token=frame, embed=embed, internal=internal,
                                                registration=registration_view, enrollment=form_token,
                                                registration_error=form_error),
                                 401 if locked and request.method == "POST" else 422 if form_error else 200)
        response.cache_control.no_store = True
        response.cache_control.private = True
        response.headers["Content-Security-Policy"] = f"frame-ancestors {ancestors}"
        response.headers["Referrer-Policy"] = "no-referrer"
        if internal or mode != "public" or not values["indexable"] or embed:
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.route("/showcases/<slug>", methods=["GET", "POST"])
    def showcase_main(slug):
        return viewer(slug, False)

    @app.route("/showcases/<slug>/embed", methods=["GET", "POST"])
    def showcase_frame(slug):
        return viewer(slug, True)

    def authorized_media(con, token):
        try:
            uid, version, work_id, cut, privacy_revision, internal, embed, origin, form_revision, *member_tail = media.loads(token, max_age=3600)
            if len(member_tail) > 1:
                raise ValueError
            member_revision = member_tail[0] if member_tail else None
            if internal:
                media.loads(token, max_age=600)
        except (BadSignature, ValueError):
            abort(404)
        row = get_showcase(con, uid)
        values = state(row)
        work = library().get(work_id)
        policy = privacy.get_privacy(con, work_id)
        if (row["revision"] != version or work_id not in values["videos"] or not work
                or video_version_key(work) != cut or (policy["revision"] if policy else None) != privacy_revision):
            abort(404)
        member_form = None if internal else library_registration.active(con, "video", work_id)
        current_member_revision = member_form["revision"] if member_form else None
        if (member_revision != current_member_revision
                or (member_form and member_form["timing"] == "before")):
            abort(404)
        form = None if internal else library_registration.active(con, "showcase", uid)
        if (form["revision"] if form else None) != form_revision:
            abort(404)
        allowed = content_policy(con, work, permits, values, embed, origin, internal)
        if not allowed:
            abort(404)
        return uid, work, values, allowed, internal, embed

    @app.route("/showcase-registration/<token>", methods=["GET", "POST"])
    def showcase_member_registration(token):
        if request.headers.get("Sec-Fetch-Dest") not in ("iframe", "frame"):
            abort(404)
        with tx() as con:
            _, work, values, _, internal, embed = authorized_media(con, token)
            if internal:
                abort(404)
            form = library_registration.active(con, "video", work["id"])
            if not form or form["timing"] not in ("during", "after"):
                abort(404)
            scope = ["showcase-member", token, form["revision"]]
            admitted = False
            sign_on = None
            error = None
            if request.method == "POST":
                try:
                    enrollment_payload = registration.loads(
                        request.form.get("enrollment", ""), max_age=1200
                    )
                except BadSignature:
                    abort(403)
                if enrollment_payload != scope:
                    abort(403)
                admitted, sign_on, error = library_registration.admit(
                    con, "video", work["id"], form, scope
                )
            registration_view = None
            if not admitted:
                registration_view = library_registration.presentation(
                    con,
                    form,
                    sign_on,
                    mailchimp_target=("video", work["id"], scope),
                )
            html = render_template(
                "showcase_member_registration.html",
                complete=admitted,
                registration=registration_view,
                enrollment=registration.dumps(scope),
                registration_error=error,
            )
            response = make_response(html, 422 if error else 200)
            response.headers["Cache-Control"] = "private, no-store"
            if not embed:
                frame_ancestors = "'self'"
            elif values.get("domains"):
                frame_ancestors = "'self' " + privacy.frame_policy(values["domains"])
            else:
                frame_ancestors = "*"
            response.headers["Content-Security-Policy"] = (
                "frame-ancestors " + frame_ancestors
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

    def question_work(con, token):
        _, work, values, _, internal, _ = authorized_media(con, token)
        if internal or not values["questions"]:
            abort(404)
        return work

    question_session = library_video_questions.register(
        app, tx, root, operator.encode("utf-8"), library_analytics.source_origin(public_url), questions_enabled,
        question_work, resource="showcase-questions",
    )

    @app.post("/showcase-observation/<sample>")
    def showcase_analytics_sample(sample):
        try:
            token, session, context = telemetry.loads(sample, max_age=3600)
        except (BadSignature, ValueError):
            abort(404)
        request.max_content_length = 128 * 1024
        with tx() as con:
            uid, work, _, _, internal, _ = authorized_media(con, token)
            if internal:
                abort(404)
            if library_analytics.permitted():
                library_analytics.record(con, work, session, "showcase", request.get_json(), context,
                                         source_language=original_language(work))
                con.execute("INSERT OR IGNORE INTO video_showcase_analytics VALUES(?,?)", (session, uid))
        return "", 204

    @app.get("/showcase-media/<token>")
    def showcase_file(token):
        with tx() as con:
            _, work, values, allowed, internal, _ = authorized_media(con, token)
            copy, sharing = allowed
            download = request.args.get("download") == "1"
            if download and (internal or not values["download"] or not sharing.get("download")
                             or "poster" in request.args or "track" in request.args):
                abort(403)
            digest, kind = copy["sha256"], "video/mp4"
            if request.args.get("poster") == "1":
                digest, kind = selected_thumbnail(con, work), None
            if "track" in request.args:
                index = request.args.get("track", -1, type=int)
                tracks = tracks_for(work)
                if not 0 <= index < len(tracks):
                    abort(404)
                digest, kind = tracks[index]["sha256"], "text/vtt"
        return serve_file(work["id"], digest, kind, download)

    def serve_file(work_id, digest, kind, download=False):
        path = root / "objects" / digest if digest else None
        if path is None or not path.is_file():
            abort(404)
        kind = kind or thumbnail_type(path)
        if not kind:
            abort(415)
        response = send_file(path, mimetype=kind, conditional=True, max_age=0, as_attachment=download,
                             download_name="video.mp4" if download else None)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.cache_control.no_store = True
        response.cache_control.private = True
        return media_bandwidth.measure(response, tx, work_id) if kind == "video/mp4" else response

    def roku_context(con, uid):
        row = get_showcase(con, uid)
        settings = state(row)
        if not roku_visible(con, row, settings):
            abort(404)
        canonical = roku_origin(public_url)
        if not canonical:
            abort(404)
        return settings, canonical

    def roku_entry(con, work, settings):
        if not work or library_registration.active(con, "video", work["id"]):
            return None
        # A device does not arrive from an allowed embed site. Apply the existing
        # embed rules as well as public showcase and source privacy.
        allowed = content_policy(con, work, permits, settings, True, "", False)
        return allowed[0] if allowed else None

    @app.get("/showcases/tv/roku/<uid>.json")
    def showcase_roku(uid):
        with tx() as con:
            settings, canonical = roku_context(con, uid)
            works = library()
            entries = []
            for work_id in settings["videos"]:
                work = works.get(work_id)
                copy = roku_entry(con, work, settings)
                if not copy or not (root / "objects" / copy["sha256"]).is_file():
                    continue
                details = library_details.details_for(con, work)
                title = title_for(con, work)
                description = details.get("description") or title
                url = canonical + url_for("showcase_roku_media", uid=uid, work_id=work_id,
                                          digest=copy["sha256"], kind="video")
                content = dict(language=settings["roku_language"], videos=[dict(url=url, videoType="MP4")])
                duration = work.get("duration_seconds")
                if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
                    content["duration"] = math.ceil(duration)
                added = work.get("library_added_at")
                if isinstance(added, str) and added:
                    content["dateAdded"] = added
                entry = dict(id=work_id, title=title, shortDescription=description[:200], longDescription=description,
                             genres=settings["roku_genres"].split(", "), tags=details.get("tags", []), content=content)
                poster = selected_thumbnail(con, work)
                if poster:
                    path = root / "objects" / poster
                    if path.is_file() and thumbnail_type(path) in ("image/jpeg", "image/png"):
                        entry["thumbnail"] = canonical + url_for("showcase_roku_media", uid=uid, work_id=work_id,
                                                                 digest=poster, kind="poster")
                entries.append(entry)
        response = make_response(dict(providerName=settings["roku_provider"], language=settings["roku_language"],
                                      lastUpdated=datetime.now(timezone.utc).isoformat(), shortFormVideos=entries))
        response.cache_control.no_store = True
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.get("/showcases/tv/roku/<uid>/<work_id>/<any(video,poster):kind>/<digest>")
    def showcase_roku_media(uid, work_id, kind, digest):
        with tx() as con:
            settings, _ = roku_context(con, uid)
            if work_id not in settings["videos"]:
                abort(404)
            work = library().get(work_id)
            copy = roku_entry(con, work, settings)
            if not copy:
                abort(404)
            current = selected_thumbnail(con, work) if kind == "poster" else copy["sha256"]
            if not current or current != digest:
                abort(404)
        return serve_file(work_id, digest, "video/mp4" if kind == "video" else None)
