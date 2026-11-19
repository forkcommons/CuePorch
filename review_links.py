"""Expiring account-free review links for an institution's retained video versions."""

import hashlib
import math
import os
import secrets
import time

from flask import Response, abort, render_template, request, send_file
from werkzeug.security import check_password_hash, generate_password_hash

import review_feedback
import review_settings
from catalog import video_version_key

SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS review_link(
 id TEXT PRIMARY KEY, work TEXT NOT NULL, name TEXT NOT NULL, expires REAL NOT NULL,
 password_hash TEXT, enabled INTEGER NOT NULL DEFAULT 1,
 allow_download INTEGER NOT NULL DEFAULT 0, allow_versions INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_session(
 digest TEXT PRIMARY KEY, link TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_unlock_limit(
 link TEXT PRIMARY KEY, since REAL NOT NULL, attempts INTEGER NOT NULL);
"""
    + review_feedback.SCHEMA
    + review_settings.SCHEMA
)

REVIEW_LIFETIME = 30 * 86400
SESSION_LIFETIME = 2 * 3600


def session_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def register(app, tx, staff, get_work, title_for, root):
    def locate(con, work, uid):
        row = con.execute(
            "SELECT * FROM review_link WHERE id=? AND work=?", (uid, work)
        ).fetchone()
        if row is None:
            abort(404)
        return row

    def project(con, row):
        return (
            {
                name: row[name]
                for name in (
                    "id",
                    "name",
                    "expires",
                    "enabled",
                    "allow_download",
                    "allow_versions",
                    "created",
                )
            }
            | {
                "password": bool(row["password_hash"]),
                "path": "/review/" + row["id"],
            }
            | review_feedback.settings(con, row["id"])
            | review_settings.appearance(con, row["id"])
        )

    def link_fields(body):
        if not isinstance(body, dict):
            abort(400)
        name = body.get("name")
        password = body.get("password")
        expires = body.get("expires", time.time() + REVIEW_LIFETIME)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128:
            abort(400, "Give the review link a name.")
        if (
            type(expires) not in (float, int)
            or not time.time() < expires < 253402300799
            or not math.isfinite(expires)
        ):
            abort(400, "Choose a future expiration date.")
        if password is not None and (
            not isinstance(password, str) or len(password) > 256
        ):
            abort(400, "Enter a password of up to 256 characters.")
        for name_of_setting in ("allow_download", "allow_versions"):
            if type(body.get(name_of_setting, False)) is not bool:
                abort(400, "Use a valid review permission.")
        return name.strip(), expires, password

    @app.get("/api/library/work/<work>/review-links")
    def list_reviews(work):
        staff()
        with tx() as con:
            get_work(con, work)
            return [
                project(con, row)
                for row in con.execute(
                    "SELECT * FROM review_link WHERE work=? ORDER BY created DESC",
                    (work,),
                )
            ]

    @app.post("/api/library/work/<work>/review-links")
    def create_review(work):
        staff()
        request.max_content_length = 16384
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        uid = secrets.token_urlsafe(32)
        with tx() as con:
            get_work(con, work)
            defaults, default_hash = review_settings.read_defaults(con)
            choices = (
                defaults
                | {"expires": time.time() + defaults["lifetime_seconds"]}
                | body
            )
            name, expires, password = link_fields(choices)
            password_hash = (
                (generate_password_hash(password) if password else None)
                if "password" in body
                else default_hash
            )
            review_settings.insert_link(
                con, uid, work, name, expires, password_hash, choices
            )
            return project(con, locate(con, work, uid)), 201

    @app.put("/api/library/work/<work>/review-links/<uid>")
    def manage_review(work, uid):
        staff()
        request.max_content_length = 16384
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        with tx() as con:
            get_work(con, work)
            row = locate(con, work, uid)
            enabled = body.get("enabled", bool(row["enabled"]))
            if type(enabled) is not bool:
                abort(400)
            if "name" in body:
                name, expires, password = link_fields(body)
                review_feedback.save_settings(
                    con, uid, review_feedback.settings(con, uid) | body
                )
                review_settings.write_appearance(con, uid, body)
                password_hash = (
                    (generate_password_hash(password) if password else None)
                    if "password" in body
                    else row["password_hash"]
                )
                con.execute(
                    """UPDATE review_link SET name=?,expires=?,password_hash=?,
                       allow_download=?,allow_versions=?,enabled=? WHERE id=?""",
                    (
                        name,
                        expires,
                        password_hash,
                        body.get("allow_download", False),
                        body.get("allow_versions", False),
                        enabled,
                        uid,
                    ),
                )
            else:
                con.execute(
                    "UPDATE review_link SET enabled=? WHERE id=?", (enabled, uid)
                )
            if not enabled or "password" in body:
                con.execute("DELETE FROM review_session WHERE link=?", (uid,))
            return project(con, locate(con, work, uid))

    @app.post("/api/library/work/<work>/review-links/<uid>/copy")
    def copy_review(work, uid):
        staff()
        new_id = secrets.token_urlsafe(32)
        now = time.time()
        with tx() as con:
            get_work(con, work)
            locate(con, work, uid)
            con.execute(
                """INSERT INTO review_link
                   (id,work,name,expires,password_hash,allow_download,allow_versions,created)
                   SELECT ?,work,substr(name,1,121)||' (copy)',?,password_hash,
                          allow_download,allow_versions,?
                   FROM review_link WHERE id=?""",
                (new_id, now + REVIEW_LIFETIME, now, uid),
            )
            review_feedback.save_settings(
                con, new_id, review_feedback.settings(con, uid)
            )
            review_settings.write_appearance(
                con, new_id, review_settings.appearance(con, uid)
            )
            return project(con, locate(con, work, new_id)), 201

    @app.delete("/api/library/work/<work>/review-links/<uid>")
    def remove_review(work, uid):
        staff()
        with tx() as con:
            get_work(con, work)
            locate(con, work, uid)
            con.execute("DELETE FROM review_session WHERE link=?", (uid,))
            con.execute("DELETE FROM review_unlock_limit WHERE link=?", (uid,))
            con.execute("DELETE FROM review_feedback_permission WHERE link=?", (uid,))
            con.execute("DELETE FROM review_appearance WHERE link=?", (uid,))
            con.execute("DELETE FROM review_link WHERE id=?", (uid,))
        return "", 204

    def active(con, uid):
        row = con.execute(
            "SELECT * FROM review_link WHERE id=? AND enabled=1 AND expires>?",
            (uid, time.time()),
        ).fetchone()
        if not row:
            abort(404)
        if con.execute(
            "SELECT 1 FROM case_hold WHERE work=? AND active=1", (row["work"],)
        ).fetchone():
            abort(404)
        return row, get_work(con, row["work"])

    def authorized(con, row):
        if not row["password_hash"]:
            return True
        encrypted = request.cookies.get("cueporch_review", "")
        return bool(
            encrypted
            and con.execute(
                "SELECT 1 FROM review_session WHERE digest=? AND link=? AND expires>?",
                (session_digest(encrypted), row["id"], time.time()),
            ).fetchone()
        )

    def confidential(response):
        page = app.make_response(response)
        page.headers["Referrer-Policy"] = "no-referrer"
        page.headers["X-Content-Type-Options"] = "nosniff"
        page.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        page.cache_control.no_store = True
        page.cache_control.private = True
        return page

    def playable_versions(work, row):
        current = [work]
        if row["allow_versions"]:
            current.extend(reversed(work.get("versions_history", [])))
        playable = {}
        for version in current:
            files = version["files"]
            media = next(
                (
                    f
                    for role in ("playback", "original")
                    for f in files
                    if f["role"] == role and f["format"] == "mp4"
                ),
                None,
            )
            if media is not None:
                playable.setdefault(video_version_key(version), (version, media))
        return playable

    def operator_feedback_access(con, uid, version):
        staff()
        row = con.execute("SELECT work FROM review_link WHERE id=?", (uid,)).fetchone()
        if row is None:
            abort(404)
        work = get_work(con, row["work"])
        match = playable_versions(work, {"allow_versions": True}).get(version)
        if not match:
            abort(404)
        return match[0]

    def scoped_feedback_access(con, uid, version):
        if request.headers.get("Authorization"):
            return operator_feedback_access(con, uid, version), dict.fromkeys(
                review_feedback.OPTIONS, True
            )
        row, work = active(con, uid)
        if not authorized(con, row):
            abort(403)
        match = playable_versions(work, row).get(version)
        if not match:
            abort(404)
        return match[0], review_feedback.settings(con, uid)

    review_feedback.register(
        app, tx, scoped_feedback_access, operator_feedback_access, confidential
    )
    review_settings.register(app, tx, staff)

    @app.get("/review/<uid>")
    def review_page(uid):
        with tx() as con:
            row, work = active(con, uid)
            if not authorized(con, row):
                return confidential(
                    render_template("review.html", locked=True, uid=uid)
                )
            versions = playable_versions(work, row)
            title = title_for(con, work)
            appearance = review_settings.appearance(con, uid)
            recommendations = (
                con.execute(
                    """SELECT kind,slug,description FROM route r WHERE kind='work' AND slug<>?
                   AND NOT EXISTS (SELECT 1 FROM library_deleted_item d WHERE d.kind='work' AND d.item=r.slug)
                   ORDER BY description,slug LIMIT 3""",
                    (row["work"],),
                ).fetchall()
                if appearance["show_other_videos"]
                else []
            )
        selected = request.args.get("version") or next(iter(versions), None)
        if selected is not None and selected not in versions:
            abort(404)
        tracks = []
        if selected:
            version, media = versions[selected]
            tracks = [
                f
                for f in version["files"]
                if f["role"] == "caption" and f["format"] == "vtt"
            ]
        return confidential(
            render_template(
                "review.html",
                uid=uid,
                title=title,
                selected=selected,
                versions=versions,
                allow_download=row["allow_download"],
                tracks=tracks,
                appearance=appearance,
                recommendations=recommendations,
            )
        )

    @app.get("/review/<uid>/logo")
    def review_logo(uid):
        with tx() as con:
            row, _ = active(con, uid)
            if not authorized(con, row):
                abort(403)
            digest = review_settings.appearance(con, uid)["logo"]
            logo = con.execute(
                "SELECT mime,content FROM review_logo WHERE digest=?", (digest,)
            ).fetchone()
            if logo is None:
                abort(404)
        return confidential(Response(logo["content"], mimetype=logo["mime"]))

    @app.get("/review/<uid>/media/<version>")
    @app.get("/review/<uid>/caption/<version>/<int:track>")
    def review_media(uid, version, track=None):
        with tx() as con:
            row, work = active(con, uid)
            if not authorized(con, row):
                abort(403)
            selected = playable_versions(work, row).get(version)
            if selected is None:
                abort(404)
            cut, video = selected
            download = request.args.get("download") == "1"
            if download and (not row["allow_download"] or track is not None):
                abort(403)
        if track is not None:
            captions = [
                f
                for f in cut["files"]
                if f["role"] == "caption" and f["format"] == "vtt"
            ]
            if track >= len(captions):
                abort(404)
            item = captions[track]
            media_type = "text/vtt"
        else:
            item = video
            media_type = "video/mp4"
        path = root / "objects" / item["sha256"]
        if not path.is_file():
            abort(404)
        return confidential(
            send_file(
                path,
                mimetype=media_type,
                conditional=True,
                as_attachment=download,
                download_name="review-video.mp4" if download else None,
            )
        )

    @app.post("/review/<uid>/unlock")
    def unlock(uid):
        request.max_content_length = 8192
        password = request.form.get("password", "")
        if len(password) > 256:
            abort(400)
        now = time.time()
        with tx() as con:
            row, _ = active(con, uid)
            password_hash = row["password_hash"]
            if not password_hash:
                abort(400)
            recent = con.execute(
                "SELECT * FROM review_unlock_limit WHERE link=? AND since>?",
                (uid, now - 60),
            ).fetchone()
            if recent and recent["attempts"] >= 8:
                abort(429, "Wait a minute before trying again.")
            con.execute(
                "INSERT OR REPLACE INTO review_unlock_limit VALUES(?,?,?)",
                (
                    uid,
                    recent["since"] if recent else now,
                    recent["attempts"] + 1 if recent else 1,
                ),
            )
        if not check_password_hash(password_hash, password):
            return confidential(
                (render_template("review.html", locked=True, uid=uid, error=True), 403)
            )
        token = secrets.token_urlsafe(32)
        with tx() as con:
            row, _ = active(con, uid)
            if row["password_hash"] != password_hash:
                abort(409, "The review password changed. Open the review again.")
            expires = min(row["expires"], now + SESSION_LIFETIME)
            con.execute("DELETE FROM review_session WHERE expires<=?", (now,))
            con.execute(
                "INSERT INTO review_session VALUES (?,?,?)",
                (session_digest(token), uid, expires),
            )
            con.execute("DELETE FROM review_unlock_limit WHERE link=?", (uid,))
        response = confidential(("", 303, {"Location": "/review/" + uid}))
        response.set_cookie(
            "cueporch_review",
            token,
            max_age=int(expires - now),
            secure=request.is_secure
            or os.environ.get("CUEPORCH_PUBLIC_URL", "").startswith("https://"),
            httponly=True,
            samesite="Lax",
            path="/review/" + uid,
        )
        return response
