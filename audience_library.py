"""Viewer likes and personally curated video collections."""

import re
import time
import uuid

from flask import abort, render_template, request, url_for

import community_analytics
import creator_blocks
from library_discovery import entry_payload

SCHEMA = """
CREATE TABLE IF NOT EXISTS audience_like(
 account TEXT NOT NULL, kind TEXT NOT NULL, slug TEXT NOT NULL, liked REAL NOT NULL,
 PRIMARY KEY(account,kind,slug));
CREATE INDEX IF NOT EXISTS audience_like_video ON audience_like(kind,slug);
CREATE TABLE IF NOT EXISTS audience_collection(
 id TEXT PRIMARY KEY, account TEXT NOT NULL, title TEXT NOT NULL,
 description TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS audience_collection_account ON audience_collection(account,created);
CREATE TABLE IF NOT EXISTS audience_collection_video(
 collection TEXT NOT NULL, kind TEXT NOT NULL, slug TEXT NOT NULL, added REAL NOT NULL,
 PRIMARY KEY(collection,kind,slug));
"""
COLLECTION_ID = re.compile(r"[a-f0-9]{32}")
VIDEO_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")


def remove_account(con, account):
    con.execute("DELETE FROM audience_like WHERE account=?", (account,))
    con.execute("""DELETE FROM audience_collection_video WHERE collection IN
                   (SELECT id FROM audience_collection WHERE account=?)""", (account,))
    con.execute("DELETE FROM audience_collection WHERE account=?", (account,))


def collection_payload(row):
    return dict(id=row["id"], title=row["title"], description=row["description"],
                url=url_for("viewer_collection_page", uid=row["id"]))


def own_collection(con, account, uid):
    row = con.execute("SELECT * FROM audience_collection WHERE id=? AND account=?", (uid, account)).fetchone()
    if row is None:
        abort(404)
    return row


def collection_fields(body):
    title, description = body.get("title"), body.get("description", "")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
        abort(400, "Enter a collection name of up to 120 characters.")
    if not isinstance(description, str) or len(description) > 1500:
        abort(400, "Use up to 1,500 characters for the description.")
    return title.strip(), description.strip()


def visible_videos(available, membership):
    return [entry_payload(available[key]) for key in map(tuple, membership) if key in available]


