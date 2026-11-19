"""Policy and scoped video sharing for the CuePorch library."""

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

from flask import abort, make_response, render_template, request, send_file, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

import library_peer_handoff
from library_trash import hidden

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_privacy(
 work TEXT PRIMARY KEY, settings TEXT NOT NULL, password_hash TEXT,
 revision TEXT NOT NULL, share_key TEXT);
CREATE TABLE IF NOT EXISTS library_privacy_change(
 work TEXT PRIMARY KEY, settings TEXT NOT NULL, password_hash TEXT,
 revision TEXT NOT NULL, share_key TEXT, approval TEXT NOT NULL, listing TEXT);
CREATE TABLE IF NOT EXISTS library_privacy_attempt(
 work TEXT NOT NULL, visitor TEXT NOT NULL, since REAL NOT NULL, attempts INTEGER NOT NULL,
 PRIMARY KEY(work,visitor));
CREATE TABLE IF NOT EXISTS library_upload_defaults(
 actor TEXT PRIMARY KEY, settings TEXT NOT NULL, password_hash TEXT);
"""
MODES = {"private", "password", "unlisted", "public", "embed_only"}
DEFAULTS = dict(visibility="private", embed="disabled", domains=[], download=False)
DOMAIN_PATTERN = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


def playback(work):
    return next(
        (
            item
            for role in ("playback", "original")
            for item in work["files"]
            if item["role"] == role and item["format"] == "mp4"
        ),
        None,
    )


def captions(work):
    return [f for f in work["files"] if f["role"] == "caption" and f["format"] == "vtt"]


def normalize_domain(value):
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("Enter an embed domain such as example.org.")
    parts = urlsplit("//" + value.strip())
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("Enter embed domains without paths or credentials.")
    domain = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    try:
        address = ipaddress.ip_address(domain)
        domain = f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        labels = domain.split(".")
        if (domain != "localhost" and len(labels) < 2) or not all(map(DOMAIN_PATTERN.fullmatch, labels)):
            raise ValueError("Enter an embed domain such as example.org.") from None
    return f"{domain}:{parts.port}" if parts.port is not None else domain


def requested_settings(body, previous_hash=None):
    if not isinstance(body, dict):
        abort(400)
    visibility = body.get("visibility")
    embed = body.get("embed")
    domains = body.get("domains", [])
    password = body.get("password", "")
    download = body.get("download", False)
    if not isinstance(visibility, str) or visibility not in MODES:
        abort(400, "Choose a visibility setting.")
    if embed not in ("disabled", "anywhere", "domains"):
        abort(400, "Choose where the player may be embedded.")
    if not isinstance(domains, list) or len(domains) > 50 or type(download) is not bool:
        abort(400, "Use valid embed and download settings.")
    if not isinstance(password, str) or len(password) > 256:
        abort(400, "Enter a password of up to 256 characters.")
    if visibility == "private":
        return DEFAULTS, None
    if visibility == "embed_only" and embed == "disabled":
        abort(400, "Choose at least one place to embed an embed-only video.")
    if embed == "domains":
        try:
            domains = sorted(set(map(normalize_domain, domains)))
        except (ValueError, UnicodeError):
            abort(400, "Use domain names such as example.org, with an optional port.")
        if not domains:
            abort(400, "Enter at least one allowed embed domain.")
    else:
        domains = []
    password_hash = None
    if visibility == "password":
        if not password and not previous_hash:
            abort(400, "Enter a password for viewers.")
        if previous_hash and (not password or check_password_hash(previous_hash, password)):
            password_hash = previous_hash
        else:
            password_hash = generate_password_hash(password)
    return dict(visibility=visibility, embed=embed, domains=domains, download=download), password_hash


def get_privacy(con, uid):
    return con.execute("SELECT * FROM library_privacy WHERE work=?", (uid,)).fetchone()


def manages_sharing(con, uid):
    return con.execute(
        "SELECT 1 FROM library_privacy WHERE work=? UNION ALL "
        "SELECT 1 FROM library_privacy_change WHERE work=? UNION ALL "
        "SELECT 1 FROM library_peer_handoff WHERE work=? LIMIT 1", (uid, uid, uid),
    ).fetchone() is not None


def apply_pending(con):
    rows = con.execute(
        """SELECT p.*, a.status AS publication_status, c.status AS catalog_status
           FROM library_privacy_change p JOIN action a ON a.id=p.approval
           LEFT JOIN action c ON c.id=p.listing
           WHERE NOT EXISTS (SELECT 1 FROM library_peer_handoff h
               WHERE h.work=p.work AND h.state='pending')"""
    ).fetchall()
    for row in rows:
        if protected_route(con, row["work"]):
            cancel_pending(con, row)
            continue
        statuses = (row["publication_status"], row["catalog_status"])
        if statuses in (("eligible", None), ("eligible", "eligible")):
            save(con, row["work"], row["settings"], row["password_hash"], row["revision"], row["share_key"])
        elif "rejected" not in statuses:
            continue
        cancel_pending(con, row)


def save(con, uid, settings, password_hash, revision, share_key):
    con.execute(
        """INSERT INTO library_privacy VALUES(?,?,?,?,?) ON CONFLICT(work) DO UPDATE SET
           settings=excluded.settings,password_hash=excluded.password_hash,
           revision=excluded.revision,share_key=excluded.share_key""",
        (uid, settings, password_hash, revision, share_key),
    )


def cancel_pending(con, row):
    con.execute(
        "UPDATE action SET status='rejected',decision='superseded' WHERE id IN (?,?) AND status='waiting'",
        (row["approval"], row["listing"]),
    )
    con.execute("DELETE FROM library_privacy_change WHERE work=?", (row["work"],))


def share_paths(uid, row):
    settings = json.loads(row["settings"])
    mode = settings["visibility"]
    query = "?" + urlencode({"key": row["share_key"]}) if row["share_key"] else ""
    return {
        "watch": f"/watch/work/{uid}{query}" if mode in {"public", "password", "unlisted"} else None,
        "embed_url": f"/embed/work/{uid}{query}" if settings["embed"] != "disabled" else None,
    }


def allowed_ancestor(referrer, domains):
    try:
        parsed = urlsplit(referrer)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return False
        for value in domains:
            rule = urlsplit("//" + value)
            host = parsed.hostname.lower().rstrip(".")
            if host == rule.hostname or (
                domain_has_subdomains(rule.hostname) and host.endswith("." + rule.hostname)
            ):
                if rule.port is None or rule.port == parsed.port:
                    return True
    except ValueError:
        return False
    return False


def frame_policy(domains):
    sources = []
    for rule in domains:
        url = urlsplit("//" + rule)
        host = rule if url.port is not None else rule + ":*"
        sources.extend(("https://" + host, "http://" + host))
        if domain_has_subdomains(url.hostname):
            sources.extend(("https://*." + host, "http://*." + host))
    return " ".join(sources)


def domain_has_subdomains(host):
    return "." in host and not re.fullmatch(r"\d+(?:\.\d+){3}", host)


def blocked(con, uid):
    return hidden(con, "work", uid) or con.execute(
        "SELECT 1 FROM case_hold WHERE work=? AND active=1", (uid,)
    ).fetchone() is not None


def protected_route(con, uid):
    return con.execute(
        "SELECT 1 FROM route WHERE slug=? AND public_record=1", (uid,)
    ).fetchone() is not None


def editable(work):
    return work["provider"] == "cueporch-upload" and playback(work) is not None


def actor_defaults(con, actor):
    row = con.execute(
        "SELECT settings,password_hash FROM library_upload_defaults WHERE actor=?", (actor,)
    ).fetchone()
    return (json.loads(row["settings"]), row["password_hash"]) if row else (DEFAULTS, None)


def apply_settings(con, action, work, settings, password_hash):
    uid = work["id"]
    row = get_privacy(con, uid)
    pending = con.execute("SELECT * FROM library_privacy_change WHERE work=?", (uid,)).fetchone()
    previous = json.loads(row["settings"]) if row else DEFAULTS
    if pending:
        cancel_pending(con, pending)
    if settings == previous and password_hash == (row["password_hash"] if row else None):
        return
    revision = secrets.token_hex(16)
    share_key = None
    if settings["visibility"] == "unlisted":
        share_key = row["share_key"] if row and row["share_key"] else secrets.token_urlsafe(24)
    encoded = json.dumps(settings)
    if settings["visibility"] == "private":
        save(con, uid, encoded, password_hash, revision, share_key)
        return
    approval = f"library-share-{revision}"
    listing = approval + "-catalog" if settings["visibility"] == "public" else None
    payload = dict(work=uid, title=work["title"], visibility=settings["visibility"])
    action(con, approval, "publication", payload)
    if listing:
        action(con, listing, "catalog", payload)
    con.execute(
        "INSERT INTO library_privacy_change VALUES(?,?,?,?,?,?,?)",
        (uid, encoded, password_hash, revision, share_key, approval, listing),
    )


def register(app, tx, staff, action, read_works, root, title_for, operator, permits, details_for):
    serializer = URLSafeTimedSerializer(operator, salt="cueporch-library-view")
    embed_form = URLSafeTimedSerializer(operator, salt="cueporch-library-embed-unlock")

    def find_work(uid):
        return next((work for work in read_works() if work["id"] == uid), None)

    @app.route("/api/library/upload-defaults", methods=["GET", "PUT"])
    def upload_defaults():
        actor = staff()
        with tx() as con:
            settings, password = actor_defaults(con, actor)
            if request.method == "PUT":
                settings, password = requested_settings(request.get_json(), password)
                con.execute(
                    """INSERT INTO library_upload_defaults VALUES(?,?,?) ON CONFLICT(actor) DO UPDATE SET
                       settings=excluded.settings,password_hash=excluded.password_hash""",
                    (actor, json.dumps(settings), password),
                )
        return dict(**settings, has_password=bool(password))

    @app.put("/api/library/privacy")
    def bulk_privacy():
        staff()
        body = request.get_json()
        ids = body.get("works") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids or any(not isinstance(uid, str) for uid in ids):
            abort(400, "Select videos to update.")
        settings, password_hash = requested_settings(body)
        members = set(ids)
        with tx() as con:
            works = [work for work in read_works() if work["id"] in members]
            if len(works) != len(members) or any(hidden(con, "work", uid) for uid in members):
                abort(404, "A selected work no longer exists.")
            if any(not editable(work) for work in works) or any(protected_route(con, uid) for uid in members):
                abort(409, "Select library uploads that can use these privacy settings.")
            for work in works:
                apply_settings(con, action, work, settings, password_hash)
            apply_pending(con)
            waiting = {row[0] for row in con.execute("SELECT work FROM library_privacy_change")}
        return dict(updated=len(members - waiting), pending=len(members & waiting))

    @app.route("/api/library/work/<uid>/privacy", methods=["GET", "PUT"])
    def privacy(uid):
        staff()
        body = request.get_json() if request.method == "PUT" else None
        with tx() as con:
            work = find_work(uid)
            if not work or hidden(con, "work", uid):
                abort(404)
            if not editable(work):
                abort(409, "This control currently applies to videos uploaded directly to the library.")
            if protected_route(con, uid):
                abort(409, "This institution has designated the video as a public record.")
            row = get_privacy(con, uid)
            if request.method == "PUT":
                settings, password_hash = requested_settings(body, row["password_hash"] if row else None)
                apply_settings(con, action, work, settings, password_hash)
                apply_pending(con)
                row = get_privacy(con, uid)
            pending = con.execute("SELECT * FROM library_privacy_change WHERE work=?", (uid,)).fetchone()
            paths = share_paths(uid, row) if row else dict(watch=None, embed_url=None)
        return dict(
            **(json.loads(row["settings"]) if row else DEFAULTS),
            **paths,
            has_password=bool(row and row["password_hash"]),
            pending=json.loads(pending["settings"]) if pending else None,
        )

    def try_password(con, uid, value, password_hash):
        if not value or len(value) > 256:
            return False
        visitor = hmac.new(operator.encode(), (request.remote_addr or "").encode(), hashlib.sha256).hexdigest()
        now = time.time()
        con.execute("DELETE FROM library_privacy_attempt WHERE since<=?", (now - 60,))
        attempt = con.execute(
            "SELECT attempts FROM library_privacy_attempt WHERE work=? AND visitor=?", (uid, visitor)
        ).fetchone()
        if attempt and attempt["attempts"] >= 8:
            abort(429, "Wait a minute before trying again.")
        con.execute(
            """INSERT INTO library_privacy_attempt VALUES(?,?,?,1) ON CONFLICT(work,visitor)
               DO UPDATE SET attempts=attempts+1""",
            (uid, visitor, now),
        )
        return check_password_hash(password_hash, value)

    def watch(uid, embed, frame):
        with tx() as con:
            row = get_privacy(con, uid)
            handoff = library_peer_handoff.get(con, uid)
            if handoff and handoff["state"] == "pending":
                abort(404)
            if row is None or protected_route(con, uid):
                return None
            if blocked(con, uid):
                abort(404)
            settings = json.loads(row["settings"])
            mode = settings["visibility"]
            if mode == "private" or (mode == "embed_only" and not embed):
                abort(404)
            key = request.args.get("key", "")
            if mode == "unlisted" and not hmac.compare_digest(key.encode(), row["share_key"].encode()):
                abort(404)
            ancestors = "'none'"
            unlock_token = ""
            if embed:
                if not frame or settings["embed"] == "disabled":
                    abort(404)
                ancestors = "*"
                if settings["embed"] == "domains":
                    if request.method == "POST":
                        try:
                            prior_frame = embed_form.loads(request.form.get("frame", ""), max_age=1200)
                        except BadSignature:
                            abort(403, "Open this video's player again.")
                        if prior_frame != [uid, row["revision"]]:
                            abort(403, "Open this video's player again.")
                    elif not allowed_ancestor(request.referrer or "", settings["domains"]):
                        abort(403, "Embedding is disabled on this site.")
                    unlock_token = embed_form.dumps([uid, row["revision"]])
                    ancestors = frame_policy(settings["domains"])
            work = find_work(uid)
            video = playback(work) if work else None
            if video is None or not permits(work, settings):
                abort(404)
            locked = mode == "password" and not (
                request.method == "POST" and try_password(con, uid, request.form.get("password", ""), row["password_hash"])
            )
            token = None if locked else serializer.dumps([uid, row["revision"], video["sha256"]])
            title = "Protected video" if locked else title_for(con, work)
            details = details_for(con, work) if not locked and not embed else None
        response = make_response(
            render_template("library_watch.html", work=uid, title=title, embed=embed,
                            token=token, tracks=captions(work), download=settings["download"],
                            wrong_password=locked and request.method == "POST",
                            unlock_token=unlock_token, details=details),
            401 if locked and request.method == "POST" else 200,
        )
        response.headers["Content-Security-Policy"] = f"frame-ancestors {ancestors}"
        if mode != "public" or embed:
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.get("/shared-video/<token>")
    @app.get("/shared-video/<token>/caption/<int:track>")
    def shared_video(token, track=None):
        try:
            uid, revision, digest = serializer.loads(token, max_age=4 * 3600)
        except BadSignature:
            abort(404)
        with tx() as con:
            row = get_privacy(con, uid)
            if not row or row["revision"] != revision or blocked(con, uid):
                abort(404)
            work = find_work(uid)
            video = playback(work) if work else None
            settings = json.loads(row["settings"])
            if video is None or video["sha256"] != digest or not permits(work, settings):
                abort(404)
            download = request.args.get("download") == "1"
            if download and (track is not None or not settings["download"]):
                abort(403)
        if track is not None:
            tracks = captions(work)
            if not 0 <= track < len(tracks):
                abort(404)
            item, media_type = tracks[track], "text/vtt"
        else:
            item, media_type = video, "video/mp4"
        path = root / "objects" / item["sha256"]
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype=media_type, conditional=True,
                         as_attachment=download, download_name="video.mp4" if download else None)

    return watch
