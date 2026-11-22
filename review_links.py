"""Expiring account-free review links for an institution's retained video versions."""

import hashlib
import math
import os
import re
import secrets
import time

from flask import Response, abort, redirect, render_template, request, send_file, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import folder_reviews
import review_feedback
from library_trash import hidden
from library_languages import tracks_for
import media_bandwidth
import review_notifications
import review_push
import review_settings
from catalog import video_version_key
from media_tools import review_frame_times, video_properties

SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS review_link(
 id TEXT PRIMARY KEY, work TEXT NOT NULL, name TEXT NOT NULL, expires REAL NOT NULL,
 password_hash TEXT, enabled INTEGER NOT NULL DEFAULT 1,
 allow_download INTEGER NOT NULL DEFAULT 0, allow_versions INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_session(
 digest TEXT PRIMARY KEY, link TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_preview(
 digest TEXT PRIMARY KEY, link TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_unlock_limit(
 link TEXT PRIMARY KEY, since REAL NOT NULL, attempts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS vimeo_review_replacement (work TEXT NOT NULL, source TEXT NOT NULL, link TEXT NOT NULL, PRIMARY KEY (work, source));
"""
    + review_feedback.SCHEMA
    + review_notifications.SCHEMA
    + review_push.SCHEMA
    + review_settings.SCHEMA
    + folder_reviews.SCHEMA
)

REVIEW_LIFETIME = 30 * 86400
SESSION_LIFETIME = 2 * 3600


def session_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def register(app, tx, staff, get_work, title_for, root, replace_video, read_works):
    def locate(con, work, uid):
        row = con.execute(
            """SELECT * FROM review_link WHERE id=? AND work=?
               AND NOT EXISTS (SELECT 1 FROM review_folder_item WHERE link=review_link.id)""",
            (uid, work),
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

    def new_review_link(con, uid, work, body):
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

    def visible_folder(con, folder):
        if hidden(con, "folder", folder) or con.execute(
            "SELECT 1 FROM library_folder WHERE id=?", (folder,)
        ).fetchone() is None:
            abort(404)

    @app.get("/api/library/folders/<folder>/review-links")
    def list_folder_reviews(folder):
        staff()
        with tx() as con:
            visible_folder(con, folder)
            return [
                project(con, row)
                for row in con.execute(
                    "SELECT rl.* FROM review_link rl JOIN review_folder rf "
                    "ON rf.id=rl.id WHERE rf.folder=? ORDER BY rl.created DESC",
                    (folder,),
                )
            ]

    @app.post("/api/library/folders/<folder>/review-links")
    def create_folder_review(folder):
        staff()
        request.max_content_length = 16384
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        uid = secrets.token_urlsafe(32)
        with tx() as con:
            visible_folder(con, folder)
            new_review_link(con, uid, "", body)
            con.execute("INSERT INTO review_folder VALUES(?,?)", (uid, folder))
            return project(con, locate(con, "", uid)), 201

    @app.put("/api/library/folders/<folder>/review-links/<uid>")
    def toggle_folder_review(folder, uid):
        staff()
        request.max_content_length = 16384
        body = request.get_json()
        if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
            abort(400)
        with tx() as con:
            visible_folder(con, folder)
            row = folder_reviews.folder_for(con, uid)
            if row is None or row["folder"] != folder:
                abort(404)
            con.execute("UPDATE review_link SET enabled=? WHERE id=?",
                        (int(body["enabled"]), uid))
            if not body["enabled"]:
                con.execute("DELETE FROM review_session WHERE link=?", (uid,))
            return project(con, locate(con, "", uid))

    @app.get("/api/library/work/<work>/review-links")
    def list_reviews(work):
        staff()
        with tx() as con:
            get_work(con, work)
            return [
                project(con, row)
                for row in con.execute(
                    """SELECT * FROM review_link WHERE work=?
                       AND NOT EXISTS (SELECT 1 FROM review_folder_item WHERE link=review_link.id)
                       ORDER BY created DESC""",
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
            new_review_link(con, uid, work, body)
            return project(con, locate(con, work, uid)), 201

    @app.get("/api/library/work/<work>/vimeo-review-links")
    def list_vimeo_review_links(work):
        staff()
        with tx() as con:
            inventory = get_work(con, work).get("source_review", {
                "state": "unavailable", "status": None, "links": [],
            })
            replacements = {
                source: "/review/" + uid if uid else None
                for source, uid in con.execute(
                    """SELECT v.source, r.id FROM vimeo_review_replacement v
                       LEFT JOIN review_link r ON r.id=v.link AND r.work=v.work
                       WHERE v.work=?""",
                    (work,),
                )
            }
            return inventory | {
                "links": [
                    row | {"replacement": replacements[row["id"]]}
                    if row["id"] in replacements else row
                    for row in inventory["links"]
                ],
            }

    @app.post("/api/library/work/<work>/vimeo-review-links/<source>")
    def create_vimeo_review_link(work, source):
        staff()
        request.max_content_length = 16384
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        with tx() as con:
            inventory = get_work(con, work).get("source_review", {})
            previous = con.execute(
                "SELECT link FROM vimeo_review_replacement WHERE work=? AND source=?",
                (work, source),
            ).fetchone()
            if previous:
                return project(con, locate(con, work, previous["link"]))

            preset = next((row for row in inventory.get("links", []) if row["id"] == source), None)
            if preset is None:
                abort(404)
            if not preset["enabled"] or preset["expires"] is None or preset["expires"] <= time.time():
                abort(400, "This Vimeo link needs a new review link with a future expiration.")
            password = body.get("password")
            if preset["needs_password"] and (not isinstance(password, str) or not password):
                abort(400, "Set a new password for this replacement review link.")
            choices = {
                key: preset[key] for key in (
                    "name", "expires", "allow_download", "allow_versions", "allow_comment",
                    "allow_status", "allow_resolved", "dark", "show_other_videos",
                )
            } | {"password": password, "logo": None}
            uid = secrets.token_urlsafe(32)
            new_review_link(con, uid, work, choices)
            con.execute("INSERT INTO vimeo_review_replacement VALUES(?,?,?)", (work, source, uid))
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
                con.execute("DELETE FROM review_preview WHERE link=?", (uid,))
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
            con.execute("DELETE FROM review_preview WHERE link=?", (uid,))
            con.execute("DELETE FROM review_unlock_limit WHERE link=?", (uid,))
            con.execute("DELETE FROM review_feedback_permission WHERE link=?", (uid,))
            con.execute("DELETE FROM review_appearance WHERE link=?", (uid,))
            con.execute(
                "DELETE FROM review_reaction WHERE comment IN (SELECT id FROM review_comment WHERE link=?)",
                (uid,),
            )
            review_notifications.forget(con, uid)
            review_push.forget(con, uid)
            con.execute("DELETE FROM review_link WHERE id=?", (uid,))
        return "", 204

    def active_link(con, uid):
        now = time.time()
        row = con.execute(
            "SELECT * FROM review_link WHERE id=? AND enabled=1 AND expires>?",
            (uid, now),
        ).fetchone()
        if not row or not folder_reviews.scope_active(con, uid, now):
            abort(404)
        return row

    def active_folder_review(con, uid):
        parent = folder_reviews.folder_for(con, uid)
        if parent is None:
            abort(404)
        visible_folder(con, parent["folder"])
        return active_link(con, uid), parent["folder"]

    def active(con, uid):
        row = active_link(con, uid)
        if con.execute(
            "SELECT 1 FROM case_hold WHERE work=? AND active=1", (row["work"],)
        ).fetchone():
            abort(404)
        return row, get_work(con, row["work"])

    def preview_session(con, uid):
        token = request.cookies.get("cueporch_review_preview", "")
        return bool(
            token
            and con.execute(
                "SELECT 1 FROM review_preview WHERE digest=? AND link=? AND expires>?",
                (session_digest(token), uid, time.time()),
            ).fetchone()
        )

    def preview_form_token():
        token = request.cookies.get("cueporch_review_preview", "")
        return session_digest("review replacement:" + token)

    def folder_cookie(uid):
        return "cueporch_folder_review_" + uid

    def folder_session(con, uid):
        token = request.cookies.get(folder_cookie(uid), "")
        return bool(
            token
            and con.execute(
                "SELECT 1 FROM review_session WHERE digest=? AND link=? AND expires>?",
                (session_digest(token), uid, time.time()),
            ).fetchone()
        )

    def create_review_session(con, row, uid):
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = min(row["expires"], now + SESSION_LIFETIME)
        con.execute("DELETE FROM review_session WHERE expires<=?", (now,))
        con.execute(
            "INSERT INTO review_session VALUES (?,?,?)",
            (session_digest(token), uid, expires),
        )
        return token, expires - now

    def authorized(con, row):
        parent = folder_reviews.parent_for(con, row["id"])
        if parent is not None:
            return folder_session(con, parent)
        if preview_session(con, row["id"]):
            return True
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

    def set_review_cookie(response, name, token, uid, seconds, *, path=None):
        response.set_cookie(
            name,
            token,
            max_age=int(seconds),
            secure=request.is_secure
            or os.environ.get("CUEPORCH_PUBLIC_URL", "").startswith("https://"),
            httponly=True,
            samesite="Lax",
            path=path or "/review/" + uid,
        )
        return response

    @app.post("/api/library/work/<work>/review-links/<uid>/preview")
    def prepare_review_preview(work, uid):
        staff()
        now = time.time()
        token = secrets.token_urlsafe(32)
        with tx() as con:
            locate(con, work, uid)
            row, _ = active(con, uid)
            expires = min(row["expires"], now + 600)
            con.execute("DELETE FROM review_preview WHERE expires<=?", (now,))
            con.execute(
                "INSERT INTO review_preview VALUES(?,?,?)",
                (session_digest(token), uid, expires),
            )
        return set_review_cookie(
            confidential({"path": "/review/" + uid}),
            "cueporch_review_preview",
            token,
            uid,
            expires - now,
        )

    @app.post("/review/<uid>/versions")
    def replace_review_video(uid):
        submitted = request.headers.get("X-CuePorch-Preview", "")

        def verify(con):
            row, _ = active(con, uid)
            if not preview_session(con, uid) or not secrets.compare_digest(
                submitted.encode(), preview_form_token().encode()
            ):
                abort(403)
            return row["work"]

        with tx() as con:
            work = verify(con)
        return confidential(replace_video(work, verify))

    def playable_versions(work, row):
        current = [work]
        if row["allow_versions"]:
            current.extend(reversed(work.get("versions_history", [])))
        playable = {}
        for version in current:
            media = next(iter(video_choices(version).values()), None)
            if media is not None:
                playable.setdefault(video_version_key(version), (version, media))
        return playable

    def video_choices(cut):
        choices = {}
        seen = set()
        for role in ("playback", "original"):
            file = next(
                (f for f in cut["files"] if f["role"] == role and f["format"] == "mp4"),
                None,
            )
            if file is not None and file["sha256"] not in seen:
                seen.add(file["sha256"])
                choices[role] = file
        return choices

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
        app,
        tx,
        scoped_feedback_access,
        operator_feedback_access,
        confidential,
        session_digest,
    )
    review_settings.register(app, tx, staff)

    @app.get("/review/notifications-worker.js")
    def review_notification_worker():
        if not review_push.contact():
            abort(404)
        return confidential(app.send_static_file("ui/review_notification_worker.js"))

    def folder_landing(con, uid, row, folder):
        opened = folder_session(con, uid)
        if row["password_hash"] and not opened:
            return confidential(render_template("review.html", locked=True, uid=uid))
        allowed = folder_reviews.members(con, folder)
        works = [
            {"id": work["id"], "title": title_for(con, work)}
            for work in read_works()
            if work["id"] in allowed and playable_versions(work, row)
        ]
        works.sort(key=lambda work: (work["title"].casefold(), work["id"]))
        page = confidential(
            render_template("folder_review.html", title=row["name"], uid=uid, works=works)
        )
        if not opened:
            token, seconds = create_review_session(con, row, uid)
            set_review_cookie(page, folder_cookie(uid), token, uid, seconds, path="/review")
        return page

    @app.get('/review/<uid>/item/<work>')
    def folder_review_item(uid, work):
        with tx() as con:
            row, folder = active_folder_review(con, uid)
            if not folder_session(con, uid):
                abort(403)
            if work not in folder_reviews.members(con, folder, work):
                abort(404)
            if not playable_versions(get_work(con, work), row):
                abort(404)

            link_row = con.execute(
                "SELECT link FROM review_folder_item WHERE parent=? AND work=?",
                (uid, work)
            ).fetchone()

            if link_row:
                child = link_row['link']
            else:
                child = secrets.token_urlsafe(32)
                review_settings.insert_link(
                    con,
                    child,
                    work,
                    row['name'],
                    row['expires'],
                    None,
                    project(con, row) | {'show_other_videos': False}
                )
                con.execute(
                    "INSERT INTO review_folder_item (parent, work, link) VALUES (?, ?, ?)",
                    (uid, work, child)
                )

            return confidential(redirect(url_for('review_page', uid=child), code=303))

    @app.get("/review/<uid>")
    def review_page(uid):
        with tx() as con:
            if folder_reviews.folder_for(con, uid):
                folder_review, folder = active_folder_review(con, uid)
                return folder_landing(con, uid, folder_review, folder)
            row, work = active(con, uid)
            parent = folder_reviews.parent_for(con, uid)
            if not authorized(con, row):
                if parent:
                    abort(403)
                return confidential(
                    render_template(
                        "review.html",
                        locked=True,
                        uid=uid,
                        unlock_version=request.args.get("version", ""),
                    )
                )
            versions = playable_versions(work, row)
            title = title_for(con, work)
            is_preview = preview_session(con, uid)
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
        renditions = []
        if selected:
            version, _ = versions[selected]
            tracks = tracks_for(work, version, selected)
            for role, file in video_choices(version).items():
                properties = video_properties(root / "objects" / file["sha256"])
                renditions.append(
                    {
                        "role": role,
                        "label": "Original" if role == "original" else "Screening copy",
                        **properties,
                    }
                )
        page = confidential(
            render_template(
                "review.html",
                uid=uid,
                title=title,
                folder_parent=parent,
                selected=selected,
                versions=versions,
                allow_download=row["allow_download"],
                tracks=tracks,
                appearance=appearance,
                recommendations=recommendations,
                operator_preview=is_preview,
                replacement_token=preview_form_token() if is_preview else None,
                current_version=video_version_key(work),
                renditions=renditions,
            )
        )
        if selected and not review_feedback.reviewer_token():
            set_review_cookie(
                page,
                review_feedback.REVIEWER_COOKIE,
                secrets.token_urlsafe(32),
                uid,
                min(REVIEW_LIFETIME, row["expires"] - time.time()),
            )
        return page

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
            rendition = request.args.get("rendition")
            if rendition and track is None:
                video = video_choices(cut).get(rendition)
                if video is None:
                    abort(404)
        if track is not None:
            captions = tracks_for(work, cut, version)
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
        response = confidential(
            send_file(
                path,
                mimetype=media_type,
                conditional=True,
                as_attachment=download,
                download_name="review-video.mp4" if download else None,
            )
        )
        return media_bandwidth.measure(response, tx, work["id"]) if track is None else response

    @app.get("/review/<uid>/frames/<version>")
    def review_frames(uid, version):
        with tx() as con:
            row, work = active(con, uid)
            if not authorized(con, row):
                abort(403)
            selected = playable_versions(work, row).get(version)
            if selected is None:
                abort(404)
            cut, video = selected
            rendition = request.args.get("rendition")
            if rendition:
                video = video_choices(cut).get(rendition)
                if video is None:
                    abort(404)
        address = request.args.get("at", "")
        if not re.fullmatch(r"[0-9]{1,6}", address):
            abort(400)
        bucket = int(address)
        if bucket % 10 or bucket > 7 * 86400:
            abort(400)
        path = root / "objects" / video["sha256"]
        if not path.is_file():
            abort(404)
        try:
            frames = review_frame_times(path, bucket)
        except ValueError:
            return confidential(({"error": "Frame timestamps are unavailable."}, 503))
        return confidential({"frames": frames})

    @app.post("/review/<uid>/unlock")
    def unlock(uid):
        request.max_content_length = 8192
        password = request.form.get("password", "")
        version = request.form.get("version", "")
        fragment = request.form.get("comment", "")
        if len(password) > 256:
            abort(400)
        now = time.time()
        with tx() as con:
            if folder_reviews.parent_for(con, uid):
                abort(403)
            folder_scope = folder_reviews.folder_for(con, uid) is not None
            row, _ = active_folder_review(con, uid) if folder_scope else active(con, uid)
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
                (
                    render_template(
                        "review.html",
                        locked=True,
                        uid=uid,
                        error=True,
                        unlock_version=version,
                        unlock_comment=fragment,
                    ),
                    403,
                )
            )
        with tx() as con:
            row, _ = active_folder_review(con, uid) if folder_scope else active(con, uid)
            if row["password_hash"] != password_hash:
                abort(409, "The review password changed. Open the review again.")
            token, seconds = create_review_session(con, row, uid)
            con.execute("DELETE FROM review_unlock_limit WHERE link=?", (uid,))
        query = {"version": version} if re.fullmatch(r"[a-f0-9]{64}", version) else {}
        path = url_for("review_page", uid=uid, **query)
        if re.fullmatch(r"comment-[a-f0-9]{32}", fragment):
            path += "#" + fragment
        return set_review_cookie(
            confidential(("", 303, {"Location": path})),
            folder_cookie(uid) if folder_scope else "cueporch_review",
            token,
            uid,
            seconds,
            path="/review" if folder_scope else None,
        )
