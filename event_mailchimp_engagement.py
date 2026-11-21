import json
import math
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event_mailchimp_engagement (
    id TEXT PRIMARY KEY NOT NULL,
    event TEXT NOT NULL,
    opens INTEGER NOT NULL,
    generation TEXT NOT NULL,
    registrant TEXT NOT NULL,
    sharing TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    due INTEGER DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    write_started INTEGER NOT NULL DEFAULT 0,
    snapshot TEXT,
    UNIQUE(generation, registrant)
);
CREATE INDEX IF NOT EXISTS idx_live_event_mailchimp_engagement_sharing
    ON live_event_mailchimp_engagement(sharing);
CREATE INDEX IF NOT EXISTS idx_live_event_mailchimp_engagement_state_due
    ON live_event_mailchimp_engagement(state, due);
CREATE TABLE IF NOT EXISTS live_event_mailchimp_completion (
    generation TEXT PRIMARY KEY NOT NULL,
    completed REAL NOT NULL,
    duration REAL NOT NULL
);
"""


def enqueue(con, action, plan, registrant, sharing):
    row_id = uuid4().hex
    generation = plan["generation"]
    cur = con.execute(
        """
        INSERT OR IGNORE INTO live_event_mailchimp_engagement
            (id, event, opens, generation, registrant, sharing)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (row_id, plan["event"], plan["opens"], generation, registrant, sharing),
    )
    inserted = cur.rowcount > 0
    if inserted:
        action(
            con,
            row_id,
            "event_engagement_sync",
            {
                "provider": "mailchimp",
                "event": plan["event"],
                "occurrence": plan["opens"],
                "registrant": registrant,
            },
        )
    return bool(inserted)


def complete(con, generation, completed, duration):
    try:
        if not (math.isfinite(completed) and math.isfinite(duration)):
            return False
        if completed <= 0 or duration <= 0:
            return False
    except (TypeError, ValueError):
        return False

    cur = con.execute(
        """
        INSERT OR IGNORE INTO live_event_mailchimp_completion
            (generation, completed, duration)
        VALUES (?, ?, ?)
        """,
        (generation, completed, duration),
    )
    return bool(cur.rowcount > 0)


def cancel(con, *, generation=None, sharing=None):
    if (generation is None) == (sharing is None):
        raise ValueError("Choose one Mailchimp delivery scope.")
    column, value = (
        ("sharing", sharing) if generation is None else ("generation", generation)
    )
    con.execute(
        f"""UPDATE action SET status='rejected', decision='canceled'
            WHERE status IN ('eligible', 'waiting') AND id IN (
                SELECT id FROM live_event_mailchimp_engagement
                WHERE {column}=? AND state='queued'
            )""",
        (value,),
    )
    con.execute(
        f"""UPDATE live_event_mailchimp_engagement SET state='canceled', due=NULL, snapshot=NULL
            WHERE {column}=? AND state='queued'""",
        (value,),
    )


def next_job(con, now):
    return con.execute(
        """
        SELECT j.*, c.completed, c.duration
        FROM live_event_mailchimp_engagement j
        JOIN live_event_mailchimp_completion c ON c.generation = j.generation
        JOIN action a ON a.id = j.id
        WHERE j.state = 'queued'
          AND j.due <= ?
          AND a.status IN ('eligible', 'rejected')
          AND (
              a.status = 'rejected'
              OR NOT EXISTS (
                  SELECT 1
                  FROM live_event_contact_delivery d
                  WHERE d.provider = 'mailchimp'
                    AND d.generation = j.generation
                    AND d.registrant = j.registrant
                    AND d.state = 'queued'
              )
          )
        ORDER BY j.due, j.id
        LIMIT 1
    """,
        (now,),
    ).fetchone()


def status(con, generation):
    rows = con.execute(
        """
        SELECT CASE
                   WHEN j.state = 'queued' AND a.status = 'waiting' THEN 'waiting'
                   ELSE j.state
               END AS label,
               COUNT(*) AS count
        FROM live_event_mailchimp_engagement j
        LEFT JOIN action a ON a.id = j.id
        WHERE j.generation = ?
        GROUP BY label
        ORDER BY label
        """,
        (generation,),
    ).fetchall()
    return {label: count for label, count in rows}


def scope(con, job, connection, validate, stop):
    from flask import abort

    from event_contact_delivery import get

    if stop.is_set():
        abort(423)

    row = con.execute(
        "SELECT j.write_started, j.snapshot, a.status "
        "FROM live_event_mailchimp_engagement j "
        "JOIN action a ON a.id = j.id "
        "WHERE j.id = ? AND j.state = 'queued'",
        (job["id"],),
    ).fetchone()

    if row is None:
        abort(409)

    if row["status"] == "waiting":
        abort(423)

    if row["status"] != "eligible":
        abort(409)

    plan = get(con, "mailchimp", job["event"], job["opens"])
    if not plan:
        abort(409)

    if plan["generation"] != job["generation"]:
        abort(409)

    connection.authorize(con, plan["provider_revision"])

    dest = json.loads(plan["destination"])
    if dest.get("engagement") is not True:
        abort(409)

    title = validate(con, plan)

    hit = con.execute(
        "SELECT r.email, r.registered "
        "FROM live_attendee_sharing s "
        "JOIN live_event_active_registrant r ON r.id = s.registrant "
        "JOIN live_event_contact_delivery d "
        "  ON d.registrant = r.id "
        " AND d.event = r.event "
        " AND d.opens = r.opens "
        "WHERE s.id = ? "
        "  AND r.id = ? "
        "  AND s.revoked = 0 "
        "  AND r.event = ? "
        "  AND r.opens = ? "
        "  AND d.provider = 'mailchimp' "
        "  AND d.generation = ? "
        "  AND d.state = 'sent'",
        (
            job["sharing"],
            job["registrant"],
            job["event"],
            job["opens"],
            job["generation"],
        ),
    ).fetchone()

    if hit is None:
        abort(409)

    return (plan["provider_revision"], dest["audience"], hit, title, row)


