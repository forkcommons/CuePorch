"""Share version-bound, revocable direct file and adaptive streaming URLs."""

import json
import re
import sqlite3
import uuid
from subprocess import TimeoutExpired
from threading import Event, Thread

from filelock import FileLock, Timeout as LockTimeout
from flask import abort, render_template, request, send_file, url_for
from werkzeug.exceptions import HTTPException

import file_link_media
import library_peer_handoff
import library_privacy
import library_registration
import media_bandwidth
from browser_account_security import private
from catalog import video_version_key


SCHEMA = """
CREATE TABLE IF NOT EXISTS video_file_link(
 id TEXT PRIMARY KEY, work TEXT NOT NULL UNIQUE, version TEXT NOT NULL,
 privacy TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL,
 renditions TEXT, error TEXT);
"""
FILES = re.compile(r"(?:[0-9]+p\.mp4|hls\.m3u8|dash\.mpd|media_[0-9]+\.m3u8|"
                   r"(?:init-stream[0-9]+|chunk-stream[0-9]+-[0-9]+)\.m4s)")
MIME = {".mp4": "video/mp4", ".m3u8": "application/vnd.apple.mpegurl",
        ".mpd": "application/dash+xml", ".m4s": "video/iso.segment"}
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range",
}


def active_policy(con, work, permits):
    uid = work["id"]
    row = library_privacy.get_privacy(con, uid)
    if row is None or library_privacy.blocked(con, uid) or library_privacy.protected_route(con, uid):
        return None, None
    settings = json.loads(row["settings"])
    handoff = library_peer_handoff.get(con, uid)
    if (settings["visibility"] != "public" or settings["embed"] != "anywhere"
            or library_registration.active(con, "video", uid) or not permits(work, settings)
            or (handoff and handoff["state"] == "pending")):
        return None, None
    return row["revision"], settings


def source_file(work):
    return next((item for item in work["files"] if item["role"] == "original"), None) or library_privacy.playback(work)


def state_for(work, row, privacy, settings):
    version = video_version_key(work)
    if not privacy or not source_file(work):
        return dict(version=version, state="unavailable",
                    id=row["id"] if row else None,
                    issue="Direct links require a retained video open to everyone and embeddable anywhere.")
    if row is None or (row["version"], row["privacy"]) != (version, privacy):
        return dict(version=version, state="available", id=row["id"] if row else None)
    state = dict(version=version, state=row["state"], id=row["id"], issue=row["error"])
    if row["state"] == "ready":
        def path(name, download=False):
            return url_for("direct_video_file", link=row["id"], filename=name, **({"download": "1"} if download else {}))

        renditions = json.loads(row["renditions"])
        state.update(hls=path("hls.m3u8"), dash=path("dash.mpd"), files=[
            dict(width=item["width"], height=item["height"], bytes=item["bytes"], play=path(item["name"]),
                 download=path(item["name"], True) if settings["download"] else None)
            for item in renditions
        ])
    return state


