"""Adobe/Marketo engagement payload builder for CuePorch live activity."""

import json
import math


WATCH_FIELD = "cuePorchWatchPercentage"
QUESTIONS_FIELD = "cuePorchQuestions"
POLLS_FIELD = "cuePorchPolls"

DELIMITER = "\n"


def _truncate(value, limit):
    if type(limit) is not int or limit <= 0:
        raise ValueError("Marketo field length must be positive.")
    if len(value.encode("utf-16-le")) // 2 <= limit:
        return value
    units = 0
    out = []
    for ch in value:
        size = 2 if ord(ch) > 0xFFFF else 1
        if units + size > limit:
            break
        units += size
        out.append(ch)
    return "".join(out)


def payload(con, job, fields, duration_seconds, completed_at):
    """Build (properties, attended_live) for one live occurrence and consent."""

    from event_engagement import activity_snapshot

    snapshot = activity_snapshot(con, job, completed_at)
    attended_live = snapshot["attended_live"]
    watched_ms = snapshot["live_ms"]
    questions = snapshot["questions"]
    polls = snapshot["polls"]

    has_questions = bool(questions)
    has_polls = bool(polls)

    if not attended_live and not has_questions and not has_polls:
        return None, False

    properties = {}

    if attended_live:
        valid_duration = False
        try:
            duration = float(duration_seconds)
            valid_duration = math.isfinite(duration) and duration > 0
        except (TypeError, ValueError):
            valid_duration = False
        if valid_duration:
            raw = (watched_ms / (duration * 1000.0)) * 100.0
            if raw < 0:
                raw = 0.0
            elif raw > 100.0:
                raw = 100.0
            properties[WATCH_FIELD] = int(math.floor(raw + 0.5))

    properties[QUESTIONS_FIELD] = _truncate(
        DELIMITER.join(questions), fields[QUESTIONS_FIELD]["length"]
    )
    properties[POLLS_FIELD] = _truncate(
        DELIMITER.join(polls), fields[POLLS_FIELD]["length"]
    )

    return properties, attended_live


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event_marketo_engagement (
    id TEXT PRIMARY KEY NOT NULL,
    event TEXT NOT NULL,
    opens INTEGER NOT NULL,
    generation TEXT NOT NULL,
    registrant TEXT NOT NULL,
    sharing TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    due INTEGER DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    fields_sent INTEGER NOT NULL DEFAULT 0,
    attended_live INTEGER NOT NULL DEFAULT 0,
    UNIQUE(generation, registrant)
);
CREATE INDEX IF NOT EXISTS idx_live_event_marketo_engagement_sharing
    ON live_event_marketo_engagement(sharing);
