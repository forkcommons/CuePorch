import datetime
import json
import time
from math import floor, ceil
from uuid import uuid4

from flask import abort

from catalog import video_version_key
import event_contact_delivery as outbox
import salesforce_outbound

STEP = "event_engagement_sync"
DAY = 86400
LEAD_IN = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_salesforce_video_report_plan (
    generation TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS library_salesforce_video_report (
    id TEXT PRIMARY KEY,
    generation TEXT NOT NULL,
    work TEXT NOT NULL,
    version TEXT NOT NULL,
    recipient TEXT NOT NULL,
    email_key TEXT,
    first_due INTEGER NOT NULL,
    due INTEGER,
    state TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS library_salesforce_video_report_queued_uq
    ON library_salesforce_video_report(generation, work, version, recipient)
    WHERE state = 'queued';

CREATE INDEX IF NOT EXISTS library_salesforce_video_report_state_due_idx
    ON library_salesforce_video_report(state, due);

CREATE TABLE IF NOT EXISTS library_salesforce_video_report_sample (
    session TEXT PRIMARY KEY,
    report TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS library_salesforce_video_report_sample_report_idx
    ON library_salesforce_video_report_sample(report);
"""

def enabled(con, generation):
    row = con.execute(
        "SELECT 1 FROM library_salesforce_video_report_plan WHERE generation = ?",
        (generation,),
    ).fetchone()
    return row is not None

def enable(con, generation):
    con.execute(
        "INSERT OR IGNORE INTO library_salesforce_video_report_plan(generation) VALUES (?)",
        (generation,),
    )

def next_slot(first_due, after):
    if after < first_due:
        return first_due
    k = floor((after - first_due) / DAY) + 1
    return first_due + k * DAY

def first_report_at(work):
    duration = work.get("duration_seconds")
    if type(duration) not in (int, float):
        return None
    if not (0 < duration <= 7 * 86400):
        return None
    for raw in (work.get("source_created_time"), work.get("library_added_at")):
        try:
            instant = datetime.datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if instant.tzinfo is None or instant.utcoffset() is None:
            continue
        return ceil(instant.timestamp() + LEAD_IN)
    return None

def consent(con, action, plan, work, recipient, email_key, now):
    if plan["kind"] != "video":
        return None
    if work["id"] != plan["subject"]:
        return None
    if not enabled(con, plan["generation"]):
        return None
    first_due = first_report_at(work)
    if first_due is None:
        return None
    report_id = uuid4().hex
    version = video_version_key(work)
    due = next_slot(first_due, now - 1)
    cur = con.execute(
        "INSERT OR IGNORE INTO library_salesforce_video_report "
        "(id, generation, work, version, recipient, email_key, first_due, due, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')",
        (report_id, plan["generation"], work["id"], version, recipient, email_key, first_due, due),
    )
    if cur.rowcount == 0:
        row = con.execute(
            "SELECT id FROM library_salesforce_video_report "
            "WHERE generation = ? AND work = ? AND version = ? AND recipient = ? "
            "AND email_key = ? AND state = 'queued'",
            (plan["generation"], work["id"], version, recipient, email_key),
        ).fetchone()
        return row["id"] if row else None
    action(con, report_id, STEP, {"provider": "salesforce", "kind": "video", "subject": work["id"]})
    return report_id

def revoke(con, *, generation=None, report=None):
    if (generation is None) == (report is None):
        raise ValueError("Choose one Salesforce delivery scope.")

    column, value = ("id", report) if generation is None else ("generation", generation)

    con.execute(
        f"""UPDATE action SET status='rejected', decision='canceled'
            WHERE status IN ('eligible', 'waiting') AND id IN (
                SELECT id FROM library_salesforce_video_report
                WHERE {column}=? AND state='queued'
            )""",
        (value,),
    )

    con.execute(
        f"""UPDATE library_salesforce_video_report
            SET state = CASE WHEN state='queued' THEN 'canceled' ELSE state END,
                due = NULL,
                email_key = NULL
            WHERE {column}=?""",
        (value,),
    )

    con.execute(
        f"""DELETE FROM library_salesforce_video_report_sample
            WHERE report IN (
                SELECT id FROM library_salesforce_video_report WHERE {column}=?
            )""",
        (value,),
    )

    if generation is not None:
        con.execute(
            "DELETE FROM library_salesforce_video_report_plan WHERE generation=?",
            (generation,),
        )

def link_sample(con, report, work, version, session):
    cur = con.execute(
        "INSERT OR IGNORE INTO library_salesforce_video_report_sample(session,report) "
        "SELECT ?, r.id "
        "FROM library_salesforce_video_report r "
        "JOIN library_salesforce_video_report_plan p ON p.generation=r.generation "
        "JOIN video_analytics_session s ON s.id=? AND s.work=r.work AND s.version=r.version AND s.played=1 "
        "WHERE r.id=? AND r.work=? AND r.version=? AND r.state='queued'",
        (session, session, report, work, version),
    )
    return cur.rowcount

def status(con, generation):
    cur = con.execute(
        "SELECT CASE WHEN r.state='queued' AND a.status='waiting' THEN 'waiting' ELSE r.state END, "
        "COUNT(*) "
        "FROM library_salesforce_video_report r "
        "LEFT JOIN action a ON a.id=r.id "
        "WHERE r.generation=? "
        "GROUP BY CASE WHEN r.state='queued' AND a.status='waiting' THEN 'waiting' ELSE r.state END",
        (generation,),
    )
    return {row[0]: row[1] for row in cur}

def next_job(con, now):
    return con.execute(
        "SELECT r.* FROM library_salesforce_video_report r "
        "JOIN action a ON a.id=r.id "
        "WHERE r.state='queued' AND (a.status='rejected' OR (a.status='eligible' "
        "AND r.due<=? AND NOT EXISTS (SELECT 1 FROM library_salesforce_form_delivery d "
        "WHERE d.generation=r.generation AND d.kind='video' AND d.subject=r.work "
        "AND d.recipient_key=r.recipient AND d.state='queued'))) "
        "ORDER BY r.due,r.id LIMIT 1",
        (now,),
    ).fetchone()

def delivery_context(con, row):
    query = """
        SELECT p.provider_revision, p.list_id, p.form_revision, v.email
        FROM library_salesforce_form_plan p
        JOIN library_salesforce_video_report_plan e
          ON e.generation = p.generation
        JOIN library_salesforce_form_delivery d
          ON d.kind = p.kind
         AND d.subject = p.subject
         AND d.generation = p.generation
         AND d.recipient_key = ?
         AND d.state = 'sent'
        JOIN viewer_registrant v
          ON v.kind = p.kind
         AND v.subject = p.subject
         AND v.email_key = ?
        WHERE p.kind = 'video'
          AND p.subject = ?
          AND p.generation = ?
    """
    return con.execute(
        query,
        (
            row["recipient"],
            row["email_key"],
            row["work"],
            row["generation"],
        ),
    ).fetchone()

def scope(con, job, connection, validate, stop):
    # Stop signal -> 423 (retryable)
    if stop.is_set():
        abort(423)

    row = con.execute(
        "SELECT r.*, a.status AS approval "
        "FROM library_salesforce_video_report r "
        "JOIN action a ON a.id = r.id "
        "WHERE r.id = ? AND r.state = 'queued'",
        (job["id"],),
    ).fetchone()

    # Missing / not eligible -> 409
    if row is None:
        abort(409)

    row = dict(row)

    # Row waiting -> 423, anything not pending is ineligible -> 409
    if row.get("approval") == "waiting":
        abort(423)
    if row.get("approval") != "eligible":
        abort(409)

    form = validate(con, "video", row["work"])
    context = delivery_context(con, row)

    if not form or not context:
        abort(409)

    if form["revision"] != context["form_revision"]:
        abort(409)
    if form["salesforce_version"] != row["version"]:
        abort(409)

    connection.authorize(con, context["provider_revision"])

    return (
        context["provider_revision"],
        context["list_id"],
        context["email"],
        form["salesforce_duration"],
    )

def metrics(con, job, duration, now):
    limit = round(duration * 1000)
    cur = con.execute(
        "SELECT s.intervals FROM video_analytics_session s "
        "JOIN library_salesforce_video_report_sample l ON l.session=s.id "
        "WHERE l.report=? AND s.work=? AND s.version=? AND s.played=1 AND s.created<=?",
        (job["id"], job["work"], job["version"], now),
    )
    spans = []
    views = 0
    for (raw,) in cur.fetchall():
        views += 1
        if raw:
            for start, stop in json.loads(raw):
                spans.append((start, stop))
    spans.sort()
    end = 0
    covered = 0
    for start, stop in spans:
        if stop > limit:
            stop = limit
        covered += max(0, stop - max(end, start))
        end = max(stop, end)
    pct = 0 if limit <= 0 else min(100, round(100 * covered / limit))
    return (pct, views)

def deliver(tx, job, connection, validate, stop, clock):
    with tx() as con:
        expected = scope(con, job, connection, validate, stop)
        revision, campaign, email, duration = expected
        watch_percent, video_views = metrics(con, job, duration, clock())

    def before_write():
        with tx() as fresh:
            if scope(fresh, job, connection, validate, stop) != expected:
                abort(409)

    return salesforce_outbound.publish_engagement(
        connection, revision, campaign=campaign, kind="video",
        source_ref=job["work"],
        registrant=job["recipient"], address=email,
        before_write=before_write,
        watch_percent=watch_percent, video_views=video_views)

def finish(con, id, state, due, attempts, now):
    cur = con.cursor()
    if state == "sent":
        cur.execute("SELECT first_due FROM library_salesforce_video_report WHERE id=?", (id,))
        state = "queued"
        due = next_slot(cur.fetchone()[0], now)
        attempts = 0
    cur.execute(
        "UPDATE library_salesforce_video_report SET state=?,due=?,attempts=? WHERE id=? AND state='queued'",
        (state, due, attempts, id),
    )
    if state != "queued" and cur.rowcount:
        revoke(con, report=id)

def run_one(tx, connection, validate, stop, clock=time.time):
    return outbox.run_job(
        tx,
        next_job,
        lambda job: deliver(tx, job, connection, validate, stop, clock),
        lambda con, job_id, state, due, attempts: finish(
            con, job_id, state, due, attempts, int(clock())
        ),
        stop,
        clock,
        retry_ambiguous=True,
    )
