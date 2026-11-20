"""Persist a provider cursor and import new revisions in the background."""

import json
from datetime import datetime
import time
import uuid
from threading import Event, Thread

from filelock import FileLock, Timeout
from flask import abort
from werkzeug.exceptions import HTTPException

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_intake_watch(
 provider TEXT PRIMARY KEY, revision TEXT NOT NULL, connection TEXT NOT NULL,
 source TEXT NOT NULL, destination TEXT, actor TEXT NOT NULL, cursor TEXT NOT NULL,
 checked INTEGER, error TEXT, started INTEGER NOT NULL, recovering INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS workspace_intake_queue(
 id INTEGER PRIMARY KEY, watch TEXT NOT NULL, file TEXT NOT NULL, version TEXT NOT NULL,
 entry TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'waiting', work TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL DEFAULT 0, error TEXT,
 notice_due INTEGER, notice_attempts INTEGER NOT NULL DEFAULT 0, notice_state TEXT, notice_actor TEXT,
 UNIQUE(watch,file,version));
"""
POLL_SECONDS = 30


class CursorReset(Exception):
    pass


def migrate(con):
    queue_columns = {row[1] for row in con.execute("PRAGMA table_info(workspace_intake_queue)")}
    for name, sql_type in (("notice_due", "INTEGER"), ("notice_attempts", "INTEGER NOT NULL DEFAULT 0"),
                           ("notice_state", "TEXT"), ("notice_actor", "TEXT")):
        if name not in queue_columns:
            con.execute(f"ALTER TABLE workspace_intake_queue ADD COLUMN {name} {sql_type}")
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_dropbox_watch'").fetchone():
        return
    columns = {row[1] for row in con.execute("PRAGMA table_info(workspace_dropbox_watch)")}
    dates = "started,recovering" if "started" in columns else "coalesce(checked,unixepoch()),0"
    con.execute(f"""INSERT INTO workspace_intake_watch
                    SELECT 'dropbox',revision,connection,source,destination,actor,cursor,checked,error,{dates}
                    FROM workspace_dropbox_watch WHERE id=1""")
    fields = "id,watch,file,version,entry,state,work,attempts,next_attempt,error"
    con.execute(f"INSERT INTO workspace_intake_queue ({fields}) SELECT {fields} FROM workspace_dropbox_queue")
    con.execute("DROP TABLE workspace_dropbox_queue")
    con.execute("DROP TABLE workspace_dropbox_watch")


def changed_since(row, cutoff):
    try:
        if not isinstance(row.get("server_modified"), str):
            return False
        modified = datetime.fromisoformat(row["server_modified"])
        return modified.tzinfo is not None and modified.timestamp() >= cutoff
    except (ValueError, OverflowError, OSError):
        return False


class Watch:
    def __init__(self, app, tx, database, connection, list_changes, queue_entry, ingest, interval=None, notify=None):
        self.app, self.tx, self.connection = app, tx, connection
        self.list_changes, self.queue_entry, self.ingest = list_changes, queue_entry, ingest
        self.provider = connection.config.key
        self.notify = notify
        self.interval = interval if interval is not None else POLL_SECONDS
        self.lock = FileLock(str(database.with_suffix(f".{self.provider}-watch.lock")), timeout=0)
        self.wake, self.closed = Event(), Event()
        self.worker = None
        if connection.configured:
            self.worker = Thread(target=self.run, name=f"CuePorch {connection.config.name} watch", daemon=True)
            self.worker.start()

    def stop(self):
        self.closed.set()
        self.wake.set()
        if self.worker:
            self.worker.join(timeout=10)

    def current(self, con):
        return con.execute("SELECT * FROM workspace_intake_watch WHERE provider=?", (self.provider,)).fetchone()

    def clear(self, con):
        con.execute("""DELETE FROM workspace_intake_queue WHERE watch IN (
                       SELECT revision FROM workspace_intake_watch WHERE provider=?)""", (self.provider,))
        con.execute("DELETE FROM workspace_intake_watch WHERE provider=?", (self.provider,))

    def disable(self):
        with self.tx() as con:
            self.clear(con)

    def enable(self, connection, source, destination, actor, cursor, keep_queue=False):
        with self.tx() as con:
            self.connection.authorize(con, connection)
            if destination and not con.execute(
                """SELECT 1 FROM library_folder WHERE id=? AND NOT EXISTS(
                   SELECT 1 FROM library_deleted_item WHERE kind='folder' AND item=?)""",
                (destination, destination),
            ).fetchone():
                abort(404, "The selected Library folder no longer exists.")
            active = self.current(con)
            revision = uuid.uuid4().hex
            if (keep_queue and active and (active["connection"], active["source"]) == (connection, source)):
                if (active["destination"], active["actor"]) == (destination, actor):
                    return
                con.execute("""UPDATE workspace_intake_watch SET revision=?,destination=?,actor=? WHERE provider=?""",
                            (revision, destination, actor, self.provider))
                con.execute("UPDATE workspace_intake_queue SET watch=? WHERE watch=?", (revision, active["revision"]))
            else:
                self.clear(con)
                con.execute("""INSERT INTO workspace_intake_watch(
                                provider,revision,connection,source,destination,actor,cursor,started)
                                VALUES(?,?,?,?,?,?,?,?)""",
                            (self.provider, revision, connection, source, destination, actor, cursor, int(time.time())))
        self.wake.set()

    def receipt(self):
        with self.tx() as con:
            row = self.current(con)
            if not row:
                return {"enabled": False, "jobs": []}
            jobs = con.execute("""SELECT id,entry,state,work,error,notice_state FROM workspace_intake_queue
                                  WHERE watch=? ORDER BY id DESC LIMIT 50""", (row["revision"],)).fetchall()
        return dict(enabled=True, source=row["source"], destination=row["destination"],
                    checked=row["checked"], error=row["error"],
                    jobs=[dict(id=job["id"], name=json.loads(job["entry"])["name"], state=job["state"],
                               work=job["work"], error=job["error"],
                               **({"email": job["notice_state"]} if job["notice_state"] else {})) for job in jobs])

    def retry(self, job):
        with self.tx() as con:
            row = self.current(con)
            if not row or not con.execute("""UPDATE workspace_intake_queue
                SET state='waiting',attempts=0,next_attempt=0,error=NULL
                WHERE id=? AND watch=? AND state='failed'""", (job, row["revision"])).rowcount:
                abort(404, "That failed watch import is no longer available.")
        self.wake.set()

    def authorized(self, con, watch):
        active = self.current(con)
        if self.closed.is_set() or not active or active["revision"] != watch["revision"]:
            abort(409, "Automatic import settings changed.")
        self.connection.authorize(con, watch["connection"])

    def poll(self, watch):
        try:
            page = self.list_changes(watch)
        except CursorReset:
            with self.tx() as con:
                self.authorized(con, watch)
                con.execute("""UPDATE workspace_intake_watch SET cursor='',recovering=1,error=?
                               WHERE revision=?""",
                            ("Dropbox reset the cursor. Checking for new versions.", watch["revision"]))
            return True
        except HTTPException as error:
            with self.tx() as con:
                con.execute("UPDATE workspace_intake_watch SET error=? WHERE revision=?",
                            (error.description, watch["revision"]))
            return False
        with self.tx() as con:
            self.authorized(con, watch)
            for row in page["entries"]:
                entry = self.queue_entry(row, watch)
                if not entry:
                    continue
                con.execute("""INSERT OR IGNORE INTO workspace_intake_queue(watch,file,version,entry)
                               VALUES(?,?,?,?)""", (watch["revision"], entry["id"], entry["rev"], json.dumps(entry)))
            recovering = bool(watch["recovering"] and page["has_more"])
            con.execute("""UPDATE workspace_intake_watch SET cursor=?,checked=?,recovering=?,error=?
                           WHERE revision=?""", (
                            page["cursor"], int(time.time()), recovering,
                            "Checking for new versions after the Dropbox cursor reset." if recovering else None,
                            watch["revision"],
                        ))
        return page["has_more"]

    def import_next(self, watch):
        with self.tx() as con:
            self.authorized(con, watch)
            job = con.execute("""SELECT * FROM workspace_intake_queue WHERE watch=? AND state='waiting'
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
                con.execute("""UPDATE workspace_intake_queue SET state=?,attempts=?,next_attempt=?,error=?
                               WHERE id=? AND watch=?""",
                            ("waiting" if waiting else "failed", attempts, int(time.time()) + delay,
                             error.description, job["id"], watch["revision"]))
            return True
        with self.tx() as con:
            due = int(time.time()) if self.notify else None
            con.execute("""UPDATE workspace_intake_queue SET state='imported',work=?,error=NULL,notice_due=?,
                           notice_state=?,notice_actor=? WHERE id=? AND watch=?""",
                        (work["id"], due, "queued" if due is not None else None,
                         watch["actor"] if due is not None else None, job["id"], watch["revision"]))
        return True

    def send_notice(self, watch):
        with self.tx() as con:
            self.authorized(con, watch)
            job = con.execute("""SELECT id,work,notice_attempts,notice_actor FROM workspace_intake_queue
                                 WHERE watch=? AND notice_due<=? ORDER BY notice_due,id LIMIT 1""",
                              (watch["revision"], int(time.time()))).fetchone()
        if not job:
            return
        accepted = self.notify(job["notice_actor"], self.connection.config.name, job["work"])
        attempts = job["notice_attempts"] + 1
        due = int(time.time()) + 30 * attempts if accepted is False and attempts < 3 else None
        state = {True: "accepted", False: "queued" if due else "failed", None: None}[accepted]
        with self.tx() as con:
            con.execute("""UPDATE workspace_intake_queue SET notice_due=?,notice_attempts=?,notice_state=?
                           WHERE id=? AND work=? AND watch IN (
                               SELECT revision FROM workspace_intake_watch WHERE provider=? AND connection=?)""",
                        (due, attempts, state, job["id"], job["work"], self.provider, watch["connection"]))

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
        if self.notify and not self.closed.is_set():
            self.send_notice(watch)

    def run(self):
        while not self.closed.is_set():
            try:
                with self.lock, self.app.app_context():
                    self.tick()
            except (Timeout, HTTPException):
                # Another server owns the poll or settings changed during this pass.
                pass
            except Exception:
                self.app.logger.exception("%s automatic import pass failed", self.provider)
            self.wake.wait(self.interval)
            self.wake.clear()