def register(app, tx, viewer, mutation, listing, private):
    @app.get("/watch/liked")
    def liked_videos_page():
        with tx() as con:
            account = viewer(con, required=False)
            rows = visible_videos(listing(con), con.execute(
                "SELECT kind,slug FROM audience_like WHERE account=? ORDER BY liked DESC,kind,slug",
                (account["id"],),
            )) if account else []
        return private(render_template("viewer_watch_later.html", signed_in=bool(account), rows=rows,
                                       title="Liked videos", endpoint="likes", empty_label="You haven’t liked any videos.",
                                       remove_label="Unlike"))

    @app.route("/api/viewer/likes/<any(work,meeting):kind>/<slug>", methods=("GET", "PUT", "DELETE"))
    def viewer_likes(kind, slug):
        if not VIDEO_KEY.fullmatch(slug):
            abort(404)
        read = request.method in ("GET", "HEAD")
        if not read:
            mutation()
        with tx() as con:
            account = viewer(con, required=not read)
            key = (kind, slug)
            uid = account["id"] if account else None
            if request.method == "DELETE":
                con.execute("DELETE FROM audience_like WHERE account=? AND kind=? AND slug=?", (uid, *key))
            if key not in listing(con, key):
                if request.method == "DELETE":
                    return private(dict(liked=False, likes=0))
                abort(404)
            if request.method == "PUT":
                if creator_blocks.video_blocked(con, uid, kind, slug):
                    abort(403, "You cannot like this video.")
                added = con.execute("INSERT OR IGNORE INTO audience_like VALUES(?,?,?,?)", (uid, *key, time.time()))
                if added.rowcount:
                    community_analytics.observe_like(con, uid, *key)
            total, liked = con.execute(
                "SELECT count(*),coalesce(max(account=?),0) FROM audience_like WHERE kind=? AND slug=?",
                (uid, *key),
            ).fetchone()
        return private(dict(liked=bool(liked), likes=total))

    @app.get("/watch/collections")
    def viewer_collections_page():
        with tx() as con:
            account = viewer(con, required=False)
            entries = [collection_payload(row) for row in con.execute(
                "SELECT * FROM audience_collection WHERE account=? ORDER BY created DESC,id", (account["id"],),
            )] if account else []
        return private(render_template("viewer_collections.html", signed_in=bool(account), collections=entries))

    @app.get("/watch/collections/<uid>")
    def viewer_collection_page(uid):
        if not COLLECTION_ID.fullmatch(uid):
            abort(404)
        with tx() as con:
            account = viewer(con, required=False)
            if account is None:
                abort(404)
            entry = collection_payload(own_collection(con, account["id"], uid))
            videos = visible_videos(listing(con), con.execute(
                "SELECT kind,slug FROM audience_collection_video WHERE collection=? ORDER BY added DESC,kind,slug",
                (uid,),
            ))
        return private(render_template("viewer_collection.html", collection=entry, rows=videos))

    @app.route("/api/viewer/collections", methods=("GET", "POST"))
    def viewer_collections():
        create = request.method == "POST"
        if create:
            title, description = collection_fields(mutation())
        with tx() as con:
            account = viewer(con)
            if create:
                uid = uuid.uuid4().hex
                con.execute("INSERT INTO audience_collection VALUES(?,?,?,?,?)",
                            (uid, account["id"], title, description, time.time()))
                return private(collection_payload(own_collection(con, account["id"], uid)), 201)
            kind, slug = request.args.get("kind"), request.args.get("slug")
            included = set()
            if kind is not None or slug is not None:
                if kind not in ("work", "meeting") or not VIDEO_KEY.fullmatch(slug or ""):
                    abort(404)
                if (kind, slug) not in listing(con, (kind, slug)):
                    abort(404)
                included = {row[0] for row in con.execute(
                    """SELECT v.collection FROM audience_collection_video v
                       JOIN audience_collection c ON c.id=v.collection
                       WHERE c.account=? AND v.kind=? AND v.slug=?""", (account["id"], kind, slug),
                )}
            return private([collection_payload(row) | {"included": row["id"] in included} for row in con.execute(
                "SELECT * FROM audience_collection WHERE account=? ORDER BY created DESC,id", (account["id"],),
            )])

    @app.route("/api/viewer/collections/<uid>", methods=("PUT", "DELETE"))
    def viewer_collection(uid):
        if not COLLECTION_ID.fullmatch(uid):
            abort(404)
        body = mutation()
        with tx() as con:
            account = viewer(con)
            own_collection(con, account["id"], uid)
            if request.method == "DELETE":
                con.execute("DELETE FROM audience_collection_video WHERE collection=?", (uid,))
                con.execute("DELETE FROM audience_collection WHERE id=?", (uid,))
                return private("", 204)
            title, description = collection_fields(body)
            con.execute("UPDATE audience_collection SET title=?,description=? WHERE id=?", (title, description, uid))
            return private(collection_payload(own_collection(con, account["id"], uid)))

    @app.route("/api/viewer/collections/<uid>/videos/<any(work,meeting):kind>/<slug>", methods=("PUT", "DELETE"))
    def viewer_collection_video(uid, kind, slug):
        if not COLLECTION_ID.fullmatch(uid) or not VIDEO_KEY.fullmatch(slug):
            abort(404)
        mutation()
        with tx() as con:
            account = viewer(con)
            own_collection(con, account["id"], uid)
            if request.method == "DELETE":
                con.execute("DELETE FROM audience_collection_video WHERE collection=? AND kind=? AND slug=?", (uid, kind, slug))
                return private(dict(included=False))
            entry = listing(con, (kind, slug)).get((kind, slug))
            if entry is None:
                abort(404)
            if not entry.get("allow_viewer_collections", True):
                abort(403)
            con.execute("INSERT OR IGNORE INTO audience_collection_video VALUES(?,?,?,?)", (uid, kind, slug, time.time()))
            return private(dict(included=True))