CREATE TABLE IF NOT EXISTS live_event_marketo_completion (
    generation TEXT PRIMARY KEY NOT NULL,
    completed REAL NOT NULL,
    duration REAL NOT NULL
);
"""


def enqueue(con, action, plan, registrant, sharing):
    from uuid import uuid4

    row_id = str(uuid4())
    generation = plan["generation"]
    cur = con.execute(
        """
        INSERT OR IGNORE INTO live_event_marketo_engagement
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
            dict(
                provider="marketo",
                event=plan["event"],
                occurrence=plan["opens"],
                registrant=registrant,
            ),
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
        INSERT OR IGNORE INTO live_event_marketo_completion
            (generation, completed, duration)
        VALUES (?, ?, ?)
        """,
        (generation, completed, duration),
    )
    return bool(cur.rowcount > 0)


def cancel(con, *, generation=None, sharing=None):
    if (generation is None) == (sharing is None):
        raise ValueError("Choose one Marketo delivery scope.")
    column, value = (
        ("sharing", sharing) if generation is None else ("generation", generation)
    )
    con.execute(
        f"""UPDATE action SET status='rejected', decision='canceled'
            WHERE status IN ('eligible', 'waiting') AND id IN (
                SELECT id FROM live_event_marketo_engagement
                WHERE {column}=? AND state='queued'
            )""",
        (value,),
    )
    con.execute(
        f"""UPDATE live_event_marketo_engagement SET state='canceled', due=NULL
            WHERE {column}=? AND state='queued'""",
        (value,),
    )
    if sharing is not None:
        con.execute(
            "DELETE FROM live_event_shared_activity WHERE sharing=?", (sharing,)
        )


def next_job(con, now):
    return con.execute(
        """
        SELECT j.*, c.completed, c.duration
        FROM live_event_marketo_engagement j
        JOIN live_event_marketo_completion c ON c.generation = j.generation
        JOIN action a ON a.id = j.id
        WHERE j.state = 'queued'
          AND j.due <= ?
          AND a.status IN ('eligible', 'rejected')
          AND (
              a.status = 'rejected'
              OR NOT EXISTS (
                  SELECT 1
                  FROM live_event_contact_delivery d
                  WHERE d.provider = 'marketo'
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


def scope(con, job, revision, validate, stop):
    from flask import abort
    from event_contact_delivery import get

    if stop.is_set():
        abort(423)

    row = con.execute(
        "SELECT j.fields_sent, j.attended_live, a.status "
        "FROM live_event_marketo_engagement j "
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

    plan = get(con, "marketo", job["event"], job["opens"])
    if not plan:
        abort(409)

    if plan["generation"] != job["generation"]:
        abort(409)
    if plan["provider_revision"] != revision:
        abort(409)

    dest = json.loads(plan["destination"])
    if dest.get("engagement") is not True:
        abort(409)

    validate(con, plan)

    hit = con.execute(
        "SELECT r.email, d.lead_id "
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
        "  AND d.provider = 'marketo' "
        "  AND d.generation = ? "
        "  AND d.state = 'sent' "
        "  AND d.lead_id > 0",
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

    return (
        dest["program"],
        hit["lead_id"],
        hit["email"],
        row["fields_sent"],
        bool(row["attended_live"]),
    )


def deliver(tx, job, connection, revision, validate, stop):
    from flask import abort
    import marketo_crm

    def before_write():
        with tx() as con:
            scope(con, job, revision, validate, stop)

    with tx() as con:
        program, lead_id, email, fields_sent, attended = scope(
            con, job, revision, validate, stop
        )

    lead = marketo_crm.lookup_lead(connection, email)
    if lead is None or lead.get("id") != lead_id:
        abort(409)
    if lead.get("opted_out"):
        return "skipped"

    if not fields_sent:
        fields = marketo_crm.ensure_engagement_fields(
            connection, before_write=before_write
        )
        with tx() as con:
            scope(con, job, revision, validate, stop)
            properties, attended = payload(
                con, job, fields, job["duration"], job["completed"]
            )
        if properties is None:
            return "skipped"
        marketo_crm.save_member_engagement(
            connection, program, lead_id, properties, before_write=before_write
        )
        with tx() as con:
            scope(con, job, revision, validate, stop)
            con.execute(
                "UPDATE live_event_marketo_engagement "
                "SET fields_sent=1, attended_live=? "
                "WHERE id=? AND state='queued'",
                (int(attended), job["id"]),
            )

    if attended:
        lead = marketo_crm.lookup_lead(connection, email)
        if lead is None or lead.get("id") != lead_id:
            abort(409)
        if lead.get("opted_out"):
            return "skipped"
        marketo_crm.mark_attended_live(
            connection, program, lead_id, before_write=before_write
        )

    return "sent"


def run_one(tx, connection, revision, validate, stop, clock):
    from werkzeug.exceptions import HTTPException

    if stop.is_set():
        return False
    with tx() as con:
        job = next_job(con, int(clock()))
    if job is None:
        return False
    attempts, due = job["attempts"] + 1, None
    try:
        state = deliver(tx, job, connection, revision, validate, stop)
    except HTTPException as exc:
        code = getattr(exc, "code", None)
        if code == 423:
            return False
        if code == 429 and attempts < 5:
            state = "queued"
            due = int(clock()) + max(15, getattr(exc, "retry_after", None) or 0)
        elif code in (409, 404):
            state = "canceled"
        elif code in (502, 503, 504):
            state = "unconfirmed"
        else:
            state = "problem"
    except (ValueError, KeyError):
        state = "problem"
    with tx() as con:
        con.execute(
            "UPDATE live_event_marketo_engagement SET state=?, due=?, attempts=? "
            "WHERE id=? AND state='queued'",
            (state, due, attempts, job["id"]),
        )
    return True
