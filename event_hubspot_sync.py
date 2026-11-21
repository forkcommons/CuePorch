import json
from datetime import datetime
from zoneinfo import ZoneInfo


def local_midnight(opens, zone):
    dt = datetime.fromtimestamp(opens, ZoneInfo(zone))
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def next_due(opens: int, end: int, zone: str, now: int) -> int | None:
    limit = min(end, opens + 7200)
    candidates = (
        local_midnight(opens, zone),
        opens - 3600,
        opens + 900 * max(0, (now - opens) // 900 + 1),
    )
    valid = (time for time in candidates if now < time < limit)
    return min(valid, default=None)


SYNC_TABLES = {
    "hubspot": "live_event_hubspot_sync",
    "marketo": "live_event_marketo_sync",
}

SCHEMA = "\n".join(
    f"""
CREATE TABLE IF NOT EXISTS {table} (
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

CREATE INDEX IF NOT EXISTS {table}_due_idx
    ON {table} (due);
"""
    for table in SYNC_TABLES.values()
)


def get(con, event, opens, *, provider="hubspot"):
    table = SYNC_TABLES[provider]
    cur = con.execute(
        f"SELECT * FROM {table} WHERE event = ? AND opens = ?",
        (event, opens),
    )
    return cur.fetchone()


def remove(con, event, opens, *, provider="hubspot"):
    table = SYNC_TABLES[provider]
    cur = con.execute(
        f"DELETE FROM {table} WHERE event = ? AND opens = ?",
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
    first_due=None,
    provider="hubspot",
):
    from uuid import uuid4

    table = SYNC_TABLES[provider]
    generation = uuid4().hex
    due = now if first_due is None else first_due
    con.execute(
        f"INSERT OR REPLACE INTO {table} "
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
            due,
            "scheduled",
        ),
    )
    return get(con, event, opens, provider=provider)


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


def due_job(con, now, *, provider="hubspot"):
    table = SYNC_TABLES[provider]
    return con.execute(
        f"SELECT * FROM {table} WHERE due<=? ORDER BY due,event,opens LIMIT 1",
        (now,),
    ).fetchone()


def finish(con, job, now, result, *, provider="hubspot"):
    import json

    table = SYNC_TABLES[provider]
    due = next_due(job["opens"], job["closes"], job["zone"], now)
    state = "complete" if due is None else "scheduled"
    con.execute(
        f"UPDATE {table} "
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


def fail(con, job, now, status, retry_after=None, *, provider="hubspot"):
    table = SYNC_TABLES[provider]
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
        f"UPDATE {table} "
        "SET due=?, state=?, failures=?, problem=? "
        "WHERE event=? AND opens=? AND generation=?",
        (due, state, failures, problem, job["event"], job["opens"], job["generation"]),
    )


def complete(con, job, *, provider="hubspot"):
    table = SYNC_TABLES[provider]
    con.execute(
        f"UPDATE {table} "
        "SET due=NULL,problem=NULL,state=? "
        "WHERE event=? AND opens=? AND generation=?",
        ("complete", job["event"], job["opens"], job["generation"]),
    )


def run_one(
    tx,
    connection,
    validate,
    apply,
    stop,
    clock,
    *,
    provider="hubspot",
    fetch=None,
    authorize=None,
):
    from werkzeug.exceptions import HTTPException

    if fetch is None:
        import hubspot_crm

    auth = authorize if authorize is not None else connection.authorize

    now = int(clock())
    with tx() as con:
        job = due_job(con, now, provider=provider)
        if not job:
            return False
        cutoff = min(job["closes"], job["opens"] + 7200)
        if now >= cutoff:
            complete(con, job, provider=provider)
            return True

    try:
        with tx() as con:
            current = get(con, job["event"], job["opens"], provider=provider)
            if (
                stop.is_set()
                or not current
                or current["generation"] != job["generation"]
            ):
                return False
            auth(con, job["connection"])
            validate(con, job)
        if stop.is_set():
            return False

        if fetch is not None:
            fetched = fetch(job)
        else:
            fetched = hubspot_crm.contacts(
                connection, job["connection"], job["segment"], include_membership=True
            )

        with tx() as con:
            current = get(con, job["event"], job["opens"], provider=provider)
            if (
                stop.is_set()
                or not current
                or current["generation"] != job["generation"]
            ):
                return False

            now = int(clock())
            cutoff = min(job["closes"], job["opens"] + 7200)
            if now >= cutoff:
                complete(con, job, provider=provider)
                return True

            auth(con, job["connection"])

            result = apply(con, job, *fetched)
            finish(con, job, now, result, provider=provider)
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
                    provider=provider,
                )
        return True


def start(
    app,
    tx,
    database,
    connection,
    validate,
    apply,
    *,
    clock=None,
    provider="hubspot",
    fetch=None,
    authorize=None,
):
    from threading import Event, Thread
    from time import time
    from filelock import FileLock, Timeout

    if connection is None:
        return lambda: None
    if provider == "hubspot" and not connection.configured:
        return lambda: None

    stop = Event()
    if clock is None:
        clock = time

    label = "HubSpot" if provider == "hubspot" else provider.title()

    def _run():
        while not stop.is_set():
            try:
                with FileLock(f"{database}.{provider}-sync.lock", timeout=0):
                    if run_one(
                        tx,
                        connection,
                        validate,
                        apply,
                        stop,
                        clock,
                        provider=provider,
                        fetch=fetch,
                        authorize=authorize,
                    ):
                        continue
                stop.wait(1)
            except Timeout:
                stop.wait(1)
            except Exception:
                app.logger.error(
                    f"The {label} webinar synchronization queue could not be processed."
                )
                stop.wait(1)

    worker = Thread(
        target=_run,
        name=f"CuePorch {label} webinar sync",
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


SCHEMA += """
CREATE TABLE IF NOT EXISTS live_event_hubspot_member (
    registrant TEXT PRIMARY KEY,
    link TEXT UNIQUE NOT NULL,
    event TEXT NOT NULL,
    opens INTEGER NOT NULL,
    connection TEXT NOT NULL,
    segment TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    removed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(event, opens, connection, segment, contact_id)
);
CREATE VIEW IF NOT EXISTS live_event_active_registrant AS
SELECT r.* FROM live_event_registrant r
WHERE NOT EXISTS (
    SELECT 1 FROM live_event_hubspot_member m
    WHERE m.registrant = r.id AND m.removed = 1
);
"""


def track_member(con, job, contact_id, registrant, link):
    con.execute(
        "INSERT OR IGNORE INTO live_event_hubspot_member "
        "(registrant, link, event, opens, connection, segment, contact_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            registrant,
            link,
            job["event"],
            job["opens"],
            job["connection"],
            job["segment"],
            contact_id,
        ),
    )


def reconcile_members(con, job, ids):
    j = json.dumps(ids)
    removed = con.execute(
        "UPDATE live_event_hubspot_member SET removed=1 "
        "WHERE removed=0 AND event=? AND opens=? AND connection=? AND segment=? "
        "AND contact_id NOT IN (SELECT value FROM json_each(?))",
        (job["event"], job["opens"], job["connection"], job["segment"], j),
    ).rowcount
    restored = con.execute(
        "UPDATE live_event_hubspot_member SET removed=0 "
        "WHERE removed=1 AND event=? AND opens=? AND connection=? AND segment=? "
        "AND contact_id IN (SELECT value FROM json_each(?))",
        (job["event"], job["opens"], job["connection"], job["segment"], j),
    ).rowcount
    return removed, restored
