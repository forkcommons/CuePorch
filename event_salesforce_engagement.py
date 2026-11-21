import json
import math
from uuid import uuid4

from flask import abort

import event_contact_delivery as outbox
import salesforce_outbound


STEP = "event_engagement_sync"
PROVIDER = "salesforce"
BATCH_SECONDS = 300
WINDOW_SECONDS = 1500
AUTO_DELAY = 3600


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event_salesforce_engagement (
    id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    opens INTEGER NOT NULL,
    generation TEXT NOT NULL,
    registrant TEXT NOT NULL,
    sharing TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    due INTEGER DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE(generation, registrant)
);
CREATE INDEX IF NOT EXISTS live_event_salesforce_engagement_sharing_idx
    ON live_event_salesforce_engagement(sharing);
CREATE INDEX IF NOT EXISTS live_event_salesforce_engagement_state_due_idx
    ON live_event_salesforce_engagement(state, due);
CREATE TABLE IF NOT EXISTS live_event_salesforce_completion (
    generation TEXT PRIMARY KEY,
    completed REAL NOT NULL,
    duration REAL NOT NULL,
    report_at REAL NOT NULL
);
"""


def enqueue(con, action, plan, registrant, sharing):
    row_id = uuid4().hex
    generation = plan["generation"]
    cur = con.execute(
        """
        INSERT OR IGNORE INTO live_event_salesforce_engagement
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
            STEP,
            {
                "provider": PROVIDER,
                "event": plan["event"],
                "occurrence": plan["opens"],
                "registrant": registrant,
            },
        )
    return bool(inserted)


def complete(con, generation, completed, duration, *, manual=False):
    if (
        isinstance(completed, bool)
        or isinstance(duration, bool)
        or not isinstance(completed, (int, float))
        or not isinstance(duration, (int, float))
        or not math.isfinite(completed)
        or not math.isfinite(duration)
        or completed <= 0
        or duration <= 0
    ):
        abort(400)

    report_at = completed if manual else completed + AUTO_DELAY
    cur = con.execute(
        """
        INSERT OR IGNORE INTO live_event_salesforce_completion
            (generation, completed, duration, report_at)
        VALUES (?, ?, ?, ?)
        """,
        (generation, float(completed), float(duration), float(report_at)),
    )
    if cur.rowcount == 1:
        return True
    if not manual:
        return False

    cur = con.execute(
        """
        UPDATE live_event_salesforce_completion
        SET report_at = ?
        WHERE generation = ? AND report_at > ?
        """,
        (float(completed), generation, float(completed)),
    )
    return cur.rowcount > 0

def cancel(con, *, generation=None, sharing=None):
    if (generation is None) == (sharing is None):
        raise ValueError("Choose one Salesforce delivery scope.")
    column, value = (
        ("sharing", sharing) if generation is None else ("generation", generation)
    )
    con.execute(
        f"""UPDATE action SET status='rejected', decision='canceled'
            WHERE status IN ('eligible', 'waiting') AND id IN (
                SELECT id FROM live_event_salesforce_engagement
                WHERE {column}=? AND state='queued'
            )""",
        (value,),
    )
    con.execute(
        f"""UPDATE live_event_salesforce_engagement SET state='canceled', due=NULL
            WHERE {column}=? AND state='queued'""",
        (value,),
    )


def next_job(con, now):
    return con.execute(
        """
        SELECT j.*, c.report_at, c.duration
        FROM live_event_salesforce_engagement j
        LEFT JOIN live_event_salesforce_completion c ON c.generation = j.generation
        JOIN action a ON a.id = j.id
        WHERE j.state = 'queued'
          AND a.status IN ('eligible', 'rejected')
          AND (
              a.status = 'rejected'
              OR (
                  j.due <= ?
                  AND c.report_at <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM live_event_contact_delivery d
                      WHERE d.provider = 'salesforce'
                        AND d.generation = j.generation
                        AND d.registrant = j.registrant
                        AND d.event = j.event
                        AND d.opens = j.opens
                        AND d.state = 'queued'
                  )
              )
          )
        ORDER BY j.due, j.id
        LIMIT 1
    """,
        (now, now),
    ).fetchone()


