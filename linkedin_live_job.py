"""Persist LinkedIn scheduling and announcement outcomes across retries."""

from contextlib import suppress
from threading import Event, Thread
from time import time

from filelock import FileLock, Timeout
from flask import abort
from werkzeug.exceptions import HTTPException

from linkedin_live import LinkedInWriteUnconfirmed


SCHEMA = """
CREATE TABLE IF NOT EXISTS linkedin_live_creation(
 event TEXT NOT NULL, opens INTEGER NOT NULL, company TEXT NOT NULL,
 event_revision TEXT NOT NULL, revision TEXT NOT NULL,
 state TEXT NOT NULL, video TEXT, post TEXT, PRIMARY KEY(event,opens,company));
CREATE TABLE IF NOT EXISTS linkedin_live_ingest(
 event TEXT NOT NULL, opens INTEGER NOT NULL, company TEXT NOT NULL,
 revision TEXT NOT NULL, phase TEXT NOT NULL, asset TEXT, destination TEXT, end_after INTEGER, bind_until INTEGER,
 PRIMARY KEY(event,opens,company));
"""
READ = "SELECT * FROM linkedin_live_creation WHERE event=? AND opens=? AND company=?"
UPDATE = """UPDATE linkedin_live_creation SET state=?,video=?,post=?
            WHERE event=? AND opens=? AND company=?"""


def provision(tx, check, create, announce, event, opens, company, event_revision, revision):
    key = (event, opens, company)
    with tx() as con:
        check(con)
        row = con.execute(READ, key).fetchone()
        if row:
            if (row["event_revision"], row["revision"]) != (event_revision, revision):
                abort(409, "The event or LinkedIn connection has changed. Check the existing schedule on LinkedIn.")
            if row["state"] == "scheduled":
                return {"state": "scheduled", "post_urn": row["post"]}, 200
            if row["state"] != "created":
                return {"state": "unconfirmed"}, 202
            video = row["video"]
        else:
            con.execute("INSERT INTO linkedin_live_creation VALUES(?,?,?,?,?,'creating',NULL,NULL)",
                        (*key, event_revision, revision))
            video = None

    if video is None:
        try:
            video = create()
        except LinkedInWriteUnconfirmed:
            return {"state": "unconfirmed"}, 202
        except HTTPException as error:
            if error.code is not None and 400 <= error.code < 500:
                with tx() as con:
                    con.execute("DELETE FROM linkedin_live_creation WHERE event=? AND opens=? AND company=?", key)
            raise
        with tx() as con:
            con.execute(UPDATE, ("created", video, None, *key))

    with tx() as con:
        check(con)
        con.execute(UPDATE, ("announcing", video, None, *key))
    try:
        post = announce(video)
    except LinkedInWriteUnconfirmed:
        return {"state": "unconfirmed"}, 202
    except HTTPException as error:
        if error.code is not None and 400 <= error.code < 500:
            with tx() as con:
                con.execute(UPDATE, ("created", video, None, *key))
        raise
    with tx() as con:
        con.execute(UPDATE, ("scheduled", video, post, *key))
    return {"state": "scheduled", "post_urn": post}, 201


INGEST = "SELECT * FROM linkedin_live_ingest WHERE event=? AND opens=? AND company=?"
SCOPE = " WHERE event=? AND opens=? AND company=?"


def prepare_ingest(tx, check, send, save, key, revision):
    with tx() as con:
        check(con)
        row = con.execute(INGEST, key).fetchone()
        if row:
            if row["phase"] in ("registered", "bound"):
                return {"state": "prepared", "id": row["destination"]}, 200
            return {"state": row["phase"] if row["asset"] else "unconfirmed"}, 202
        con.execute("INSERT INTO linkedin_live_ingest VALUES(?,?,?,?,'registering',NULL,NULL,NULL,NULL)",
                    (*key, revision))

    try:
        target = send()
    except LinkedInWriteUnconfirmed:
        return {"state": "unconfirmed"}, 202
    except HTTPException as error:
        if error.code is not None and 400 <= error.code < 500:
            with tx() as con:
                con.execute("DELETE FROM linkedin_live_ingest" + SCOPE, key)
        raise

    try:
        with tx() as con:
            check(con)
            destination = save(con, target)
            con.execute("UPDATE linkedin_live_ingest SET phase='registered',asset=?,destination=?" + SCOPE,
                        (target["asset"], destination, *key))
    except HTTPException:
        with tx() as con:
            con.execute("UPDATE linkedin_live_ingest SET phase='closing',asset=?,end_after=?" + SCOPE,
                        (target["asset"], int(time()) + 10, *key))
        raise
    return {"state": "prepared", "id": destination}, 201