def payload(con, job, attendee, title):
    from datetime import UTC, datetime

    from event_engagement import activity_snapshot

    snap = activity_snapshot(con, job, job["completed"])
    ts = datetime.fromtimestamp(job["completed"], UTC).isoformat()
    dur_ms = job["duration"] * 1000
    pct = (snap["live_ms"] / dur_ms * 100) if dur_ms else 0.0
    pct = max(0.0, min(100.0, pct))
    reg = attendee["registered"]
    registered_at = (
        datetime.fromtimestamp(reg, UTC).isoformat() if reg is not None else ""
    )
    props = {
        "event_title": str(title),
        "event_id": str(job["event"]),
        "occurrence_id": str(job["opens"]),
        "attended_live": "true" if snap["attended_live"] else "false",
        "live_watch_ms": str(snap["live_ms"]),
        "replay_watch_ms": str(snap["replay_ms"]),
        "live_watch_percent": str(round(pct)),
        "questions": "\n".join(snap["questions"])[:1000],
        "question_upvotes": "\n".join(snap["upvotes"])[:1000],
        "polls": "\n".join(snap["polls"])[:1000],
        "registered_at": registered_at,
    }
    return ts, props


def deliver(tx, job, connection, validate, stop):
    from flask import abort
    from werkzeug.exceptions import TooManyRequests

    import mailchimp_crm

    with tx() as con:
        revision, list_id, attendee, title, state = scope(
            con, job, connection, validate, stop
        )
        email = attendee["email"]
        started = bool(state["write_started"])
        if not started:
            snapshot = state["snapshot"]
            if snapshot is not None:
                occurred_at, properties = json.loads(snapshot)
            else:
                occurred_at, properties = payload(con, job, attendee, title)
            con.execute(
                "UPDATE live_event_mailchimp_engagement SET snapshot=? WHERE id=?",
                (json.dumps([occurred_at, properties]), job["id"]),
            )

    if started:
        if mailchimp_crm.member_event_seen(
            connection, revision, list_id, email, job["id"]
        ):
            return "sent"
        abort(502)

    member = mailchimp_crm.read_member(connection, revision, list_id, email) or {}
    status = member.get("status")
    if status == "pending":
        raise TooManyRequests(retry_after=300)
    if status != "subscribed":
        return "skipped"

    def before_write():
        with tx() as con:
            current_revision, current_list, current_attendee, _, _ = scope(
                con, job, connection, validate, stop
            )
            if (
                current_revision != revision
                or current_list != list_id
                or current_attendee["email"] != email
            ):
                abort(409)
            cur = con.execute(
                "UPDATE live_event_mailchimp_engagement SET write_started=1 "
                "WHERE id=? AND state='queued' AND write_started=0",
                (job["id"],),
            )
            if cur.rowcount != 1:
                abort(409)

    try:
        mailchimp_crm.add_member_event(
            connection,
            revision,
            list_id,
            email,
            job["id"],
            occurred_at,
            properties,
            before_write=before_write,
        )
    except TooManyRequests:
        with tx() as con:
            con.execute(
                "UPDATE live_event_mailchimp_engagement SET write_started=0 "
                "WHERE id=? AND state='queued'",
                (job["id"],),
            )
        raise

    return "sent"


def run_one(tx, connection, validate, stop, clock):
    from werkzeug.exceptions import HTTPException

    if stop.is_set():
        return False
    with tx() as con:
        job = next_job(con, int(clock()))
    if job is None:
        return False
    attempts = job["attempts"] + 1
    try:
        state = deliver(tx, job, connection, validate, stop)
        due = None
    except HTTPException as exc:
        code = exc.code
        if code == 423:
            return False
        elif code == 429 and attempts < 5:
            state = "queued"
            due = int(clock()) + max(15, getattr(exc, "retry_after", None) or 0)
        elif code in (502, 503, 504) and attempts < 5:
            state = "queued"
            due = int(clock()) + 20 * attempts
        elif code in (502, 503, 504):
            state = "unconfirmed"
            due = None
        elif code in (404, 409):
            state = "canceled"
            due = None
        else:
            state = "problem"
            due = None
    except (ValueError, KeyError):
        state = "problem"
        due = None

    with tx() as con:
        con.execute(
            "UPDATE live_event_mailchimp_engagement "
            "SET state=?, due=?, attempts=?, "
            "snapshot=CASE WHEN ?='queued' THEN snapshot ELSE NULL END "
            "WHERE id=? AND state='queued'",
            (state, due, attempts, state, job["id"]),
        )
    return True
