"""Branded galleries with their own publication and expiring viewer access."""

from datetime import datetime, timezone
import json
import re
import uuid

from flask import abort, make_response, render_template, request, send_file, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import generate_password_hash

from catalog import video_version_key
import library_peer_handoff
import library_privacy as privacy
import library_registration
import media_bandwidth
from library_analytics import source_origin
from library_appearance import brand_image
from library_interactivity import image_id, text_field, web_link
from library_languages import tracks_for
from library_thumbnail import selected_thumbnail, thumbnail_type
from vimeo_privacy import target_settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS video_showcase(
 id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, revision TEXT NOT NULL,
 settings TEXT NOT NULL, password TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS video_showcase_migration(
 source TEXT PRIMARY KEY, showcase TEXT NOT NULL, inventory TEXT NOT NULL);
"""
DEFAULTS = dict(
    title="Untitled showcase", description="", visibility="private", domains=[], videos=[],
    logo=None, accent="#2b7e7d", dark=False, layout="grid", size="medium", search=True,
    copy_link=True, next_video=False, loop=False, indexable=False, seo_title="", seo_description="",
    seo_keywords="", headline="", summary="", button_label="", button_url="",
)
VISIBILITIES = ("private", "password", "public", "embed_only")
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")


def import_showcases(con, records):
    for record in records:
        prior = con.execute("SELECT showcase FROM video_showcase_migration WHERE source=?", (record["uri"],)).fetchone()
        uid = record["id"]
        snapshot = json.dumps(record)
        if prior:
            con.execute("UPDATE video_showcase_migration SET inventory=? WHERE source=?", (snapshot, record["uri"]))
        else:
            slug = uid if not con.execute("SELECT 1 FROM video_showcase WHERE slug=?", (uid,)).fetchone() else uuid.uuid4().hex
            settings = DEFAULTS | dict(title=record["title"], description=record["description"], videos=record["members"])
            con.execute("INSERT INTO video_showcase VALUES(?,?,?,?,NULL,?)",
                        (uid, slug, uuid.uuid4().hex, json.dumps(settings), datetime.now(timezone.utc).isoformat()))
            con.execute("INSERT INTO video_showcase_migration VALUES(?,?,?)", (record["uri"], uid, snapshot))
        chosen = prior["showcase"] if prior else uid
        record["target"] = chosen if con.execute("SELECT 1 FROM video_showcase WHERE id=?", (chosen,)).fetchone() else None


def get_showcase(con, value, by_slug=False):
    key = "slug" if by_slug else "id"
    row = con.execute(f"SELECT * FROM video_showcase WHERE {key}=?", (value,)).fetchone()
    if row is None:
        abort(404)
    return row


def state(row):
    return DEFAULTS | json.loads(row["settings"])


def summary(row):
    settings = state(row)
    mode = settings["visibility"]
    return dict(id=row["id"], slug=row["slug"], revision=row["revision"], settings=settings,
                has_password=bool(row["password"]), created=row["created"],
                watch=url_for("showcase_main", slug=row["slug"]) if mode in ("public", "password") else None,
                embed=url_for("showcase_frame", slug=row["slug"]) if mode != "private" else None)


def validate_settings(con, raw, lookup):
    if not isinstance(raw, dict):
        abort(422, "Enter valid showcase settings.")
    values = dict(DEFAULTS)
    for name, limit in (("title", 128), ("description", 5000), ("seo_title", 140),
                        ("seo_description", 200), ("seo_keywords", 500), ("headline", 140),
                        ("summary", 2000), ("button_label", 100)):
        values[name] = text_field(raw.get(name, DEFAULTS[name]), name.replace("_", " "), limit, name == "title")
    values["button_url"] = web_link(raw.get("button_url", ""))
    if bool(values["button_label"]) != bool(values["button_url"]):
        abort(422, "Enter both a featured button label and link.")
    values["logo"] = image_id(con, raw.get("logo"))
    accent = raw.get("accent", DEFAULTS["accent"])
    if not isinstance(accent, str) or not re.fullmatch(r"#[A-Fa-f0-9]{6}", accent):
        abort(422, "Choose a six-digit theme color.")
    values["accent"] = accent.lower()
    for name in ("dark", "search", "copy_link", "next_video", "loop", "indexable"):
        value = raw.get(name, DEFAULTS[name])
        if type(value) is not bool:
            abort(422, "Choose valid layout and playback switches.")
        values[name] = value
    for name, options in (("visibility", VISIBILITIES), ("layout", ("grid", "featured")),
                          ("size", ("small", "medium", "large", "extra-large"))):
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
        return file
    if not permits(work, policy):
        return None
    mode = policy["visibility"]
    if showcase["visibility"] == "public" and mode != "public":
        return None
    if not embed and (showcase["visibility"] == "public" or mode == "public"):
        return file if mode == "public" else None
    if mode == "private":
        return None
    if showcase["visibility"] != "password" and mode not in ("public", "embed_only"):
        return None
    if policy["embed"] == "disabled":
        return None
    if policy["embed"] == "domains" and not privacy.allowed_ancestor(ancestor, policy["domains"]):
        return None
    return file


def register(app, tx, staff, read_works, title_for, root, operator, permits, public_url):
    access = URLSafeTimedSerializer(operator, salt="cueporch-showcase-access")
    media = URLSafeTimedSerializer(operator, salt="cueporch-showcase-media")
    registration = URLSafeTimedSerializer(operator, salt="cueporch-showcase-registration")

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
                return [summary(row) for row in con.execute("SELECT * FROM video_showcase ORDER BY created DESC")]
            uid = uuid.uuid4().hex
            con.execute("INSERT INTO video_showcase VALUES(?,?,?,?,NULL,?)",
                        (uid, uid, uuid.uuid4().hex, json.dumps(DEFAULTS), datetime.now(timezone.utc).isoformat()))
            return summary(get_showcase(con, uid)), 201

    @app.route("/api/showcases/<uid>", methods=["GET", "PUT", "DELETE", "POST"])
    def operator_showcase(uid):
        staff()
        with tx() as con:
            row = get_showcase(con, uid)
            if request.method == "GET":
                return summary(row)
            if request.method == "DELETE":
                con.execute("DELETE FROM video_showcase WHERE id=?", (uid,))
                con.execute("DELETE FROM viewer_form WHERE kind='showcase' AND subject=?", (uid,))
                con.execute("DELETE FROM viewer_registrant WHERE kind='showcase' AND subject=?", (uid,))
                return "", 204
            if request.method == "POST":
                new_id = uuid.uuid4().hex
                values = state(row)
                values.update(title=(values["title"][:118] + " (copy)"), visibility="private", indexable=False)
                con.execute("INSERT INTO video_showcase VALUES(?,?,?,?,NULL,?)",
                            (new_id, new_id, uuid.uuid4().hex, json.dumps(values), datetime.now(timezone.utc).isoformat()))
                return summary(get_showcase(con, new_id)), 201
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
            return summary(get_showcase(con, uid))

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
        origin = source_origin(ancestor)
        if embed and origin == "Unknown":
            abort(403)
        enrollment = request.form.get("enrollment", "")
        locked = not internal and not enrollment and mode == "password" and (request.method != "POST" or not unlock(row))
        entries = []
        frame = access.dumps([row["id"], row["revision"], origin]) if embed else None
        logo = None
        form = None
        form_token = None
        form_error = None
        if not locked:
            with tx() as con:
                current = get_showcase(con, row["id"])
                if current["revision"] != row["revision"]:
                    abort(409, "Reopen the showcase. Its settings have changed.")
                form = None if internal else library_registration.active(con, "showcase", row["id"])
                form_revision = form["revision"] if form else None
                scope = [row["id"], row["revision"], form_revision, origin, embed]
                if enrollment:
                    try:
                        issued = registration.loads(enrollment, max_age=1200)
                    except BadSignature:
                        abort(403, "Open the registration form again.")
                    if not form or scope != issued:
                        abort(403, "Open the registration form again.")
                    form_error = library_registration.enroll(con, "showcase", row["id"], form, request.form, request.args)
                if form and (not enrollment or form_error):
                    form_token = registration.dumps(scope)
                logo = brand_image(con, values["logo"])
                if not form_token:
                    works = library()
                    for uid in values["videos"]:
                        work = works.get(uid)
                        if not work or (not internal and library_registration.active(con, "video", uid)):
                            continue
                        if not content_policy(con, work, permits, values, embed, origin, internal):
                            continue
                        policy = privacy.get_privacy(con, uid)
                        token = media.dumps([row["id"], row["revision"], uid, video_version_key(work),
                                             policy["revision"] if policy else None, internal, embed, origin, form_revision])
                        entries.append(dict(id=uid, title=title_for(con, work), media=url_for("showcase_file", token=token),
                                            poster=url_for("showcase_file", token=token, poster=1)
                                            if selected_thumbnail(con, work) else None,
                                            captions=[dict(language=track.get("language") or "und",
                                                           url=url_for("showcase_file", token=token, track=index))
                                                      for index, track in enumerate(tracks_for(work))]))
        response = make_response(render_template("showcase_view.html", settings=values, entries=entries, logo=logo,
                                                locked=locked, wrong=locked and request.method == "POST",
                                                unlock_token=frame, embed=embed, internal=internal,
                                                registration=form if form_token else None, enrollment=form_token,
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

    @app.get("/showcase-media/<token>")
    def showcase_file(token):
        try:
            uid, version, work_id, cut, privacy_revision, internal, embed, origin, form_revision = media.loads(token, max_age=3600)
            if internal:
                media.loads(token, max_age=600)
        except (BadSignature, ValueError):
            abort(404)
        with tx() as con:
            row = get_showcase(con, uid)
            values = state(row)
            work = library().get(work_id)
            policy = privacy.get_privacy(con, work_id)
            if (row["revision"] != version or work_id not in values["videos"] or not work
                    or video_version_key(work) != cut or (policy["revision"] if policy else None) != privacy_revision
                    or (not internal and library_registration.active(con, "video", work_id))):
                abort(404)
            form = None if internal else library_registration.active(con, "showcase", uid)
            if (form["revision"] if form else None) != form_revision:
                abort(404)
            copy = content_policy(con, work, permits, values, embed, origin, internal)
            if not copy:
                abort(404)
            digest, kind = copy["sha256"], "video/mp4"
            if request.args.get("poster") == "1":
                digest, kind = selected_thumbnail(con, work), None
            if "track" in request.args:
                index = request.args.get("track", -1, type=int)
                tracks = tracks_for(work)
                if not 0 <= index < len(tracks):
                    abort(404)
                digest, kind = tracks[index]["sha256"], "text/vtt"
        path = root / "objects" / digest if digest else None
        if path is None or not path.is_file():
            abort(404)
        kind = kind or thumbnail_type(path)
        if not kind:
            abort(415)
        response = send_file(path, mimetype=kind, conditional=True, max_age=0)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.cache_control.no_store = True
        response.cache_control.private = True
        return media_bandwidth.measure(response, tx, work_id) if kind == "video/mp4" else response
