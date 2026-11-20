"""Retain one folder cursor and import new Dropbox revisions in the background."""

import json
from datetime import datetime
import time
import uuid
from threading import Event, Thread

from filelock import FileLock, Timeout
from flask import abort
from werkzeug.exceptions import HTTPException

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_dropbox_watch(
 id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL, connection TEXT NOT NULL,
 source TEXT NOT NULL, destination TEXT, actor TEXT NOT NULL, cursor TEXT NOT NULL,
 checked INTEGER, error TEXT, started INTEGER NOT NULL, recovering INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS workspace_dropbox_queue(
 id INTEGER PRIMARY KEY, watch TEXT NOT NULL, file TEXT NOT NULL, version TEXT NOT NULL,
 entry TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'waiting', work TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0, error TEXT,
 UNIQUE(watch,file,version));
"""
POLL_SECONDS = 30


class CursorReset(Exception):
    pass


def migrate(con):
    columns = {row[1] for row in con.execute("PRAGMA table_info(workspace_dropbox_watch)")}
    if "started" not in columns:
        con.execute("ALTER TABLE workspace_dropbox_watch ADD COLUMN started INTEGER NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE workspace_dropbox_watch ADD COLUMN recovering INTEGER NOT NULL DEFAULT 0")
        con.execute("UPDATE workspace_dropbox_watch SET started=coalesce(checked,unixepoch())")


def changed_since(row, cutoff):
    try:
        if not isinstance(row.get("server_modified"), str):
            return False
        modified = datetime.fromisoformat(row["server_modified"])
        return modified.tzinfo is not None and modified.timestamp() >= cutoff
    except (ValueError, OverflowError, OSError):
        return False


class Watch:
    def __init__(self, app, tx, database, connection, list_changes, is_video, ingest):
        self.app, self.tx, self.connection = app, tx, connection
        self.list_changes, self.is_video, self.ingest = list_changes, is_video, ingest
        self.lock = FileLock(str(database.with_suffix(".dropbox-watch.lock")), timeout=0)
        self.wake, self.closed = Event(), Event()
        self.worker = None
        if connection.configured:
            self.worker = Thread(target=self.run, name="CuePorch Dropbox watch", daemon=True)
            self.worker.start()

    def stop(self):
        self.closed.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=10)

    @staticmethod
    def current(con):
        return con.execute("SELECT * FROM workspace_dropbox_watch WHERE id=1").fetchone()

    @staticmethod
    def clear(con):
        con.execute("DELETE FROM workspace_dropbox_watch")
        con.execute("DELETE FROM workspace_dropbox_queue")

    def disable(self):
        with self.tx() as con:
            self.clear(con)

    def enable(self, connection, source, destination, actor, cursor):
        with self.tx() as con:
            self.connection.authorize(con, connection)
            if destination and not con.execute(
                """SELECT 1 FROM library_folder WHERE id=? AND NOT EXISTS(
                   SELECT 1 FROM library_deleted_item WHERE kind='folder' AND item=?)""",
                (destination, destination),
            ).fetchone():
                abort(404, "The selected Library folder no longer exists.")
            self.clear(con)
            con.execute("""INSERT INTO workspace_dropbox_watch(
                            id,revision,connection,source,destination,actor,cursor,started)
                            VALUES(1,?,?,?,?,?,?,?)""",
                        (uuid.uuid4().hex, connection, source, destination, actor, cursor, int(time.time())))
        self.wake.set()

    def receipt(self):
        with self.tx() as con:
            row = self.current(con)
            if not row:
                return {"enabled": False, "jobs": []}
            jobs = con.execute("""SELECT id,entry,state,work,error FROM workspace_dropbox_queue
                                  WHERE watch=? ORDER BY id DESC LIMIT 50""", (row["revision"],)).fetchall()
        return dict(enabled=True, source=row["source"], destination=row["destination"],
                    checked=row["checked"], error=row["error"],
                    jobs=[dict(id=job["id"], name=json.loads(job["entry"])["name"], state=job["state"],
                               work=job["work"], error=job["error"]) for job in jobs])

    def retry(self, job):
        with self.tx() as con:
            row = self.current(con)
            if not row or not con.execute("""UPDATE workspace_dropbox_queue
                SET state='waiting',attempts=0,next_attempt=0,error=NULL
                WHERE id=? AND watch=? AND state='failed'""", (job, row["revision"])).rowcount:
                abort(404, "That failed watch import is no longer available.")
        self.wake.set()

    def authorized(self, con, watch):
        active = self.current(con)
        if self.closed.is_set() or not active or active["revision"] != watch["revision"]:
            abort(409, "Dropbox watch settings changed.")
        self.connection.authorize(con, watch["connection"])

    def poll(self, watch):
        try:
            page = self.list_changes(watch["connection"], watch["source"], watch["cursor"])
        except CursorReset:
            with self.tx() as con:
                self.authorized(con, watch)
                con.execute("""UPDATE workspace_dropbox_watch SET cursor='',recovering=1,error=?
                               WHERE revision=?""",
                            ("Dropbox reset the cursor. Checking for new versions.", watch["revision"]))
            return True
        except HTTPException as error:
            with self.tx() as con:
                con.execute("UPDATE workspace_dropbox_watch SET error=? WHERE revision=?",
                            (error.description, watch["revision"]))
            return False
        with self.tx() as con:
            self.authorized(con, watch)
            for row in page["entries"]:
                if not self.is_video(row) or not isinstance(row.get("rev"), str):
                    continue
                if watch["recovering"] and not changed_since(row, watch["started"]):
                    continue
                entry = {key: row.get(key) for key in (".tag", "id", "name", "rev", "size", "content_hash")}
                con.execute("""INSERT OR IGNORE INTO workspace_dropbox_queue(watch,file,version,entry)
                               VALUES(?,?,?,?)""", (watch["revision"], row["id"], row["rev"], json.dumps(entry)))
            recovering = bool(watch["recovering"] and page["has_more"])
            con.execute("""UPDATE workspace_dropbox_watch SET cursor=?,checked=?,recovering=?,error=?
                           WHERE revision=?""", (
                            page["cursor"], int(time.time()), recovering,
                            "Checking for new versions after the Dropbox cursor reset." if recovering else None,
                            watch["revision"],
                        ))
        return page["has_more"]

    def import_next(self, watch):
        with self.tx() as con:
            self.authorized(con, watch)
            job = con.execute("""SELECT * FROM workspace_dropbox_queue WHERE watch=? AND state='waiting'
                                 AND next_attempt<=? ORDER BY id LIMIT 1""",
                              (watch["revision"], int(time.time()))).fetchone()
        if not job:
            return False
        try:
            work, _ = self.ingest(
                watch["connection"], watch["actor"], json.loads(job["entry"]), watch["destination"],
                lambda con: self.authorized(con, watch),
            )
        except HTTPException as error:
            attempts = job["attempts"] + 1
            waiting = (error.code == 429 or error.code >= 500) and attempts < 3
            delay = max(15 * attempts, int(getattr(error, "retry_after", 0) or 0))
            with self.tx() as con:
                con.execute("""UPDATE workspace_dropbox_queue SET state=?,attempts=?,next_attempt=?,error=?
                               WHERE id=? AND watch=?""",
                            ("waiting" if waiting else "failed", attempts, int(time.time()) + delay,
                             error.description, job["id"], watch["revision"]))
            return True
        with self.tx() as con:
            con.execute("""UPDATE workspace_dropbox_queue SET state='imported',work=?,error=NULL
                           WHERE id=? AND watch=?""", (work["id"], job["id"], watch["revision"]))
        return True

    def tick(self):
        for _ in range(5):
            if self.closed.is_set():
                return
            with self.tx() as con:
                watch = self.current(con)
            if not watch or not self.poll(watch):
                break
        if not watch:
            return
        for _ in range(5):
            if self.closed.is_set() or not self.import_next(watch):
                break

    def run(self):
        while not self.closed.is_set():
            try:
                with self.lock, self.app.app_context():
                    self.tick()
            except (Timeout, HTTPException):
                # Another server owns the poll or settings changed during this pass.
                pass
            except Exception:
                self.app.logger.exception("Dropbox watch pass failed")
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()
