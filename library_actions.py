"""Editing and personal queues for the institutional library."""

import hashlib
import json
import re
import secrets
import time
from pathlib import Path

from flask import abort, request, send_file

import library_details
import library_appearance
import library_audio
import library_languages
import library_caption_jobs
import library_interactivity
import library_ratings
import library_registration
import library_analytics
import library_editing
import library_file_links
import library_timeline
import library_thumbnail
import library_versions
import library_privacy
import library_share
import library_peer_handoff
import media_bandwidth
import review_links
import timeline_jobs
from catalog import copy_work
from library_trash import hidden

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_title(
 work TEXT PRIMARY KEY, title TEXT NOT NULL, changed REAL NOT NULL,
 synced INTEGER NOT NULL DEFAULT 0, attempted REAL NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS library_watch_later(
 actor TEXT NOT NULL, work TEXT NOT NULL, PRIMARY KEY(actor,work));
CREATE TABLE IF NOT EXISTS library_download(
 ticket TEXT PRIMARY KEY, work TEXT NOT NULL, object TEXT NOT NULL, expires REAL NOT NULL);
"""
VIDEO_MIME = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
}
PREVIEW_MIME = {
    "video": VIDEO_MIME,
    "image": {"png": "image/png"},
    "audio": {"mp3": "audio/mpeg"},
}


def title_for(con, work):
    row = con.execute(
        "SELECT title FROM library_title WHERE work=?", (work["id"],)
    ).fetchone()
    return row[0] if row else work["title"]


def save_title(con, work, name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128:
        abort(400, "Enter a title of 1 to 128 characters.")
    uid = work["id"]
    name = name.strip()
    if title_for(con, work) == name:
        return
    con.execute(
        """INSERT INTO library_title(work,title,changed) VALUES(?,?,?)
           ON CONFLICT(work) DO UPDATE SET title=excluded.title,
             changed=excluded.changed,synced=0,attempted=0,error=NULL""",
        (uid, name, time.time()),
    )
    con.execute(
        """UPDATE route SET description=? WHERE slug=? AND EXISTS
           (SELECT 1 FROM publication_job j WHERE j.id=route.current_action AND j.work=route.slug)""",
        (name, uid),
    )


def register(app, tx, staff, read_works, root, remember_work, permits, action, signing_key, authorize_editor):
    def get_work(con, uid):
        work = next((item for item in read_works() if item["id"] == uid), None)
        if not work or hidden(con, "work", uid):
            abort(404)
        return work

    library_thumbnail.register(app, tx, staff, get_work, root)
    library_appearance.register(app, tx, staff, get_work, read_works, title_for)
    library_details.register(app, tx, authorize_editor, root, get_work, title_for, save_title)
    start_file_links = library_file_links.register(app, tx, staff, root, get_work, permits)
    library_interactivity.register(app, tx, staff, get_work, title_for, root, remember_work, read_works, permits)
    library_ratings.register(app, tx, staff, get_work, read_works)
    library_registration.register(app, tx, staff, get_work, library_privacy.protected_route)
    library_analytics.register(app, tx, staff, get_work, title_for)
    library_editing.register(app, tx, staff, get_work, root, remember_work, title_for, VIDEO_MIME)
    library_timeline.register(app, tx, staff, get_work, root, signing_key)
    start_editor = timeline_jobs.register(app, tx, staff, get_work, root, remember_work)
    library_caption_jobs.register(app, tx, staff, get_work, action)
    library_languages.register(
        app, tx, staff, get_work, root, remember_work, title_for,
        lambda con, work: library_caption_jobs.queue(con, action, work, require_missing=True),
    )
    library_audio.register(app, tx, staff, get_work, root, remember_work, library_privacy.playback)
    library_share.register(
        app, tx, staff, get_work,
        lambda con, work: library_privacy.operator_sharing(con, work, permits),
    )
    replace_video = library_versions.register(app, tx, staff, get_work, root, remember_work)
    review_links.register(app, tx, staff, get_work, title_for, root, replace_video)

    @app.get("/api/library/work/<uid>")
    def work_info(uid):
        staff()
        with tx() as con:
            work = get_work(con, uid)
            route = con.execute(
                """SELECT kind FROM route WHERE slug=?
                   ORDER BY CASE kind WHEN 'work' THEN 0 WHEN 'meeting' THEN 1 ELSE 2 END, kind LIMIT 1""",
                (uid,),
            ).fetchone()
            if library_peer_handoff.get(con, uid):
                route = None
            title = title_for(con, work)
            details = library_details.details_for(con, work)
            thumbnail = library_thumbnail.selected_thumbnail(con, work)
            policy = library_privacy.get_privacy(con, uid)
            shared = None
            is_public_record = library_privacy.protected_route(con, uid)
            if policy and not is_public_record:
                shared, _ = library_privacy.operator_sharing(con, work, permits)
        files = [
            {
                k: f[k]
                for k in ("sha256", "role", "format", "bytes", "language", "name")
                if k in f
            }
            for f in work["files"]
        ]
        asset_type = work.get("asset_type", "video")
        accepted = PREVIEW_MIME.get(asset_type, {})
        display_files = (
            sorted(files, key=lambda item: item["role"] != "playback")
            if "asset_type" in work else files
        )
        return {
            "id": uid,
            "title": title,
            "source_title": work["title"],
            "files": files,
            "asset_type": asset_type,
            "source": work["source_url"],
            "duration_seconds": work.get("duration_seconds"),
            "creator": work.get("source_creator"),
            "description": details["description"],
            "source_privacy": work.get("source_privacy"),
            "vimeo_source": work["provider"] == "vimeo",
            "editable_privacy": not is_public_record and library_privacy.editable(work),
            "disposition": work["disposition"],
            "watch": shared["watch"] if shared else f"/watch/{route['kind']}/{uid}" if route else None,
            "embed": shared["embed_url"] if shared else f"/embed/{route['kind']}/{uid}" if route else None,
            "thumbnail": thumbnail is not None,
            "preview": next(
                (
                    f["sha256"]
                    for f in display_files
                    if f["role"] in ("original", "playback") and f["format"] in accepted
                    and (asset_type == "video" or f["role"] == "playback")
                ),
                None,
            ),
        }

    @app.put("/api/library/work/<uid>/title")
    def rename_work(uid):
        staff()
        body = request.get_json()
        name = body.get("title") if isinstance(body, dict) else None
        with tx() as con:
            work = get_work(con, uid)
            save_title(con, work, name)
        return "", 204

    @app.post("/api/library/work/<uid>/copy")
    def duplicate_work(uid):
        staff()
        body = request.get_json()
        name = body.get("title") if isinstance(body, dict) else None
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128:
            abort(400, "Enter a title of 1 to 128 characters.")
        with tx() as con:
            get_work(con, uid)
            result = copy_work(root, uid, name.strip())
        return {"id": result}, 201

    @app.put("/api/library/work/<uid>/watch-later")
    def set_watch_later(uid):
        actor = staff()
        body = request.get_json()
        if not isinstance(body, dict) or type(body.get("saved")) is not bool:
            abort(400)
        with tx() as con:
            get_work(con, uid)
            con.execute(
                "INSERT OR IGNORE INTO library_watch_later VALUES(?,?)"
                if body["saved"]
                else "DELETE FROM library_watch_later WHERE actor=? AND work=?",
                (actor, uid),
            )
        return "", 204

    @app.post("/api/library/work/<uid>/ticket")
    def download_ticket(uid):
        staff()
        body = request.get_json()
        digest = body.get("sha256") if isinstance(body, dict) else None
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            abort(400)
        before = time.time()
        token = secrets.token_urlsafe(32)
        with tx() as con:
            work = get_work(con, uid)
            if not any(
                f["sha256"] == digest and f["role"] in ("original", "playback")
                for f in work["files"]
            ):
                abort(404)
            con.execute("DELETE FROM library_download WHERE expires<=?", (before,))
            con.execute(
                "INSERT INTO library_download VALUES(?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), uid, digest, before + 600),
            )
        return {
            "download": f"/library-media/{token}",
            "preview": f"/library-media/{token}?preview=1",
        }

    @app.get("/library-media/<token>")
    def serve_media(token):
        with tx() as con:
            row = con.execute(
                "SELECT work,object FROM library_download WHERE ticket=? AND expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
            if not row:
                abort(404)
            work = get_work(con, row["work"])
            item = next(
                (
                    f
                    for f in work["files"]
                    if f["sha256"] == row["object"]
                    and f["role"] in ("original", "playback")
                ),
                None,
            )
            if not item:
                abort(404)
            name = title_for(con, work)
            asset_type = work.get("asset_type", "video")
        media = root / "objects" / row["object"]
        if not media.is_file():
            abort(404)
        fmt = item["format"]
        suffix = fmt if re.fullmatch("[a-zA-Z0-9]{1,10}", fmt) else "bin"
        title = re.sub('[<>:"/\\\\|?*\\x00-\\x1f]', "_", name).strip(" .") or "video"
        accepted = PREVIEW_MIME.get(asset_type, {})
        preview = (
            request.args.get("preview") == "1"
            and fmt in accepted
            and (asset_type == "video" or item["role"] == "playback")
        )
        filename = (
            item.get("name", title + "." + suffix)
            if work["provider"] == "cueporch-upload"
            else title + "." + suffix
        )
        response = send_file(
            media,
            as_attachment=not preview,
            download_name=filename,
            mimetype=accepted[fmt] if preview else "application/octet-stream",
            conditional=True,
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.cache_control.no_store = True
        response.cache_control.private = True
        return media_bandwidth.measure(response, tx, work["id"]) if asset_type == "video" else response

    return start_editor, start_file_links


def sync_edits(tx, receipts, origin, root, connect):
    """Synchronize editorial metadata while preserving the remote video identity."""
    with tx() as con:
        pending = con.execute(
            """WITH edits AS (
                 SELECT 'title' AS kind,work,title AS value,changed,synced,attempted FROM library_title
                 UNION ALL
                 SELECT 'thumbnail',work,object,changed,synced,attempted FROM library_thumbnail
               )
               SELECT * FROM edits t WHERE synced=0 AND attempted<=?
                 AND NOT EXISTS(SELECT 1 FROM library_deleted_item d WHERE d.kind='work' AND d.item=t.work)""",
            (time.time() - 15,),
        ).fetchall()
    for row in pending:
        is_title = row["kind"] == "title"
        table = "library_title" if is_title else "library_thumbnail"
        path = Path(receipts) / (row["work"] + ".json")
        if not path.is_file():
            continue
        try:
            state = json.loads(path.read_text())
            if not state.get("video"):
                continue
            if state["work"] != row["work"] or state["provider"] != origin.rstrip("/"):
                raise ValueError("Connect the original video host")
            peer = connect(origin)
            if state["account"] != peer.user["id"]:
                raise ValueError("Connect the original publishing account")
            peer.update_metadata(
                state["video"],
                {"name": row["value"][:120]} if is_title else {},
                None if is_title else root / "objects" / row["value"],
            )
        except (OSError, ValueError, KeyError) as exc:
            with tx() as con:
                con.execute(
                    f"UPDATE {table} SET attempted=?,error=? WHERE work=? AND changed=?",
                    (time.time(), type(exc).__name__, row["work"], row["changed"]),
                )
        else:
            with tx() as con:
                con.execute(
                    f"UPDATE {table} SET synced=1,error=NULL WHERE work=? AND changed=?",
                    (row["work"], row["changed"]),
                )
