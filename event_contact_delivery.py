import json
from uuid import uuid4

from flask import abort

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event_contact_plan (
    provider TEXT NOT NULL,
    event TEXT NOT NULL,
    opens INTEGER NOT NULL,
    generation TEXT NOT NULL,
    provider_revision TEXT NOT NULL,
    event_revision TEXT NOT NULL,
    form_revision TEXT NOT NULL,
    deadline INTEGER NOT NULL,
    destination TEXT NOT NULL,
    PRIMARY KEY (provider, event, opens)
);
CREATE TABLE IF NOT EXISTS live_event_contact_delivery (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    event TEXT NOT NULL,
    opens INTEGER NOT NULL,
    generation TEXT NOT NULL,
    registrant TEXT NOT NULL,
    due INTEGER,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lead_id INTEGER,
    UNIQUE (provider, event, opens, generation, registrant)
);
CREATE INDEX IF NOT EXISTS live_event_contact_delivery_pending_due
    ON live_event_contact_delivery (state, due);
CREATE TABLE IF NOT EXISTS live_event_salesforce_payload (delivery TEXT PRIMARY KEY, answers TEXT NOT NULL);
"""

STEP = "event_contact_sync"


def get(con, provider, event, opens):
    return con.execute(
        "SELECT provider, event, opens, generation, provider_revision, "
        "event_revision, form_revision, deadline, destination "
        "FROM live_event_contact_plan "
        "WHERE provider=? AND event=? AND opens=?",
        (provider, event, opens),
    ).fetchone()


def enqueue(con, action, plan, registrant, now):
    delivery_id = uuid4().hex
    cur = con.execute(
        """
        INSERT OR IGNORE INTO live_event_contact_delivery
        (id, provider, event, opens, generation, registrant, due, state, attempts)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0)
        """,
        (
            delivery_id,
            plan["provider"],
            plan["event"],
            plan["opens"],
            plan["generation"],
            registrant,
            now,
        ),
    )
    if cur.rowcount == 1:
        action(
            con,
            delivery_id,
            STEP,
            {
                "provider": plan["provider"],
                "event": plan["event"],
                "occurrence": plan["opens"],
                "registrant": registrant,
            },
        )
        return delivery_id
    return None


def disable(con, provider, event, opens):
    if provider == "marketo":
        plan = get(con, provider, event, opens)
        if plan:
            import event_marketo_engagement

            event_marketo_engagement.cancel(con, generation=plan["generation"])
    if provider == "mailchimp":
        plan = get(con, provider, event, opens)
        if plan:
            destination = json.loads(plan["destination"])
            if destination.get("engagement") is True:
                import event_mailchimp_engagement

                event_mailchimp_engagement.cancel(con, generation=plan["generation"])
    params = (provider, event, opens)
    con.execute(
        """
        UPDATE action
        SET status='rejected', decision='canceled'
        WHERE status IN ('waiting','eligible')
          AND id IN (
              SELECT id
              FROM live_event_contact_delivery
              WHERE provider=? AND event=? AND opens=? AND state='queued'
          )
        """,
        params,
    )
    con.execute(
        """
        UPDATE live_event_contact_delivery
        SET state='canceled', due=NULL
        WHERE provider=? AND event=? AND opens=? AND state='queued'
        """,
        params,
    )
    if provider == "salesforce":
        con.execute(
            "DELETE FROM live_event_salesforce_payload WHERE delivery IN ("
            "SELECT id FROM live_event_contact_delivery WHERE provider=? AND event=? AND opens=?)",
            params,
        )
    con.execute(
        "DELETE FROM live_event_contact_plan WHERE provider=? AND event=? AND opens=?",
        params,
    )


def set_destination(
    con,
    provider,
    event,
    opens,
    *,
    provider_revision,
    event_revision,
    form_revision,
    deadline,
    destination,
):
    payload = json.dumps(destination)
    disable(con, provider, event, opens)
    con.execute(
        """
        INSERT INTO live_event_contact_plan
        (provider, event, opens, generation, provider_revision,
         event_revision, form_revision, deadline, destination)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            provider,
            event,
            opens,
            uuid4().hex,
            provider_revision,
            event_revision,
            form_revision,
            deadline,
            payload,
        ),
    )
    return get(con, provider, event, opens)


def next_job(con, provider, now):
    return con.execute(
        "SELECT d.* FROM live_event_contact_delivery d "
        "JOIN action a ON a.id=d.id "
        "WHERE d.provider=? AND d.due<=? AND d.state='queued' "
        "AND a.status IN ('eligible','rejected') "
        "ORDER BY d.due, d.id LIMIT 1",
        (provider, now),
    ).fetchone()