class Broadcasts:
    """Advance acknowledged LinkedIn assets independently of the shared relay scheduler."""

    def __init__(self, app, tx, connection, outputs, lease, eligible):
        self.app = app
        self.tx = tx
        self.connection = connection
        self.outputs = outputs
        self.lease = lease
        self.eligible = eligible
        self.stopping = Event()
        self.thread = Thread(target=self.scan, name="CuePorch LinkedIn Live", daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.thread.join(2)

    def scan(self):
        while not self.stopping.wait(2):
            try:
                with self.tx() as con:
                    keys = con.execute("""SELECT event,opens,company FROM linkedin_live_ingest
                        WHERE phase IN ('registered','bound','closing')""").fetchall()
                for row in keys:
                    if self.stopping.is_set():
                        return
                    key = tuple(row)
                    with suppress(Timeout):
                        with FileLock(self.lease(key), timeout=0):
                            self.advance(key)
            except Exception:
                self.app.logger.exception("CuePorch could not advance LinkedIn Live delivery")

    def advance(self, key):
        with self.tx() as con:
            row = con.execute(INGEST, key).fetchone()
            if not row or row["phase"] not in ("registered", "bound", "closing"):
                return
            saved = self.connection.connection(con)
            if not saved or saved["revision"] != row["revision"]:
                self.close_destination(con, row, key, "disconnected")
                return
            creation = con.execute(READ, key).fetchone()
            if row["phase"] != "closing" and not self.eligible(con, row, creation):
                self.close_destination(con, row, key, "closing")
                return
            if row["phase"] == "registered" and row["bind_until"] is not None and time() >= row["bind_until"]:
                self.close_destination(con, row, key, "closing")
                return
            if row["phase"] == "closing":
                if time() < row["end_after"]:
                    return
                con.execute("UPDATE linkedin_live_ingest SET phase='end_unconfirmed'" + SCOPE, key)

        try:
            if row["phase"] == "closing":
                self.connection.end_asset(row["revision"], row["asset"])
                with self.tx() as con:
                    con.execute("UPDATE linkedin_live_ingest SET phase='ended'" + SCOPE, key)
            elif row["phase"] == "registered" and self.outputs.state(*key[:2], row["destination"]) == "sending":
                with self.tx() as con:
                    con.execute("UPDATE linkedin_live_ingest SET bind_until=COALESCE(bind_until,?)" + SCOPE,
                                (int(time()) + 90, *key))
                revision, video, asset = row["revision"], creation["video"], row["asset"]
                if not self.connection.asset_available(revision, asset):
                    return
                current = self.connection.bound_asset(revision, video)
                if current is None:
                    self.connection.bind_asset(revision, video, asset, lambda: self.check_binding(key))
                elif current != asset:
                    abort(422, "The scheduled LinkedIn broadcast has a different stream.")
                with self.tx() as con:
                    con.execute("UPDATE linkedin_live_ingest SET phase='bound'" + SCOPE, key)
        except HTTPException as error:
            if row["phase"] != "closing" and error.code in (403, 404, 422):
                with self.tx() as con:
                    self.close_destination(con, row, key, "closing")

    def check_binding(self, key):
        with self.tx() as con:
            row = con.execute(INGEST, key).fetchone()
            creation = con.execute(READ, key).fetchone()
            if not row or not self.eligible(con, row, creation):
                abort(409, "The LinkedIn Live occurrence is no longer active.")

    def close_destination(self, con, row, key, phase):
        con.execute("UPDATE live_simulcast_destination SET enabled=0 WHERE id=?", (row["destination"],))
        con.execute("UPDATE linkedin_live_ingest SET phase=?,end_after=?" + SCOPE,
                    (phase, int(time()) + 10, *key))