def register(app, tx, staff, root, get_work, permits):
    destination = root / "renditions"
    wake, stopping = Event(), Event()

    def cache(source):
        return destination / f"{file_link_media.RECIPE}-{source}"

    @app.get("/operator/videos/<uuid:work>/file-links")
    def video_file_link_page(work):
        return private(render_template("video_file_links.html", work=str(work)))

    @app.route("/api/library/work/<uuid:work>/file-links", methods=("GET", "POST", "DELETE"))
    def video_file_links(work):
        staff()
        uid = str(work)
        with tx() as con:
            item = get_work(con, uid)
            if item.get("asset_type", "video") != "video":
                abort(404)
            row = con.execute("SELECT * FROM video_file_link WHERE work=?", (uid,)).fetchone()
            privacy, settings = active_policy(con, item, permits)
            state = state_for(item, row, privacy, settings)
            if request.method in ("GET", "HEAD"):
                return private(state)
            body = request.get_json()
            if not isinstance(body, dict):
                abort(400)
            if request.method == "DELETE":
                if row and body.get("id") != row["id"]:
                    abort(409, "The direct link changed. Reload it before revoking.")
                con.execute("DELETE FROM video_file_link WHERE work=?", (uid,))
                return private("", 204)
            if state["state"] == "unavailable":
                abort(409, state["issue"])
            if body.get("version") != state["version"]:
                abort(409, "The current cut changed. Reload it before creating links.")
            if state["state"] in ("queued", "rendering", "ready"):
                return private(state, 200 if state["state"] == "ready" else 202)
            link = row["id"] if state["state"] == "failed" else str(uuid.uuid4())
            source = source_file(item)["sha256"]
            con.execute("""INSERT INTO video_file_link VALUES(?,?,?,?,?,'queued',NULL,NULL)
                ON CONFLICT(work) DO UPDATE SET id=excluded.id,version=excluded.version,privacy=excluded.privacy,
                    source=excluded.source,state='queued',renditions=NULL,error=NULL""",
                (link, uid, state["version"], privacy, source))
        wake.set()
        return private(dict(version=state["version"], state="queued", id=link), 202)

    @app.route("/file-links/<uuid:link>/<filename>", methods=("GET", "OPTIONS"))
    def direct_video_file(link, filename):
        if not FILES.fullmatch(filename):
            abort(404)
        with tx() as con:
            row = con.execute("SELECT * FROM video_file_link WHERE id=? AND state='ready'", (str(link),)).fetchone()
            if row is None:
                abort(404)
            work = get_work(con, row["work"])
            privacy, settings = active_policy(con, work, permits)
            if privacy != row["privacy"] or video_version_key(work) != row["version"]:
                abort(404)
            path = cache(row["source"]) / filename
            if not path.is_file():
                abort(404)
            download = request.args.get("download") == "1"
            if download and (not settings["download"] or path.suffix != ".mp4"):
                abort(404)
        if request.method == "OPTIONS":
            return "", 204, CORS
        response = send_file(path, mimetype=MIME[path.suffix], conditional=True, as_attachment=download,
                             download_name=filename)
        response.cache_control.no_store = True
        response.headers.update({
            **CORS,
            "X-Content-Type-Options": "nosniff", "X-Robots-Tag": "noindex, nofollow",
        })
        return media_bandwidth.measure(response, tx, row["work"])

    def render_next():
        with tx() as con:
            row = con.execute("SELECT * FROM video_file_link WHERE state IN ('queued','rendering') ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return False
            con.execute("UPDATE video_file_link SET state='rendering' WHERE id=?", (row["id"],))
        rendered, error = None, None
        try:
            with tx() as con:
                work = get_work(con, row["work"])
                privacy, _ = active_policy(con, work, permits)
                if privacy != row["privacy"] or video_version_key(work) != row["version"]:
                    raise ValueError("The video or its sharing rules changed. Reload before preparing links.")
            rendered = file_link_media.variants(root / "objects" / row["source"], cache(row["source"]), stopping)
        except InterruptedError:
            return False
        except HTTPException as issue:
            error = issue.description
        except ValueError as issue:
            error = str(issue)
        except (OSError, TimeoutExpired):
            error = "The media could not be read or rendition preparation timed out."
        with tx() as con:
            con.execute("UPDATE video_file_link SET state=?,renditions=?,error=? WHERE id=?",
                        ("failed" if error else "ready", json.dumps(rendered), error, row["id"]))
        return True

    def render_jobs():
        lock = FileLock(str(root / ".file-link-worker.lock"), timeout=0)
        while not stopping.is_set():
            try:
                with lock:
                    if render_next():
                        continue
            except LockTimeout:
                pass
            except sqlite3.OperationalError:
                app.logger.exception("Direct video links could not read their rendition queue")
            wake.wait(2)
            wake.clear()

    def start():
        worker = Thread(target=render_jobs, name="CuePorch file links", daemon=True)
        worker.start()

        def stop():
            stopping.set()
            wake.set()
            worker.join(timeout=6)

        return stop

    return start