def scope(con, job, provider_revision, now, validate, stop):
    row = con.execute(
        "SELECT d.state, a.status FROM live_event_contact_delivery d "
        "JOIN action a ON a.id=d.id WHERE d.id=?",
        (job["id"],),
    ).fetchone()
    if row is None:
        abort(409)

    stopped = stop.is_set()
    if stopped or row["status"] == "waiting":
        abort(423)
    if row["state"] != "queued" or row["status"] != "eligible":
        abort(409)

    plan = get(con, job["provider"], job["event"], job["opens"])
    if plan is None:
        abort(409)
    if (
        plan["provider_revision"] != provider_revision
        or plan["generation"] != job["generation"]
        or plan["deadline"] <= now
    ):
        abort(409)

    if job["provider"] == "salesforce":
        registrant = con.execute(
            "SELECT r.email, p.answers FROM live_event_registrant r "
            "JOIN live_event_salesforce_payload p ON p.delivery=? "
            "WHERE r.id=? AND r.event=? AND r.opens=?",
            (job["id"], job["registrant"], job["event"], job["opens"]),
        ).fetchone()
    else:
        registrant = con.execute(
            "SELECT email, answers FROM live_event_registrant "
            "WHERE id=? AND event=? AND opens=? AND source='form'",
            (job["registrant"], job["event"], job["opens"]),
        ).fetchone()
    if registrant is None:
        abort(409)

    validate(con, plan)

    try:
        destination = json.loads(plan["destination"])
        answers = json.loads(registrant["answers"])
    except (TypeError, ValueError):
        raise ValueError("malformed event contact data")
    if not isinstance(destination, dict) or not isinstance(answers, dict):
        raise ValueError("malformed event contact data")

    return plan, destination, registrant["email"], answers


def prepare_marketo_lead(connection, email, answers, mapping, stage_id, before_write):
    import marketo_crm

    record = marketo_crm.lookup_lead(connection, email)
    if record and record.get("opted_out"):
        return None
    if stage_id is not None:
        if record and record.get("id") == stage_id:
            return stage_id
        abort(409)
    mapped_properties_result = marketo_crm.mapped_properties(answers, mapping)
    if record and not mapped_properties_result:
        return record["id"]
    lead_id = marketo_crm.save_lead(
        connection,
        email,
        mapped_properties_result,
        record["id"] if record else None,
        before_write=before_write,
    )
    return lead_id


def deliver_marketo(tx, job, connection, revision, validate, stop, clock):
    import marketo_crm

    with tx() as con:
        plan, destination, email, answers = scope(
            con, job, revision, int(clock()), validate, stop
        )

    def before_write():
        with tx() as con:
            scope(con, job, revision, int(clock()), validate, stop)

    lead_id = prepare_marketo_lead(
        connection, email, answers, destination["mapping"], job["lead_id"], before_write
    )
    if lead_id is None:
        return "skipped"
    with tx() as con:
        plan, destination, email, answers = scope(
            con, job, revision, int(clock()), validate, stop
        )
        con.execute(
            "UPDATE live_event_contact_delivery SET lead_id=? "
            "WHERE id=? AND state='queued'",
            (lead_id, job["id"]),
        )
    lead = marketo_crm.lookup_lead(connection, email)
    if not lead or lead["id"] != lead_id:
        abort(409)
    if lead.get("opted_out"):
        return "skipped"
    marketo_crm.mark_registered(
        connection, destination["program"], lead_id, before_write=before_write
    )
    return "sent"


def run_job(tx, select, deliver, finish, stop, clock, *, retry_ambiguous=False):
    from werkzeug.exceptions import HTTPException

    if stop.is_set():
        return False
    with tx() as con:
        job = select(con, int(clock()))
    if job is None:
        return False
    attempts, due = job["attempts"] + 1, None
    try:
        state = deliver(job)
    except HTTPException as exc:
        code = getattr(exc, "code", None)
        if code == 423:
            return False
        if code == 429 and attempts < 5:
            state = "queued"
            due = int(clock()) + max(15, getattr(exc, "retry_after", None) or 0)
        elif retry_ambiguous and code in (502, 503, 504) and attempts < 5:
            state = "queued"
            due = int(clock()) + 15 * 2 ** (attempts - 1)
        elif code in (409, 404):
            state = "canceled"
        elif code in (502, 503, 504):
            state = "unconfirmed"
        else:
            state = "problem"
    except (ValueError, KeyError):
        state = "problem"
    with tx() as con:
        finish(con, job["id"], state, due, attempts)
    return True


