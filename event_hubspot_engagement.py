"""Event HubSpot engagement — DB helpers."""

import re
from uuid import uuid4

STEP = "event_engagement_sync"

_NAME = re.compile(r"pe[1-9][0-9]*_[a-z][a-z0-9_]*")
_NAME_MAX = 128

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event_hubspot_engagement_plan (
    generation TEXT PRIMARY KEY,
    event_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_event_hubspot_engagement_job (
    id         TEXT    PRIMARY KEY,
    generation TEXT    NOT NULL,
    event      TEXT    NOT NULL,
    opens      INTEGER NOT NULL,
    registrant TEXT    NOT NULL,
    sharing    TEXT    NOT NULL,
    due        INTEGER,
    state      TEXT    NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    problem    TEXT,
    snapshot   TEXT,
    contact_id TEXT,
    UNIQUE (generation, registrant)
);

CREATE INDEX IF NOT EXISTS live_event_hubspot_engagement_job_state_due_idx
    ON live_event_hubspot_engagement_job (state, due);
"""


def name_valid(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= _NAME_MAX
        and bool(_NAME.fullmatch(value))
    )


def configure(con, generation, event_name):
    if not name_valid(event_name):
        raise ValueError("invalid event_name")
    con.execute(
        "INSERT INTO live_event_hubspot_engagement_plan "
        "(generation, event_name) VALUES (?, ?)",
        (generation, event_name),
    )


def definition(con, generation):
    row = con.execute(
        "SELECT event_name FROM live_event_hubspot_engagement_plan WHERE generation=?",
        (generation,),
    ).fetchone()
    return row["event_name"] if row else None


def enqueue(con, action, plan, registrant, sharing):
    job_id = str(uuid4())
    due = plan["deadline"] + 3600
    cur = con.execute(
        "INSERT OR IGNORE INTO live_event_hubspot_engagement_job "
        "(id, generation, event, opens, registrant, sharing, due, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            plan["generation"],
            plan["event"],
            plan["opens"],
            registrant,
            sharing,
            due,
            "queued",
        ),
    )
    if cur.rowcount:
        action(
            con,
            job_id,
            STEP,
            {
                "event": plan["event"],
                "occurrence": plan["opens"],
                "registrant": registrant,
            },
        )
        return job_id
    return None


def cancel(con, event=None, opens=None, *, sharing=None):
    if (event is None) != (opens is None):
        raise ValueError("event and opens must be supplied together")
    if sharing is not None and (event is not None or opens is not None):
        raise ValueError("sharing cannot be combined with event/opens")

    cur = con.cursor()

    if sharing is not None:
        where = "sharing = ?"
        params = (sharing,)
        delete_plan = None
    elif event is not None and opens is not None:
        where = "event = ? AND opens = ?"
        params = (event, opens)
        delete_plan = "occurrence"
    else:
        where = "1 = 1"
        params = ()
        delete_plan = "all"

    cur.execute(
        f"""UPDATE action
            SET status = 'rejected', decision = 'canceled'
            WHERE status = 'waiting'
              AND id IN (
                  SELECT id FROM live_event_hubspot_engagement_job
                  WHERE state = 'queued' AND {where}
              )""",
        params,
    )
    cur.execute(
        f"""UPDATE live_event_hubspot_engagement_job
            SET state = 'canceled', due = NULL, snapshot = NULL, contact_id = NULL
            WHERE state = 'queued' AND {where}""",
        params,
    )

    if delete_plan == "occurrence":
        cur.execute(
            """DELETE FROM live_event_hubspot_engagement_plan
               WHERE generation IN (
                   SELECT generation FROM live_event_hubspot_outbound
                   WHERE event = ? AND opens = ?
               )""",
            (event, opens),
        )
    elif delete_plan == "all":
        cur.execute("DELETE FROM live_event_hubspot_engagement_plan")


def status(con, generation):
    states = {}
    for row in con.execute(
        "SELECT CASE WHEN j.state = 'queued' AND a.status = 'waiting' "
        "THEN 'waiting' ELSE j.state END AS status, COUNT(*) AS n "
        "FROM live_event_hubspot_engagement_job j "
        "LEFT JOIN action a ON a.id = j.id "
        "WHERE j.generation = ? "
        "GROUP BY 1",
        (generation,),
    ):
        states[row["status"]] = row["n"]
    return states


def scope(con, job, connection, validate, stop):
    from flask import abort
    import event_hubspot_outbound

    if stop.is_set():
        abort(423, "Shutting down.")

    row = con.execute(
        """SELECT j.state AS state, a.status AS status
           FROM live_event_hubspot_engagement_job j
           LEFT JOIN action a ON a.id = j.id
           WHERE j.id = ?""",
        (job["id"],),
    ).fetchone()
    if row is None or row["state"] != "queued":
        abort(409, "Job is not queued.")
    action_status = row["status"]
    if action_status == "waiting":
        abort(423, "Action is still waiting.")
    if action_status != "eligible":
        abort(409, "Action is missing or not eligible.")

    plan = event_hubspot_outbound.get(con, job["event"], job["opens"])
    if not plan or plan["generation"] != job["generation"]:
        abort(409, "Outbound plan does not match job generation.")

    event_name = definition(con, job["generation"])
    if not name_valid(event_name):
        abort(409, "Event name is missing or invalid.")

    attendee = con.execute(
        """SELECT a.email AS email, a.registered AS registered
           FROM live_attendee_sharing s
           JOIN live_event_active_registrant a ON a.id = s.registrant
           WHERE s.id = ? AND s.registrant = ? AND s.revoked = 0
             AND a.event = ? AND a.opens = ?""",
        (job["sharing"], job["registrant"], job["event"], job["opens"]),
    ).fetchone()
    if attendee is None:
        abort(409, "Sharing record is missing, revoked, or mismatched.")

    connection.authorize(con, plan["connection"])
    title = validate(con, plan)
    return plan, event_name, attendee, title


def payload(con, job, plan, attendee, title):
    from datetime import datetime, timezone

    registered = attendee["registered"]
    if registered is None or not isinstance(registered, (int, float)):
        return None

    row = con.execute(
        """SELECT COUNT(*) AS count,
                  COALESCE(SUM(CASE WHEN p.phase = 'live' THEN p.watched ELSE 0 END), 0) AS live,
                  COALESCE(SUM(CASE WHEN p.phase = 'replay' THEN p.watched ELSE 0 END), 0) AS replay
           FROM live_attendee_session s
           JOIN live_playback p ON p.id = s.session
           WHERE s.sharing = ?
             AND p.event = ? AND p.opens = ?
             AND p.played = 1
             AND p.phase IN ('live', 'replay')""",
        (job["sharing"], job["event"], job["opens"]),
    ).fetchone()
    if row["count"] == 0:
        return None

    occurred_at = (
        datetime.fromtimestamp(plan["deadline"], timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    registered_at = (
        datetime.fromtimestamp(registered, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    properties = {
        "cueporch_event_title": title,
        "cueporch_registered_at": registered_at,
        "cueporch_live_ms": str(row["live"]),
        "cueporch_replay_ms": str(row["replay"]),
    }
    return occurred_at, properties


def deliver(tx, job, connection, validate, stop):
    import json
    from flask import abort
    import hubspot_crm

    with tx() as con:
        plan, event_name, attendee, title = scope(con, job, connection, validate, stop)

    revision = plan["connection"]
    email = attendee["email"]
    registered = attendee["registered"]

    if hubspot_crm.opted_out(
        connection, revision, email, plan["subscription"], plan["business_unit"]
    ):
        return "skipped"

    contact = hubspot_crm.contact_record(connection, revision, email)
    if contact is None:
        return "skipped"

    def check(con):
        plan2, event_name2, attendee2, title2 = scope(
            con, job, connection, validate, stop
        )
        if (
            event_name2 != event_name
            or attendee2["email"] != email
            or attendee2["registered"] != registered
            or title2 != title
        ):
            abort(409)
        return plan2, event_name2, attendee2, title2

    with tx() as con:
        plan2, event_name2, attendee2, title2 = check(con)

        row = con.execute(
            "SELECT snapshot, contact_id FROM live_event_hubspot_engagement_job WHERE id = ?",
            (job["id"],),
        ).fetchone()

        snap = row["snapshot"]
        cid = row["contact_id"]

        if snap is not None:
            if cid != contact:
                return "skipped"
            iso_end, properties = json.loads(snap)
        else:
            p = payload(con, job, plan2, attendee2, title2)
            if p is None:
                return "skipped"
            iso_end, properties = p
            con.execute(
                "UPDATE live_event_hubspot_engagement_job SET snapshot = ?, contact_id = ? WHERE id = ?",
                (json.dumps([iso_end, properties]), contact, job["id"]),
            )

    def guard():
        with tx() as con:
            check(con)

    hubspot_crm.send_engagement(
        connection,
        revision,
        event_name,
        contact,
        iso_end,
        job["id"],
        properties,
        before_write=guard,
    )
    return "accepted"


def run_one(tx, connection, validate, stop, clock):
    from werkzeug.exceptions import HTTPException

    if stop.is_set():
        return False

    with tx() as con:
        job = con.execute(
            "SELECT j.* FROM live_event_hubspot_engagement_job j "
            "JOIN action a ON a.id = j.id "
            "WHERE j.state = 'queued' AND j.due <= ? "
            "AND a.status IN ('eligible','rejected') "
            "ORDER BY j.due, j.id LIMIT 1",
            (clock(),),
        ).fetchone()
        if job is None:
            return False

    if stop.is_set():
        return False

    due = None
    problem = None
    attempts = job["attempts"] + 1

    try:
        status = deliver(tx, job, connection, validate, stop)
    except HTTPException as e:
        if e.code == 423 or stop.is_set():
            return False
        if e.code in (404, 409):
            state = "canceled"
        else:
            state = "problem"
            problem = "Delivery failed ({0}).".format(e.code)
            if e.code in (429, 502, 503, 504) and attempts <= 4:
                retry_after = getattr(e, "retry_after", None)
                delay = max(300, retry_after) if isinstance(retry_after, int) else 300
                candidate_due = clock() + delay
                if candidate_due < job["opens"] + 31 * 86400:
                    state = "queued"
                    due = candidate_due
                    problem = None
    else:
        state = status

    with tx() as con:
        con.execute(
            "UPDATE live_event_hubspot_engagement_job "
            "SET state = ?, due = ?, attempts = ?, problem = ?, "
            "snapshot = CASE WHEN ? = 'queued' THEN snapshot ELSE NULL END, "
            "contact_id = CASE WHEN ? = 'queued' THEN contact_id ELSE NULL END "
            "WHERE id = ? AND state = 'queued'",
            (state, due, attempts, problem, state, state, job["id"]),
        )
        return True
