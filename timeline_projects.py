"""Keep independent timeline projects private until their first render."""

import uuid
from datetime import datetime, timezone

from flask import abort, request
from werkzeug.exceptions import NotFound

import library_trash

PROVIDER = "cueporch-timeline"

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS timeline_project ("
    " id TEXT PRIMARY KEY,"
    " actor TEXT NOT NULL,"
    " title TEXT NOT NULL,"
    " added TEXT NOT NULL,"
    " published INTEGER NOT NULL DEFAULT 0);\n"
)


def draft_for(con, uid, actor):
    row = con.execute(
        "SELECT * FROM timeline_project WHERE id = ?", (uid,)
    ).fetchone()
    if row is None or row["published"]:
        return None
    if row["actor"] != actor:
        abort(404)
    return row


def mark_published(con, uid):
    con.execute("INSERT OR IGNORE INTO library_placement(work,folder) VALUES(?,NULL)", (uid,))
    con.execute(
        "UPDATE timeline_project SET published = 1 WHERE id = ?", (uid,)
    )


def new_work(row):
    return {
        "id": row["id"],
        "source_id": row["id"],
        "provider": PROVIDER,
        "source_url": f"/operator/videos/{row['id']}/timeline",
        "title": row["title"],
        "files": [],
        "publication": "private",
        "source_privacy": "private",
        "source_embed_policy": "private",
        "source_creator": row["actor"],
        "asset_type": "video",
        "library_added_at": row["added"],
    }


def target(con, uid, actor, get_work):
    draft = draft_for(con, uid, actor)
    if draft is None:
        return get_work(con, uid), None
    if library_trash.hidden(con, "work", uid):
        abort(404)
    try:
        existing = get_work(con, uid)
    except NotFound:
        return new_work(draft), draft
    if (
        existing.get("provider") == PROVIDER
        and existing.get("source_id") == uid
    ):
        return existing, draft
    abort(409)


def register(app, tx, staff):
    @app.get("/api/library/timeline-projects")
    def list_projects():
        actor = staff()
        with tx() as con:
            rows = con.execute(
                "SELECT id, title FROM timeline_project"
                " WHERE actor = ? AND published = 0"
                " ORDER BY added DESC, id",
                (actor,),
            ).fetchall()
        return {"projects": [
            {"id": r["id"], "title": r["title"]} for r in rows
        ]}

    @app.post("/api/library/timeline-projects")
    def create_project():
        actor = staff()
        data = request.get_json(silent=True)
        title = data.get("title") if isinstance(data, dict) else None
        if not isinstance(title, str):
            abort(400, "Enter a project title of up to 128 characters.")
        title = title.strip()
        if not 1 <= len(title) <= 128 or not title.isprintable():
            abort(400, "Enter a project title of up to 128 characters.")
        uid = str(uuid.uuid4())
        added = datetime.now(timezone.utc).isoformat()
        with tx() as con:
            con.execute(
                "INSERT INTO timeline_project"
                " (id, actor, title, added, published)"
                " VALUES (?, ?, ?, ?, 0)",
                (uid, actor, title, added),
            )
        return {"id": uid, "title": title}, 201
