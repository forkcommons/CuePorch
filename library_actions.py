"""Editing and personal queues for the institutional library."""

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

from flask import abort, request, send_file

from catalog import copy_work
from library_trash import hidden
from peertube_destination import PeerTube

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


def title_for(con, work):
    row = con.execute(
        "SELECT title FROM library_title WHERE work=?", (work["id"],)
    ).fetchone()
    return row[0] if row else work["title"]


def register(app, tx, staff, read_works, root):
    def get_work(con, uid):
        work = next((item for item in read_works() if item["id"] == uid), None)
        if not work or hidden(con, "work", uid):
            abort(404)
        return work

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
            title = title_for(con, work)
        files = [
            {
                k: f[k]
                for k in ("sha256", "role", "format", "bytes", "language")
                if k in f
            }
            for f in work["files"]
        ]
        return {
            "id": uid,
            "title": title,
            "source_title": work["title"],
            "files": files,
            "source": work["source_url"],
            "duration_seconds": work.get("duration_seconds"),
            "creator": work.get("source_creator"),
            "description": work.get("description"),
            "source_privacy": work.get("source_privacy"),
            "disposition": work["disposition"],
            "watch": f"/watch/{route['kind']}/{uid}" if route else None,
            "embed": f"/embed/{route['kind']}/{uid}" if route else None,
            "preview": next(
                (
                    f["sha256"]
                    for f in files
                    if f["role"] in ("playback", "original")
                    and f["format"] in VIDEO_MIME
                ),
                None,
            ),
        }

    @app.put("/api/library/work/<uid>/title")
    def rename_work(uid):
        staff()
        body = request.get_json()
        name = body.get("title") if isinstance(body, dict) else None
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128:
            abort(400, "Enter a title of 1 to 128 characters.")
        name = name.strip()
        with tx() as con:
            work = get_work(con, uid)
            if title_for(con, work) != name:
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
        media = root / "objects" / row["object"]
        if not media.is_file():
            abort(404)
        fmt = item["format"]
        suffix = fmt if re.fullmatch("[a-zA-Z0-9]{1,10}", fmt) else "bin"
        title = re.sub('[<>:"/\\\\|?*\\x00-\\x1f]', "_", name).strip(" .") or "video"
        preview = request.args.get("preview") == "1" and fmt in VIDEO_MIME
        response = send_file(
            media,
            as_attachment=not preview,
            download_name=title + "." + suffix,
            mimetype=VIDEO_MIME.get(fmt, "application/octet-stream"),
            conditional=True,
        )
        response.cache_control.no_store = True
        response.cache_control.private = True
        return response


def sync_titles(tx, receipts, origin):
    """Update only the title of a managed video, preserving its remote identity."""
    with tx() as con:
        pending = con.execute(
            """SELECT * FROM library_title t WHERE synced=0 AND attempted<=?
               AND NOT EXISTS(SELECT 1 FROM library_deleted_item d WHERE d.kind='work' AND d.item=t.work)""",
            (time.time() - 15,),
        ).fetchall()
    peer = None
    for row in pending:
        path = Path(receipts) / (row["work"] + ".json")
        if not path.is_file():
            continue
        try:
            state = json.loads(path.read_text())
            if not state.get("video"):
                continue
            if state["work"] != row["work"] or state["provider"] != origin.rstrip("/"):
                raise ValueError("Connect the original video host")
            if peer is None:
                peer = PeerTube(
                    origin,
                    os.environ["PEERTUBE_USERNAME"],
                    os.environ["PEERTUBE_PASSWORD"],
                )
            if state["account"] != peer.user["id"]:
                raise ValueError("Connect the original publishing account")
            peer.call(
                "PUT",
                "/api/v1/videos/" + state["video"],
                json={"name": row["title"][:120]},
            )
        except (OSError, ValueError, KeyError) as exc:
            with tx() as con:
                con.execute(
                    "UPDATE library_title SET attempted=?,error=? WHERE work=? AND changed=?",
                    (time.time(), type(exc).__name__, row["work"], row["changed"]),
                )
        else:
            with tx() as con:
                con.execute(
                    "UPDATE library_title SET synced=1,error=NULL WHERE work=? AND changed=?",
                    (row["work"], row["changed"]),
                )
