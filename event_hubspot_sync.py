from datetime import datetime
from zoneinfo import ZoneInfo


def next_due(opens: int, end: int, zone: str, now: int) -> int | None:
    cutoff = min(end, opens + 7200)

    tz = ZoneInfo(zone)
    local_open = datetime.fromtimestamp(opens, tz)
    midnight_local = local_open.replace(hour=0, minute=0, second=0, microsecond=0)

    candidates = (
        int(midnight_local.timestamp()),
        opens - 3600,
        opens + 900 * max(0, (now - opens) // 900 + 1),
    )

    return min(
        (candidate for candidate in candidates if now < candidate < cutoff),
        default=None,
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event_hubspot_sync (
    event          TEXT    NOT NULL,
    opens          INTEGER NOT NULL,
    generation     TEXT    NOT NULL,
    segment        TEXT    NOT NULL,
    connection     TEXT    NOT NULL,
    actor          TEXT    NOT NULL,
    event_revision TEXT    NOT NULL,
    form_revision  TEXT    NOT NULL,
    closes         INTEGER NOT NULL,
    zone           TEXT    NOT NULL,
    due            INTEGER,
    last_sync      INTEGER,
    last_result    TEXT,
    state          TEXT    NOT NULL,
    failures       INTEGER NOT NULL DEFAULT 0,
    problem        TEXT,
    PRIMARY KEY (event, opens)
);

CREATE INDEX IF NOT EXISTS live_event_hubspot_sync_due_idx
    ON live_event_hubspot_sync (due);
"""


def get(con, event, opens):
    cur = con.execute(
        "SELECT * FROM live_event_hubspot_sync WHERE event = ? AND opens = ?",
        (event, opens),
    )
    return cur.fetchone()


def remove(con, event, opens):
    cur = con.execute(
        "DELETE FROM live_event_hubspot_sync WHERE event = ? AND opens = ?",
        (event, opens),
    )
    return cur.rowcount


def set_sync(
    con,
    event,
    opens,
    *,
    segment,
    connection,
    actor,
    event_revision,
    form_revision,
    closes,
    zone,
    now,
):
    from uuid import uuid4

    generation = uuid4().hex
    con.execute(
        "INSERT OR REPLACE INTO live_event_hubspot_sync "
        "(event, opens, generation, segment, connection, actor, event_revision, form_revision, closes, zone, due, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event,
            opens,
            generation,
            segment,
            connection,
            actor,
            event_revision,
            form_revision,
            closes,
            zone,
            now,
            "scheduled",
        ),
    )
    return get(con, event, opens)


def receipt(row):
    import json

    if row is None:
        return {"enabled": False, "state": "off"}
    return {
        "enabled": row["due"] is not None,
        "segment": row["segment"],
        "state": row["state"],
        "next_at": row["due"],
        "last_at": row["last_sync"],
        "last_result": json.loads(row["last_result"])
        if row["last_result"] is not None
        else None,
        "problem": row["problem"],
    }


def due_job(con, now):
    return con.execute(
        "SELECT * FROM live_event_hubspot_sync WHERE due<=? ORDER BY due,event,opens LIMIT 1",
        (now,),
    ).fetchone()


def finish(con, job, now, result):
    import json

    due = next_due(job["opens"], job["closes"], job["zone"], now)
    state = "complete" if due is None else "scheduled"
    con.execute(
        "UPDATE live_event_hubspot_sync "
        "SET due=?,state=?,last_sync=?,last_result=?,failures=0,problem=NULL "
        "WHERE event=? AND opens=? AND generation=?",
        (
            due,
            state,
            now,
            json.dumps(result),
            job["event"],
            job["opens"],
            job["generation"],
        ),
    )


def fail(con, job, now, status, retry_after=None):
    failures = job["failures"] + 1

    problems = {
        401: "access denied",
        403: "access denied",
        409: "connection or occurrence/form changed",
        413: "too many contacts",
        422: "invalid form or contact data",
        429: "rate limit exceeded",
    }
    problem = problems.get(status, "provider unavailable")

    due = None
    state = "problem"

    base_delays = (15, 60, 300)
    if status in (429, 502, 503, 504) and failures <= len(base_delays):
        base = base_delays[failures - 1]
        hint = (
            retry_after
            if isinstance(retry_after, int) and not isinstance(retry_after, bool)
            else 0
        )
        candidate = now + max(hint, base)
        limit = min(job["closes"], job["opens"] + 7200)
        if candidate < limit:
            due = candidate
            state = "retry"

    con.execute(
        "UPDATE live_event_hubspot_sync "
        "SET due=?, state=?, failures=?, problem=? "
        "WHERE event=? AND opens=? AND generation=?",
        (due, state, failures, problem, job["event"], job["opens"], job["generation"]),
    )


def run_one(tx, connection, validate, apply, stop, clock):
    import hubspot_crm
    from werkzeug.exceptions import HTTPException

    now = int(clock())
    with tx() as con:
        job = due_job(con, now)
        if not job:
            return False
        cutoff = min(job["closes"], job["opens"] + 7200)
        if now >= cutoff:
            con.execute(
                "UPDATE live_event_hubspot_sync SET due=NULL, state='complete', problem=NULL "
                "WHERE event=? AND opens=? AND generation=?",
                (job["event"], job["opens"], job["generation"]),
            )
            return True

    try:
        with tx() as con:
            current = get(con, job["event"], job["opens"])
            if (
                stop.is_set()
                or not current
                or current["generation"] != job["generation"]
            ):
                return False
            connection.authorize(con, job["connection"])
            validate(con, job)
        if stop.is_set():
            return False

        contacts, skipped = hubspot_crm.contacts(
            connection, job["connection"], job["segment"]
        )

        with tx() as con:
            current = get(con, job["event"], job["opens"])
            if (
                stop.is_set()
                or not current
                or current["generation"] != job["generation"]
            ):
                return False

            now = int(clock())
            cutoff = min(job["closes"], job["opens"] + 7200)
            if now >= cutoff:
                con.execute(
                    "UPDATE live_event_hubspot_sync SET due=NULL, state='complete', problem=NULL "
                    "WHERE event=? AND opens=? AND generation=?",
                    (job["event"], job["opens"], job["generation"]),
                )
                return True

            connection.authorize(con, job["connection"])

            result = apply(con, job, contacts, skipped)
            finish(con, job, now, result)
            return True
    except HTTPException as exc:
        if not stop.is_set():
            with tx() as con:
                fail(
                    con,
                    job,
                    int(clock()),
                    exc.code,
                    retry_after=getattr(exc, "retry_after", None),
                )
        return True


def start(app, tx, database, connection, validate, apply, *, clock=None):
    from threading import Event, Thread
    from time import time
    from filelock import FileLock, Timeout

    if not connection.configured:
        return lambda: None

    stop = Event()
    if clock is None:
        clock = time

    def _run():
        while not stop.is_set():
            try:
                with FileLock(str(database) + ".hubspot-sync.lock", timeout=0):
                    if run_one(tx, connection, validate, apply, stop, clock):
                        continue
                stop.wait(1)
            except Timeout:
                stop.wait(1)
            except Exception:
                app.logger.error(
                    "The HubSpot webinar synchronization queue could not be processed."
                )
                stop.wait(1)

    worker = Thread(
        target=_run,
        name="CuePorch HubSpot webinar sync",
        daemon=True,
    )
    worker.start()

    def close():
        stop.set()
        worker.join(8)

    return close


def disconnect(con):
    con.execute(
        "UPDATE live_event_hubspot_sync SET due=NULL,state='problem',problem=?,generation=lower(hex(randomblob(16))) WHERE due IS NOT NULL",
        ("HubSpot was disconnected. Reconnect and enable synchronization again.",),
    )
