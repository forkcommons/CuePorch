"""Persist LinkedIn scheduling and announcement outcomes across retries."""

from contextlib import suppress
from threading import Event, Thread
from time import time

from filelock import FileLock, Timeout
from flask import abort
from werkzeug.exceptions import HTTPException, TooManyRequests

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
CREATE TABLE IF NOT EXISTS linkedin_live_auto(
 event TEXT NOT NULL, opens INTEGER NOT NULL, company TEXT NOT NULL, state TEXT NOT NULL,
 retry_at INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(event,opens,company));
CREATE TABLE IF NOT EXISTS linkedin_live_restart(
 replaces TEXT PRIMARY KEY, event TEXT NOT NULL, opens INTEGER NOT NULL, company TEXT NOT NULL,
 old_video TEXT NOT NULL, old_asset TEXT NOT NULL, UNIQUE(event,opens,company));
"""
READ = "SELECT * FROM linkedin_live_creation WHERE event=? AND opens=? AND company=?"
UPDATE = """UPDATE linkedin_live_creation SET state=?,video=?,post=?
            WHERE event=? AND opens=? AND company=?"""


AUTO = "SELECT state FROM linkedin_live_auto WHERE event=? AND opens=? AND company=?"
SCOPE = " WHERE event=? AND opens=? AND company=?"
RESTART = "SELECT * FROM linkedin_live_restart" + SCOPE


class AutomaticIngestDeferred(Exception):
    """A local check deferred automatic registration before the provider request."""


def provider_for(owner_key):
    return "linkedin-member-live" if owner_key.startswith("member:") else "linkedin-live"


def migrate(con):
    cols = [r[1] for r in con.execute("PRAGMA table_info(linkedin_live_auto)")]
    if "retry_at" not in cols:
        con.execute("ALTER TABLE linkedin_live_auto ADD COLUMN retry_at INTEGER NOT NULL DEFAULT 0")


def set_auto(con, key, enabled):
    if enabled:
        con.execute("INSERT OR IGNORE INTO linkedin_live_auto(event,opens,company,state) VALUES(?,?,?,'queued')", key)
    else:
        con.execute("DELETE FROM linkedin_live_auto" + SCOPE, key)


def due_auto(con, now):
    return con.execute("""SELECT c.event,c.opens,c.company FROM linkedin_live_creation c
        JOIN linkedin_live_auto a ON a.event=c.event AND a.opens=c.opens AND a.company=c.company
        JOIN live_occurrence o ON o.event=c.event AND o.opens=c.opens AND o.delivery='ready'
        JOIN workspace_integration_connection g ON g.revision=c.revision
          AND g.provider=CASE WHEN c.company LIKE 'member:%' THEN 'linkedin-member-live' ELSE 'linkedin-live' END
        LEFT JOIN linkedin_live_ingest i ON i.event=c.event AND i.opens=c.opens AND i.company=c.company
        WHERE c.state='scheduled' AND a.state='queued' AND i.event IS NULL
        AND NOT EXISTS (SELECT 1 FROM live_destination_series_skip s
                        WHERE s.event=c.event AND s.opens=c.opens AND s.owner=c.company AND s.provider=g.provider)
        AND c.opens BETWEEN ? AND ? AND a.retry_at<=? ORDER BY c.opens""",
        (now - 7200, now, now)).fetchall()


def claim_auto(con, key):
    ready = con.execute("SELECT 1 FROM live_occurrence WHERE event=? AND opens=? AND delivery='ready'",
                        key[:2]).fetchone()
    if not ready or con.execute(INGEST, key).fetchone():
        return False
    return con.execute("UPDATE linkedin_live_auto SET state='attempted'" + SCOPE +
                       " AND state='queued' AND retry_at<=?",
                       key + (int(time()),)).rowcount == 1


def provision(tx, check, create, announce, event, opens, company, event_revision, revision, auto_prepare=None):
    key = (event, opens, company)
    with tx() as con:
        check(con)
        row = con.execute(READ, key).fetchone()
        if row:
            if (row["event_revision"], row["revision"]) != (event_revision, revision):
                abort(409, "The event or LinkedIn connection has changed. Check the existing schedule on LinkedIn.")
            if row["state"] == "scheduled":
                if auto_prepare is not None:
                    set_auto(con, key, auto_prepare)
                return {"state": "scheduled", "post_urn": row["post"],
                        "auto_prepare": bool(con.execute(AUTO, key).fetchone())}, 200
            if row["state"] != "created":
                return {"state": "unconfirmed"}, 202
            video = row["video"]
        else:
            con.execute("INSERT INTO linkedin_live_creation VALUES(?,?,?,?,?,'creating',NULL,NULL)",
                        (*key, event_revision, revision))
            set_auto(con, key, auto_prepare is not False)
            video = None

    if video is None:
        try:
            video = create()
        except LinkedInWriteUnconfirmed:
            return {"state": "unconfirmed"}, 202
        except HTTPException as error:
            if error.code is not None and 400 <= error.code < 500:
                with tx() as con:
                    con.execute("DELETE FROM linkedin_live_creation" + SCOPE, key)
                    set_auto(con, key, False)
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
        if auto_prepare is not None:
            set_auto(con, key, auto_prepare)
        automatic = bool(con.execute(AUTO, key).fetchone())
    return {"state": "scheduled", "post_urn": post, "auto_prepare": automatic}, 201


INGEST = "SELECT * FROM linkedin_live_ingest WHERE event=? AND opens=? AND company=?"


def start_replacement(con, key, destination):
    receipt = con.execute(RESTART, key).fetchone()
    if receipt:
        if receipt["replaces"] != destination:
            abort(409, "Another destination is already replacing this LinkedIn broadcast.")
        return receipt
    ingest = con.execute(INGEST, key).fetchone()
    if not ingest or ingest["destination"] != destination or not ingest["asset"]:
        abort(409, "The previous LinkedIn stream is unavailable.")
    if ingest["phase"] in ("registered", "bound", "closing"):
        return None
    if ingest["phase"] != "ended":
        abort(409, "Confirm the previous broadcast has ended on LinkedIn before starting another.")
    creation = con.execute(READ, key).fetchone()
    if not creation or creation["state"] != "scheduled" or not creation["video"]:
        abort(409, "The previous LinkedIn broadcast is unavailable.")
    con.execute("INSERT INTO linkedin_live_restart VALUES(?,?,?,?,?,?)",
                (destination, *key, creation["video"], ingest["asset"]))
    con.execute("DELETE FROM linkedin_live_ingest" + SCOPE, key)
    con.execute("DELETE FROM linkedin_live_creation" + SCOPE, key)
    set_auto(con, key, False)
    return con.execute(RESTART, key).fetchone()


def prepare_ingest(tx, check, send, save, key, revision, *, automatic=False):
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
    except (AutomaticIngestDeferred, TooManyRequests) as error:
        with tx() as con:
            deleted = con.execute(
                "DELETE FROM linkedin_live_ingest" + SCOPE + " AND phase='registering' AND asset IS NULL", key,
            ).rowcount
            if deleted and (automatic or isinstance(error, AutomaticIngestDeferred)):
                retry_at = (
                    0
                    if isinstance(error, AutomaticIngestDeferred)
                    else int(time()) + max(2, 60 if error.retry_after is None else error.retry_after)
                )
                con.execute(
                    "UPDATE linkedin_live_auto SET state='queued', retry_at=?" + SCOPE + " AND state='attempted'",
                    (retry_at, *key),
                )
        if not automatic and isinstance(error, TooManyRequests):
            raise
        return {"state": "pending"}, 202
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

    def __init__(self, app, tx, connections, outputs, lease, eligible, prepare):
        self.app = app
        self.tx = tx
        self.connections = connections
        self.outputs = outputs
        self.lease = lease
        self.eligible = eligible
        self.prepare = prepare
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
                with self.tx() as con:
                    upcoming = due_auto(con, int(time()))
                for row in upcoming:
                    if self.stopping.is_set():
                        return
                    with suppress(HTTPException):
                        self.prepare(row["event"], row["opens"], interactive=False, owner_key=row["company"])
            except Exception:
                self.app.logger.exception("CuePorch could not advance LinkedIn Live delivery")

    def advance(self, key):
        connection = self.connections[provider_for(key[2])]
        with self.tx() as con:
            row = con.execute(INGEST, key).fetchone()
            if not row or row["phase"] not in ("registered", "bound", "closing"):
                return
            saved = connection.connection(con)
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
                connection.end_asset(row["revision"], row["asset"])
                with self.tx() as con:
                    con.execute("UPDATE linkedin_live_ingest SET phase='ended'" + SCOPE, key)
            elif row["phase"] == "registered" and self.outputs.state(*key[:2], row["destination"]) == "sending":
                with self.tx() as con:
                    con.execute("UPDATE linkedin_live_ingest SET bind_until=COALESCE(bind_until,?)" + SCOPE,
                                (int(time()) + 90, *key))
                revision, video, asset = row["revision"], creation["video"], row["asset"]
                if not connection.asset_available(revision, asset):
                    return
                current = connection.bound_asset(revision, video)
                if current is None:
                    connection.bind_asset(revision, video, asset, lambda: self.check_binding(key))
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
