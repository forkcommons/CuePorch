"""Keep track of subtitle translation requests across selected videos and folder trees."""

import json
from time import time
from uuid import UUID, uuid4

from flask import abort, request
from werkzeug.exceptions import Conflict

import caption_adapter
import library_caption_jobs
import library_languages
from browser_account_security import private
from library_trash import hidden


SCHEMA = """
CREATE TABLE IF NOT EXISTS subtitle_translation_batch(
 id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL, folder TEXT, created REAL NOT NULL,
 selection TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subtitle_translation_item(
 batch TEXT NOT NULL REFERENCES subtitle_translation_batch(id), work TEXT NOT NULL, title TEXT NOT NULL,
 job TEXT REFERENCES caption_job(id), reason TEXT, PRIMARY KEY(batch,work));
"""
LIMIT = 500
RETRYABLE = {"failed", "superseded", "rejected", "skipped"}
REPORT = """SELECT i.work,i.title,i.job,
    CASE WHEN a.status='waiting' AND j.state='queued' THEN 'waiting'
         WHEN a.status='rejected' THEN 'rejected' ELSE coalesce(j.state,'skipped') END AS state,
    coalesce(j.error,i.reason) AS reason
    FROM subtitle_translation_item i LEFT JOIN caption_job j ON j.id=i.job
    LEFT JOIN action a ON a.id=j.id WHERE i.batch=? ORDER BY i.title COLLATE NOCASE,i.work"""


def ids_in_folder(con, folder):
    exists = con.execute("SELECT 1 FROM library_folder WHERE id=?", (folder,)).fetchone()
    if not exists or hidden(con, "folder", folder):
        abort(404, "The selected folder is unavailable.")
    rows = con.execute("""WITH RECURSIVE branch(id) AS (
        SELECT id FROM library_folder WHERE id=?
        UNION
        SELECT f.id FROM library_folder f JOIN branch b ON b.id=f.parent
        WHERE NOT EXISTS (SELECT 1 FROM library_deleted_item d WHERE d.kind='folder' AND d.item=f.id)
    ) SELECT p.work FROM library_placement p JOIN branch ON branch.id=p.folder
      ORDER BY p.work LIMIT ?""", (folder, LIMIT + 1))
    return [row[0] for row in rows]


def report(con, batch):
    return {key: batch[key] for key in ("id", "source", "target", "folder", "created")} | {
        "items": [dict(row) for row in con.execute(REPORT, (batch["id"],))],
    }


def selection(body):
    works, folder = body.get("works", []), body.get("folder")
    if (not isinstance(works, list) or len(works) > LIMIT
            or any(not isinstance(work, str) or len(work) != 36 for work in works)
            or (folder is not None and (not isinstance(folder, str) or len(folder) != 32))
            or bool(works) == bool(folder)):
        abort(400, f"Choose a folder or up to {LIMIT} selected videos.")
    return sorted(set(works)), folder


def batch_id(value):
    if value is None:
        return str(uuid4())
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        abort(400, "Use a valid subtitle translation request ID.")


def queue_items(con, action, read_works, batch, selected):
    works = {work["id"]: work for work in read_works()}
    for uid in selected:
        work = works.get(uid)
        job, reason = None, None
        if not work or hidden(con, "work", uid) or work.get("asset_type", "video") != "video":
            reason = "This video is unavailable."
        elif not library_caption_jobs.translation_source(work, batch["source"]):
            reason = "This cut has no subtitle track in the source language."
        else:
            try:
                job = library_caption_jobs.queue(con, action, work, source_language=batch["source"],
                                                 target_language=batch["target"])
            except Conflict as error:
                reason = error.description
        con.execute("""INSERT INTO subtitle_translation_item(batch,work,title,job,reason) VALUES(?,?,?,?,?)
            ON CONFLICT(batch,work) DO UPDATE SET title=excluded.title,job=excluded.job,reason=excluded.reason""",
            (batch["id"], uid, work["title"] if work else uid, job, reason))


def register(app, tx, staff, read_works, action):
    @app.route("/api/library/subtitle-translations", methods=("GET", "POST"))
    def subtitle_translations():
        staff()
        if request.method in ("GET", "HEAD"):
            with tx() as con:
                recent = [dict(row) for row in con.execute("""SELECT b.id,b.source,b.target,b.created,count(i.work) AS items
                    FROM subtitle_translation_batch b LEFT JOIN subtitle_translation_item i ON i.batch=b.id
                    GROUP BY b.id ORDER BY b.created DESC LIMIT 30""")]
            return private(dict(batches=recent))
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            abort(400, "Specify selected videos and subtitle languages.")
        source = library_languages.language_tag(body.get("source_language"))
        target = library_languages.language_tag(body.get("target_language"))
        if source == target:
            abort(400, "Choose a different target language.")
        works, folder = selection(body)
        uid = batch_id(body.get("id"))
        saved_selection = json.dumps(works)
        with tx() as con:
            existing = con.execute("SELECT * FROM subtitle_translation_batch WHERE id=?", (uid,)).fetchone()
            if existing:
                if (source, target, folder, saved_selection) != (
                    existing["source"], existing["target"], existing["folder"], existing["selection"],
                ):
                    abort(409, "This request ID already belongs to a different subtitle translation batch.")
                return private(report(con, existing))
            if not caption_adapter.translation_runtime():
                abort(409, "Configure TextFlowKit and a local translation model first.")
            selected = ids_in_folder(con, folder) if folder else works
            if not 1 <= len(selected) <= LIMIT:
                abort(409, f"Choose a folder with one to {LIMIT} library items.")
            batch = dict(id=uid, source=source, target=target, folder=folder, created=time())
            con.execute("INSERT INTO subtitle_translation_batch VALUES(?,?,?,?,?,?)",
                        (uid, source, target, folder, batch["created"], saved_selection))
            queue_items(con, action, read_works, batch, selected)
            return private(report(con, batch), 201)

    @app.route("/api/library/subtitle-translations/<uuid:batch_id>", methods=("GET", "PUT"))
    def subtitle_translation_batch(batch_id):
        staff()
        with tx() as con:
            batch = con.execute("SELECT * FROM subtitle_translation_batch WHERE id=?", (str(batch_id),)).fetchone()
            if batch is None:
                abort(404)
            if request.method == "PUT":
                if not caption_adapter.translation_runtime():
                    abort(409, "Configure TextFlowKit and a local translation model first.")
                retry = [row["work"] for row in con.execute(REPORT, (str(batch_id),)) if row["state"] in RETRYABLE]
                queue_items(con, action, read_works, batch, retry)
            return private(report(con, batch))