def status(con, generation):
    rows = con.execute(
        """
        SELECT CASE
                   WHEN j.state = 'queued' AND a.status = 'waiting' THEN 'waiting'
                   ELSE j.state
               END AS label,
               COUNT(*) AS count
        FROM live_event_salesforce_engagement j
        LEFT JOIN action a ON a.id = j.id
        WHERE j.generation = ?
        GROUP BY label
        ORDER BY label
        """,
        (generation,),
    ).fetchall()
    return {label: count for label, count in rows}

def scope(con, job, connection, validate, stop):
    if stop.is_set():
        abort(423)

    row = con.execute(
        "SELECT a.status "
        "FROM live_event_salesforce_engagement j "
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

    plan = outbox.get(con, PROVIDER, job["event"], job["opens"])
    if not plan:
        abort(409)

    if plan["generation"] != job["generation"]:
        abort(409)

    connection.authorize(con, plan["provider_revision"])

    dest = json.loads(plan["destination"])
    if dest.get("engagement") is not True:
        abort(409)

    validate(con, plan)

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
        "  AND d.provider = 'salesforce' "
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

    return (plan["provider_revision"], dest["campaign"], hit["email"])

def payload(con, job, now):
    from event_engagement import activity_snapshot

    snap = activity_snapshot(con, job, now)
    live_ms = snap['live_ms']
    replay_ms = snap['replay_ms']

    watch_percent = round(
        min(100, max(0, max(live_ms, replay_ms) / (job['duration'] * 1000) * 100))
    )

    if snap['attended_live']:
        status = 'Attended Live'
    elif replay_ms > 0:
        status = 'Registered'
    elif now >= job['report_at'] + WINDOW_SECONDS:
        status = 'No Show'
    else:
        status = None

    return {'status': status, 'watch_percent': watch_percent}

def deliver(tx, job, connection, validate, stop, clock):
    with tx() as con:
        expected = scope(con, job, connection, validate, stop)
        fields = payload(con, job, clock())
    revision, campaign, email = expected
    def before_write():
        with tx() as fresh:
            if scope(fresh, job, connection, validate, stop) != expected:
                abort(409)
    return salesforce_outbound.publish_engagement(
        connection, revision, campaign=campaign, kind="webinar",
        source_ref=f"{job['event']}:{job['opens']}",
        registrant=job['registrant'], address=email,
        before_write=before_write, **fields)

def completion(con, generation):
    return con.execute(
        "SELECT * FROM live_event_salesforce_completion WHERE generation=?",
        (generation,)
    ).fetchone()


def run_one(tx, connection, validate, stop, clock):
    now = int(clock())
    def finish(con, job_id, state, due, attempts):
        if state == 'sent':
            row = con.execute(
                "SELECT c.report_at FROM live_event_salesforce_completion c "
                "JOIN live_event_salesforce_engagement j ON j.generation=c.generation "
                "WHERE j.id=?", (job_id,)
            ).fetchone()
            if row:
                report_at = row[0]
                if now < report_at + WINDOW_SECONDS:
                    elapsed = max(0, now - report_at)
                    slot = math.floor(elapsed / BATCH_SECONDS) + 1
                    state = 'queued'
                    due = math.ceil(min(report_at + WINDOW_SECONDS,
                                        report_at + slot * BATCH_SECONDS))
                    attempts = 0
        con.execute(
            "UPDATE live_event_salesforce_engagement SET state=?, due=?, attempts=? "
            "WHERE id=? AND state='queued'",
            (state, due, attempts, job_id)
        )
    return outbox.run_job(tx, next_job,
        lambda job: deliver(tx, job, connection, validate, stop, lambda: now),
        finish, stop, clock, retry_ambiguous=True)