def _run_one(tx, provider, deliver, stop, clock):
    def finish(con, job_id, state, due, attempts):
        con.execute(
            "UPDATE live_event_contact_delivery SET state=?, due=?, attempts=? "
            "WHERE id=? AND state='queued'",
            (state, due, attempts, job_id),
        )
        if provider == "salesforce" and state != "queued":
            con.execute("DELETE FROM live_event_salesforce_payload WHERE delivery=?", (job_id,))
    return run_job(
        tx,
        lambda con, ts: next_job(con, provider, ts),
        deliver,
        finish,
        stop,
        clock,
        retry_ambiguous=provider == "salesforce",
    )


def run_one_marketo(tx, connection, revision, validate, stop, clock):
    return _run_one(
        tx,
        "marketo",
        lambda job: deliver_marketo(
            tx, job, connection, revision, validate, stop, clock
        ),
        stop,
        clock,
    )


def _start_worker(app, database, provider, tick):
    from threading import Event, Thread
    from filelock import FileLock, Timeout

    stop = Event()
    lock_path = str(database) + f".{provider}-outbound.lock"

    def worker():
        while not stop.is_set():
            try:
                with FileLock(lock_path, timeout=0):
                    if tick(stop):
                        continue
                stop.wait(1)
            except Timeout:
                stop.wait(1)
            except Exception:
                app.logger.error("%s outbound worker failed", provider)
                stop.wait(1)

    thread = Thread(target=worker, daemon=True)
    thread.start()

    def stop_worker():
        stop.set()
        thread.join(8)

    return stop_worker


def start_marketo(
    app,
    tx,
    database,
    connection,
    revision,
    validate,
    clock=None,
    *,
    engagement_validate=None,
):
    from time import time
    import event_marketo_engagement

    if connection is None:
        return lambda: None

    if clock is None:
        clock = time

    def tick(stop):
        registration_result = run_one_marketo(
            tx, connection, revision, validate, stop, clock
        )
        engagement_result = (
            event_marketo_engagement.run_one(
                tx, connection, revision, engagement_validate, stop, clock
            )
            if engagement_validate is not None
            else False
        )
        return registration_result or engagement_result

    return _start_worker(app, database, "marketo", tick)


def receipt(con, provider, event, opens, revision):
    plan = get(con, provider, event, opens)
    available = revision is not None
    enabled = (
        plan is not None
        and revision is not None
        and plan["provider_revision"] == revision
    )
    state = "connected" if enabled else ("stale" if plan is not None else "off")
    result = {"available": available, "enabled": enabled, "state": state, "states": {}}
    if plan is not None:
        rows = con.execute(
            "SELECT CASE WHEN d.state='queued' AND a.status='waiting' "
            "THEN 'waiting' ELSE d.state END AS s, COUNT(*) "
            "FROM live_event_contact_delivery d LEFT JOIN action a ON a.id=d.id "
            "WHERE d.provider=? AND d.event=? AND d.opens=? "
            "AND d.generation=? GROUP BY 1",
            (provider, event, opens, plan["generation"]),
        ).fetchall()
        result["states"] = {r[0]: r[1] for r in rows}
    if enabled and provider == "salesforce":
        result.update(json.loads(plan["destination"]))
        return result
    if enabled and provider == "mailchimp":
        destination = json.loads(plan["destination"])
        result["audience"] = destination["audience"]
        result["mapping"] = {key: value["tag"] for key, value in destination["mapping"].items()}
        result["engagement"] = destination.get("engagement") is True
        if result["engagement"]:
            import event_mailchimp_engagement

            result["engagement_states"] = event_mailchimp_engagement.status(con, plan["generation"])
        return result
    if enabled:
        destination = json.loads(plan["destination"])
        result["program"] = destination["program"]
        result["mapping"] = {k: v["name"] for k, v in destination["mapping"].items()}
        result["engagement"] = destination.get("engagement") is True
        if result["engagement"]:
            rows = con.execute(
                "SELECT CASE WHEN e.state='queued' AND a.status='waiting' "
                "THEN 'waiting' ELSE e.state END AS s, COUNT(*) "
                "FROM live_event_marketo_engagement e LEFT JOIN action a ON a.id=e.id "
                "WHERE e.event=? AND e.opens=? "
                "AND e.generation=? GROUP BY 1",
                (event, opens, plan["generation"]),
            ).fetchall()
            result["engagement_states"] = {r[0]: r[1] for r in rows}
    return result
