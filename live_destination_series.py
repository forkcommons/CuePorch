"""Schedule a separate provider broadcast for each recurring occurrence."""


from contextlib import suppress
from datetime import datetime, timedelta, timezone
import json
from threading import Event, Thread

from flask import abort
from werkzeug.exceptions import HTTPException

import event_schedule

SCAN_INTERVAL = 60


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_destination_series (
    event TEXT NOT NULL,
    provider TEXT NOT NULL,
    owner TEXT NOT NULL,
    revision TEXT NOT NULL,
    event_revision TEXT NOT NULL,
    after_opens INTEGER NOT NULL,
    options TEXT NOT NULL,
    PRIMARY KEY (event, provider, owner)
);
CREATE TABLE IF NOT EXISTS live_destination_series_skip (
    event TEXT NOT NULL,
    provider TEXT NOT NULL,
    owner TEXT NOT NULL,
    opens INTEGER NOT NULL,
    PRIMARY KEY (event, provider, owner, opens)
);
"""

ENABLED_QUERY = """
SELECT 1
FROM live_destination_series AS s
WHERE s.event = ?
  AND s.provider = ?
  AND s.owner = ?
  AND s.revision = ?
  AND s.event_revision = ?
  AND s.after_opens = ?
  AND s.options IS ?
  AND ? > s.after_opens
  AND NOT EXISTS (
      SELECT 1
      FROM live_destination_series_skip AS k
      WHERE k.event = s.event
        AND k.provider = s.provider
        AND k.owner = s.owner
        AND k.opens = ?
  )
LIMIT 1
"""


def enabled(con, row, opens):
    cur = con.execute(
        ENABLED_QUERY,
        (
            row["event"],
            row["provider"],
            row["owner"],
            row["revision"],
            row["event_revision"],
            row["after_opens"],
            row["options"],
            opens,
            opens,
        ),
    )
    return cur.fetchone() is not None


def ensure_recurring(con, event):
    row = con.execute(
        "SELECT settings FROM live_event WHERE id = ?", (event,)
    ).fetchone()
    if row is None or json.loads(row["settings"])["schedule"]["repeat"] == "none":
        abort(409, "Choose a recurring event to configure future dates.")


def save(con, event, provider, owner, revision, event_revision, opens, options):
    con.execute(
        """insert into live_destination_series
             (event, provider, owner, revision, event_revision, after_opens, options)
           values (?, ?, ?, ?, ?, ?, ?)
           on conflict(event, provider, owner) do update set
             revision = excluded.revision,
             event_revision = excluded.event_revision,
             after_opens = excluded.after_opens,
             options = excluded.options""",
        (event, provider, owner, revision, event_revision, opens, json.dumps(options)),
    )


def due(con, now):
    rows = con.execute("""
        SELECT s.*, e.settings FROM live_destination_series s
        JOIN live_event e ON e.id = s.event AND e.revision = s.event_revision
        JOIN workspace_integration_connection w
          ON w.provider = s.provider AND w.revision = s.revision
    """).fetchall()
    end = now + timedelta(hours=1)
    cutoff = now.timestamp()
    for row in rows:
        sched = json.loads(row['settings'])['schedule']
        for slot in event_schedule.occurrences(sched, now, end):
            opens = slot['id']
            if not (cutoff < opens and opens > row['after_opens']):
                continue
            q = "SELECT 1 FROM live_destination_series_skip WHERE event=? AND provider=? AND owner=? AND opens=?"
            if con.execute(q, (row['event'], row['provider'], row['owner'], opens)).fetchone():
                continue
            yield row, opens


def start(app, tx, prepare):
    stopping = Event()

    def run():
        while not stopping.wait(SCAN_INTERVAL):
            try:
                with app.app_context():
                    now = datetime.now(timezone.utc)
                    with tx() as con:
                        rows = list(due(con, now))
                    for row, opens in rows:
                        if stopping.is_set():
                            return
                        with suppress(HTTPException):
                            prepare(row, opens)
            except Exception:
                app.logger.exception("CuePorch could not advance automatic recurring simulcast delivery")

    thread = Thread(target=run, name="CuePorch recurring simulcast", daemon=True)
    thread.start()

    def stop():
        stopping.set()
        thread.join(timeout=4)

    return stop
