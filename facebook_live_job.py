"""Persist Facebook scheduling outcomes without repeating uncertain writes."""

from contextlib import suppress
from threading import Event, Thread
from time import time

from flask import abort
from werkzeug.exceptions import HTTPException

from facebook_live import FacebookLiveUnconfirmed


SCHEMA = """
CREATE TABLE IF NOT EXISTS facebook_live_creation(
 event TEXT NOT NULL, opens INTEGER NOT NULL, page TEXT NOT NULL, revision TEXT NOT NULL,
 state TEXT NOT NULL, destination TEXT, PRIMARY KEY(event,opens,page));
CREATE TABLE IF NOT EXISTS facebook_live_auto(
 event TEXT NOT NULL, opens INTEGER NOT NULL, page TEXT NOT NULL, revision TEXT NOT NULL,
 event_revision TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(event,opens,page));
CREATE TABLE IF NOT EXISTS facebook_live_series(
 event TEXT NOT NULL, page TEXT NOT NULL, revision TEXT NOT NULL, event_revision TEXT NOT NULL,
 after_opens INTEGER NOT NULL, PRIMARY KEY(event,page));
"""


def queue(con, key, revision, event_revision, *, state="queued"):
    con.execute("""INSERT INTO facebook_live_auto VALUES(?,?,?,?,?,?)
        ON CONFLICT(event,opens,page) DO UPDATE SET revision=excluded.revision,
        event_revision=excluded.event_revision,state=excluded.state""", (*key, revision, event_revision, state))


def due(con, now):
    return con.execute("""SELECT a.*,0 AS series FROM facebook_live_auto a
        JOIN live_event e ON e.id=a.event AND e.revision=a.event_revision
        JOIN live_occurrence o ON o.event=a.event AND o.opens=a.opens AND o.delivery='ready'
        JOIN workspace_integration_connection w ON w.provider='facebook-live' AND w.revision=a.revision
        LEFT JOIN facebook_live_creation c ON c.event=a.event AND c.opens=a.opens AND c.page=a.page
        WHERE a.state='queued' AND a.opens<=? AND c.event IS NULL
        UNION ALL
        SELECT s.event,o.opens,s.page,s.revision,s.event_revision,'queued',1
        FROM facebook_live_series s
        JOIN live_event e ON e.id=s.event AND e.revision=s.event_revision
        JOIN live_occurrence o ON o.event=s.event AND o.delivery='ready' AND o.opens>s.after_opens
        JOIN workspace_integration_connection w ON w.provider='facebook-live' AND w.revision=s.revision
        LEFT JOIN facebook_live_auto a ON a.event=s.event AND a.opens=o.opens AND a.page=s.page
        LEFT JOIN facebook_live_creation c ON c.event=s.event AND c.opens=o.opens AND c.page=s.page
        WHERE o.opens<=? AND a.event IS NULL AND c.event IS NULL ORDER BY opens""", (now, now)).fetchall()


def claim(con, key, automatic):
    if automatic["series"]:
        return con.execute("""INSERT OR IGNORE INTO facebook_live_auto VALUES(?,?,?,?,?,'attempted')""",
                           (*key, automatic["revision"], automatic["event_revision"])).rowcount == 1
    return con.execute("""UPDATE facebook_live_auto SET state='attempted'
        WHERE event=? AND opens=? AND page=? AND state='queued'""", key).rowcount == 1


def start(app, tx, prepare):
    stopping = Event()

    def run():
        while not stopping.wait(2):
            try:
                with tx() as con:
                    rows = due(con, int(time()))
                for row in rows:
                    if stopping.is_set():
                        return
                    with suppress(HTTPException):
                        prepare(row["event"], row["opens"], row["page"], automatic=row)
            except Exception:
                app.logger.exception("CuePorch could not advance automatic Facebook Live delivery")

    thread = Thread(target=run, name="CuePorch Facebook Live", daemon=True)
    thread.start()

    def stop():
        stopping.set()
        thread.join(timeout=4)

    return stop


def provision(tx, check, create, save, event, opens, page, revision, *, claim_auto=None):
    key = (event, opens, page)
    with tx() as con:
        check(con)
        prior = con.execute("SELECT * FROM facebook_live_creation WHERE event=? AND opens=? AND page=?", key).fetchone()
        if prior:
            if prior["revision"] != revision:
                abort(409, "This broadcast was prepared using another Facebook connection.")
            if prior["state"] != "prepared":
                return {"state": "unconfirmed"}, 202
            destination = prior["destination"]
            if not con.execute("SELECT 1 FROM live_simulcast_destination WHERE id=?", (destination,)).fetchone():
                abort(409, "This broadcast was removed. Check its status in Facebook Live Producer.")
            return {"state": "prepared", "id": destination}, 200
        if claim_auto is not None and not claim_auto(con):
            return {"state": "canceled"}, 200
        con.execute("INSERT INTO facebook_live_creation VALUES(?,?,?,?,'sending',NULL)", (*key, revision))

    try:
        target = create()
    except FacebookLiveUnconfirmed:
        with tx() as con:
            con.execute("UPDATE facebook_live_creation SET state='unconfirmed' WHERE event=? AND opens=? AND page=?", key)
        return {"state": "unconfirmed"}, 202
    except HTTPException as error:
        if error.code is not None and 400 <= error.code < 500:
            with tx() as con:
                con.execute("DELETE FROM facebook_live_creation WHERE event=? AND opens=? AND page=?", key)
        raise

    with tx() as con:
        check(con)
        destination = save(con, target)
        con.execute("UPDATE facebook_live_creation SET state='prepared',destination=? WHERE event=? AND opens=? AND page=?",
                    (destination, *key))
    return {"state": "prepared", "id": destination}, 201
