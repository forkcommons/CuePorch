"""Event HubSpot outbound — DB helpers."""

from uuid import uuid4

import event_hubspot_engagement

STEP = "event_contact_sync"


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event_hubspot_outbound (
    event          TEXT    NOT NULL,
    opens          INTEGER NOT NULL,
    generation     TEXT    NOT NULL,
    connection     TEXT    NOT NULL,
    event_revision TEXT    NOT NULL,
    form_revision  TEXT    NOT NULL,
    segment        TEXT    NOT NULL,
    subscription   TEXT    NOT NULL,
    business_unit  INTEGER NOT NULL,
    deadline       INTEGER NOT NULL,
    PRIMARY KEY (event, opens)
);

CREATE TABLE IF NOT EXISTS live_event_hubspot_delivery (
    id         TEXT    PRIMARY KEY,
    event      TEXT    NOT NULL,
    opens      INTEGER NOT NULL,
    generation TEXT    NOT NULL,
    registrant TEXT    NOT NULL,
    due        INTEGER,
    state      TEXT    NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    problem    TEXT,
    UNIQUE (event, opens, generation, registrant)
);

CREATE INDEX IF NOT EXISTS live_event_hubspot_delivery_due_idx
    ON live_event_hubspot_delivery (due);
"""


def get(con, event, opens):
    return con.execute(
        "SELECT * FROM live_event_hubspot_outbound WHERE event=? AND opens=?",
        (event, opens),
    ).fetchone()


def set_destination(
    con,
    event,
    opens,
    *,
    connection,
    event_revision,
    form_revision,
    segment,
    subscription,
    business_unit,
    deadline,
):
    disable(con, event, opens)
    generation = uuid4().hex
    con.execute(
        "INSERT INTO live_event_hubspot_outbound "
        "(event, opens, generation, connection, event_revision, form_revision, "
        "segment, subscription, business_unit, deadline) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event,
            opens,
            generation,
            connection,
            event_revision,
            form_revision,
            segment,
            subscription,
            business_unit,
            deadline,
        ),
    )
    return get(con, event, opens)


def disable(con, event, opens):
    event_hubspot_engagement.cancel(con, event, opens)
    cur = con.cursor()
    cur.execute(
        """UPDATE action
           SET status = 'rejected', decision = 'canceled'
           WHERE status = 'waiting'
             AND id IN (
                 SELECT id FROM live_event_hubspot_delivery
                 WHERE event = ? AND opens = ?
             )""",
        (event, opens),
    )
    cur.execute(
        """UPDATE live_event_hubspot_delivery
           SET state = 'canceled', due = NULL
           WHERE event = ? AND opens = ? AND state = 'queued'""",
        (event, opens),
    )
    cur.execute(
        """DELETE FROM live_event_hubspot_outbound
           WHERE event = ? AND opens = ?""",
        (event, opens),
    )


def disconnect(con):
    event_hubspot_engagement.cancel(con)
    cur = con.cursor()
    cur.execute(
        """UPDATE action
           SET status = 'rejected', decision = 'canceled'
           WHERE status = 'waiting'
             AND id IN (SELECT id FROM live_event_hubspot_delivery)"""
    )
    cur.execute(
        """UPDATE live_event_hubspot_delivery
           SET state = 'canceled', due = NULL
           WHERE state = 'queued'"""
    )
    cur.execute("DELETE FROM live_event_hubspot_outbound")


def receipt(con, event, opens):
    plan = get(con, event, opens)
    if plan is None:
        return {"enabled": False, "states": {}}

    states = {}
    for row in con.execute(
        "SELECT CASE WHEN d.state='queued' AND a.status='waiting' THEN 'waiting' "
        "ELSE d.state END AS status, COUNT(*) AS n "
        "FROM live_event_hubspot_delivery d "
        "LEFT JOIN action a ON a.id = d.id "
        "WHERE d.event=? AND d.opens=? AND d.generation=? "
        "GROUP BY 1",
        (event, opens, plan["generation"]),
    ):
        states[row["status"]] = row["n"]

    return {
        "enabled": True,
        "segment": plan["segment"],
        "subscription": plan["subscription"],
        "states": states,
        "engagement_event": event_hubspot_engagement.definition(con, plan["generation"]),
        "engagement_states": event_hubspot_engagement.status(con, plan["generation"]),
    }


def enqueue(con, action, plan, registrant, now):
    from uuid import uuid4

    delivery_id = uuid4().hex
    cur = con.execute(
        "INSERT OR IGNORE INTO live_event_hubspot_delivery "
        "(id, event, opens, generation, registrant, due, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            delivery_id,
            plan["event"],
            plan["opens"],
            plan["generation"],
            registrant,
            now,
            "queued",
        ),
    )
    if cur.rowcount:
        action(
            con,
            delivery_id,
            STEP,
            {
                "event": plan["event"],
                "occurrence": plan["opens"],
                "registrant": registrant,
            },
        )
        return delivery_id
    return None


def run_one(tx, connection, validate, stop, clock):
    import json

    from flask import abort
    from werkzeug.exceptions import HTTPException

    import hubspot_crm

    with tx() as con:
        job = con.execute(
            """SELECT d.* FROM live_event_hubspot_delivery d JOIN action a ON a.id=d.id
               WHERE d.state='queued' AND d.due<=? AND a.status IN ('eligible','rejected')
               ORDER BY d.due,d.id LIMIT 1""",
            (int(clock()),),
        ).fetchone()
    if job is None or stop.is_set():
        return False

    def scope(con):
        if stop.is_set():
            abort(423)
        approval = con.execute(
            "SELECT status FROM action WHERE id=?", (job["id"],)
        ).fetchone()
        if approval and approval["status"] == "waiting":
            abort(423)
        plan = get(con, job["event"], job["opens"])
        if (
            not approval
            or approval["status"] != "eligible"
            or not plan
            or plan["generation"] != job["generation"]
            or int(clock()) >= plan["deadline"]
        ):
            abort(409)
        registrant = con.execute(
            """SELECT email,answers FROM live_event_registrant
               WHERE id=? AND event=? AND opens=? AND source='form'""",
            (job["registrant"], job["event"], job["opens"]),
        ).fetchone()
        if not registrant:
            abort(409)
        connection.authorize(con, plan["connection"])
        validate(con, plan)
        return plan, registrant["email"], registrant["answers"]

    attempts = job["attempts"] + 1
    due = problem = plan = None
    try:
        with tx() as con:
            plan, email, answers = scope(con)
        rev = plan["connection"]

        def guard():
            with tx() as con:
                _, current_email, current_answers = scope(con)
                if (current_email, current_answers) != (email, answers):
                    abort(409)

        if hubspot_crm.opted_out(
            connection, rev, email, plan["subscription"], plan["business_unit"]
        ):
            state = "skipped"
        else:
            fields = json.loads(answers)
            properties = {
                "firstname": fields.get("first_name"),
                "lastname": fields.get("last_name"),
            }
            guard()
            contact = hubspot_crm.save_contact(
                connection, rev, email, properties, before_write=guard
            )
            hubspot_crm.add_to_segment(
                connection, rev, plan["segment"], contact, before_write=guard
            )
            state = "sent"
    except HTTPException as error:
        if error.code == 423 or stop.is_set():
            return False
        state = "canceled" if error.code in (404, 409) else "problem"
        if state == "problem":
            problem = f"Delivery failed ({error.code})."
        delays = (15, 60, 300)
        if plan and error.code in (429, 502, 503, 504) and attempts <= len(delays):
            retry_after = getattr(error, "retry_after", None)
            delay = max(
                delays[attempts - 1], retry_after if isinstance(retry_after, int) else 0
            )
            candidate = int(clock()) + delay
            if candidate < plan["deadline"]:
                state, due = "queued", candidate
    with tx() as con:
        con.execute(
            """UPDATE live_event_hubspot_delivery SET state=?,due=?,attempts=?,problem=?
               WHERE id=? AND state='queued'""",
            (state, due, attempts, problem, job["id"]),
        )
    return True


def start(app, tx, database, connection, validate, *, engagement_validate=None, clock=None):
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
                with FileLock(str(database) + ".hubspot-outbound.lock", timeout=0):
                    if run_one(tx, connection, validate, stop, clock):
                        continue
                    if (
                        engagement_validate is not None
                        and event_hubspot_engagement.run_one(
                            tx, connection, engagement_validate, stop, clock
                        )
                    ):
                        continue
                stop.wait(1)
            except Timeout:
                stop.wait(1)
            except Exception:
                app.logger.error(
                    "The HubSpot contact delivery queue could not be processed."
                )
                stop.wait(1)

    worker = Thread(
        target=_run,
        name="CuePorch HubSpot contact delivery",
        daemon=True,
    )
    worker.start()

    def close():
        stop.set()
        worker.join(8)

    return close
