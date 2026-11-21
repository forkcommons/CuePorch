import hmac
import time
from uuid import uuid4

from flask import abort
from itsdangerous import BadSignature, URLSafeTimedSerializer

import constant_contact_crm
import event_contact_delivery

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_constant_contact_form_plan (
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    generation TEXT NOT NULL,
    provider_revision TEXT NOT NULL,
    form_revision TEXT NOT NULL,
    list_id TEXT NOT NULL,
    list_name TEXT NOT NULL,
    PRIMARY KEY (kind, subject)
);

CREATE TABLE IF NOT EXISTS library_constant_contact_form_delivery (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    generation TEXT NOT NULL,
    recipient_key TEXT NOT NULL,
    email TEXT,
    first_name TEXT,
    last_name TEXT,
    due INTEGER,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    write_started INTEGER NOT NULL DEFAULT 0,
    activity_id TEXT,
    poll_deadline INTEGER,
    UNIQUE (generation, recipient_key)
);

CREATE INDEX IF NOT EXISTS library_constant_contact_form_due
ON library_constant_contact_form_delivery (state, due);
"""


def get(con, kind, subject):
    return con.execute(
        "SELECT * FROM library_constant_contact_form_plan WHERE kind=? AND subject=?",
        (kind, subject),
    ).fetchone()


def next_job(con, now):
    row = con.execute(
        """
        SELECT d.*, a.status AS action_status
        FROM library_constant_contact_form_delivery AS d
        JOIN action AS a ON a.id = d.id
        WHERE d.state IN ('queued', 'submitted')
          AND d.due <= ?
          AND a.status IN ('eligible', 'rejected')
        ORDER BY d.due, d.id
        LIMIT 1
        """,
        (now,),
    ).fetchone()
    return dict(row) if row is not None else None


def scope(con, job, connection, validate, stop):
    if stop.is_set():
        abort(423)
    row = con.execute(
        """
        SELECT d.*, a.status AS action_status
        FROM library_constant_contact_form_delivery AS d
        JOIN action AS a ON a.id = d.id
        WHERE d.id = ?
        """,
        (job["id"],),
    ).fetchone()
    if row is None or row["state"] not in ("queued", "submitted"):
        abort(409)
    if row["action_status"] == "waiting":
        abort(423)
    if row["action_status"] != "eligible":
        abort(409)

    plan = get(con, row["kind"], row["subject"])
    if plan is None or row["generation"] != plan["generation"]:
        abort(409)
    form = validate(con, row["kind"], row["subject"])
    if form is None:
        abort(404)
    if form["revision"] != plan["form_revision"]:
        abort(409)
    connection.authorize(con, plan["provider_revision"])
    return dict(row), plan


def save(con, kind, subject, provider_revision, form_revision, destination):
    row = get(con, kind, subject)
    list_id, list_name = destination["id"], destination["name"]
    if (row
            and row["provider_revision"] == provider_revision
            and row["form_revision"] == form_revision
            and row["list_id"] == list_id):
        con.execute(
            "UPDATE library_constant_contact_form_plan SET list_name=? WHERE kind=? AND subject=?",
            (list_name, kind, subject),
        )
        return
    stop(con, kind, subject)
    con.execute(
        "INSERT INTO library_constant_contact_form_plan "
        "(kind, subject, generation, provider_revision, form_revision, list_id, list_name) "
        "VALUES (?,?,?,?,?,?,?)",
        (kind, subject, uuid4().hex, provider_revision, form_revision, list_id, list_name),
    )


def stop(con, kind, subject):
    cur = con.cursor()

    # 1) Reject matching actions
    cur.execute(
        """
        UPDATE action
           SET status   = 'rejected',
               decision = 'canceled'
         WHERE status IN ('waiting', 'eligible')
           AND id IN (
                SELECT id
                  FROM library_constant_contact_form_delivery
                 WHERE kind    = ?
                   AND subject = ?
                   AND state IN ('queued', 'submitted')
           )
        """,
        (kind, subject),
    )

    # 2) Cancel/track-stop the pending delivery rows and clear PII
    cur.execute(
        """
        UPDATE library_constant_contact_form_delivery
           SET state         = CASE
                                   WHEN activity_id IS NOT NULL
                                     OR write_started = 1
                                   THEN 'tracking_stopped'
                                   ELSE 'canceled'
                               END,
               email         = NULL,
               first_name    = NULL,
               last_name     = NULL,
               due           = NULL,
               poll_deadline = NULL
         WHERE kind    = ?
           AND subject = ?
           AND state IN ('queued', 'submitted')
        """,
        (kind, subject),
    )

    # 3) Remove the scoped plan row
    cur.execute(
        """
        DELETE FROM library_constant_contact_form_plan
         WHERE kind    = ?
           AND subject = ?
        """,
        (kind, subject),
    )


def disconnect(con):
    cur = con.cursor()
    cur.execute(
        """
        SELECT kind, subject
          FROM library_constant_contact_form_plan
        """
    )
    for kind, subject in cur.fetchall():
        stop(con, kind, subject)


def enqueue(con, action, plan, email, answers, now, signing_key):
    if not isinstance(email, str):
        return None
    email_s = email.strip()
    if not email_s or len(email_s) > 80:
        return None

    def _name(key):
        v = answers.get(key)
        if not isinstance(v, str):
            return None
        v = v.strip()
        if not v or len(v) > 50 or not v.isprintable():
            return None
        return v

    first_name = _name("first_name")
    last_name = _name("last_name")
    if first_name is None or last_name is None:
        return None

    recipient_key = hmac.digest(
        signing_key, email_s.casefold().encode("utf-8"), "sha256"
    ).hex()
    job_id = uuid4().hex

    cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO library_constant_contact_form_delivery "
        "(id, kind, subject, generation, recipient_key, email, first_name, last_name, due, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            plan["kind"],
            plan["subject"],
            plan["generation"],
            recipient_key,
            email_s,
            first_name,
            last_name,
            now,
            "queued",
        ),
    )

    if cur.rowcount:
        action(
            con,
            job_id,
            event_contact_delivery.STEP,
            {
                "provider": "constant-contact",
                "kind": plan["kind"],
                "subject": plan["subject"],
                "list_id": plan["list_id"],
            },
        )
        return job_id

    return None


class Consent:
    def __init__(self, connection, action, operator):
        self.connection = connection
        self.action = action
        self.signing_key = hmac.digest(
            operator.encode(),
            b"CuePorch Constant Contact form consent",
            "sha256",
        )
        self.signer = URLSafeTimedSerializer(
            self.signing_key,
            salt="CuePorch Constant Contact form consent",
        )

    def _plan(self, con, kind, subject, form):
        if kind not in ("video", "showcase") or not isinstance(form, dict):
            return None
        if not self.connection.configured:
            return None
        row = self.connection.connection(con)
        plan = get(con, kind, subject)
        if not row or not plan:
            return None
        if plan["provider_revision"] != row["revision"]:
            return None
        if plan["form_revision"] != form["revision"]:
            return None
        return plan

    def offer(self, con, kind, subject, form, scope):
        plan = self._plan(con, kind, subject, form)
        if not plan:
            return ""
        return self.signer.dumps(
            [
                "constant-contact-form",
                kind,
                subject,
                scope,
                plan["generation"],
            ]
        )

    def capture(self, con, kind, subject, form, scope, ticket):
        plan = self._plan(con, kind, subject, form)
        if not plan or not ticket:
            return None
        payload = [
            "constant-contact-form",
            kind,
            subject,
            scope,
            plan["generation"],
        ]
        try:
            if self.signer.loads(ticket, max_age=1200) != payload:
                return None
        except BadSignature:
            return None

        def on_record(email, answers):
            if not email:
                return
            enqueue(
                con,
                self.action,
                plan,
                email,
                answers,
                int(time.time()),
                self.signing_key,
            )

        return on_record

def configure(tx, connection, validate, kind, uid, body):
    revision = connection.revision()
    with tx() as con:
        list_id = body.get("list_id")
        if not constant_contact_crm.list_id(list_id):
            abort(422, "Choose a Constant Contact list.")
        form = validate(con, kind, uid)
        if (not form or form["first_name"] == "off" or
                form["last_name"] == "off"):
            abort(409, "Enable the form with first and last name fields.")
        prior = get(con, kind, uid)

    destination = constant_contact_crm.contact_list(connection, revision, list_id)

    with tx() as con:
        connection.authorize(con, revision)
        form2 = validate(con, kind, uid)
        if (get(con, kind, uid) != prior or not form2 or
                form2["revision"] != form["revision"]):
            abort(409, "The form was changed concurrently. Please try again.")
        save(con, kind, uid, revision, form["revision"], destination)


def receipt(con, connection, kind, subject, form):
    plan = get(con, kind, subject)
    conn_row = connection.connection(con)
    available = bool(connection.configured and conn_row)
    enabled = bool(
        available
        and plan
        and form
        and plan["form_revision"] == form["revision"]
        and plan["provider_revision"] == conn_row["revision"]
    )
    if not enabled:
        return {
            "available": available,
            "enabled": enabled,
            "state": "stale" if plan else "off",
            "list_id": None,
            "list_name": None,
            "states": {},
        }
    rows = con.execute(
        "SELECT CASE WHEN d.state='queued' AND a.status='waiting' THEN 'waiting' "
        "ELSE d.state END AS receipt_state, COUNT(*) AS count "
        "FROM library_constant_contact_form_delivery d "
        "LEFT JOIN action a ON a.id=d.id WHERE d.generation=? GROUP BY receipt_state",
        (plan["generation"],)).fetchall()
    return {
        "available": available,
        "enabled": enabled,
        "state": "connected",
        "list_id": plan["list_id"],
        "list_name": plan["list_name"],
        "states": {r["receipt_state"]: r["count"] for r in rows},
    }


def register_routes(app, tx, staff, origin, connection, validate):
    from flask import request
    from browser_account_security import (
        json_mutation,
        private,
        same_origin,
        secure_site,
    )

    @app.route(
        "/api/library/work/<uid>/registration/constant-contact/outbound",
        defaults={"kind": "video"},
        methods=["GET", "PUT", "DELETE"],
    )
    @app.route(
        "/api/showcases/<uid>/registration/constant-contact/outbound",
        defaults={"kind": "showcase"},
        methods=["GET", "PUT", "DELETE"],
    )
    def constant_contact_forms(uid, kind):
        staff()
        secure = secure_site(origin)
        if request.method == "GET":
            same_origin(secure)
        else:
            body = json_mutation(secure, "X-CuePorch-Integration")
            if request.method == "PUT":
                configure(tx, connection, validate, kind, uid, body)
        with tx() as con:
            form = validate(con, kind, uid)
            if request.method == "DELETE":
                stop(con, kind, uid)
            return private(receipt(con, connection, kind, uid, form))

def _confirmed_membership(record, list_id):
    """Classify read-contact evidence for a single-list delivery."""
    if record is None:
        return None
    if record["pending"]:
        return "pending_confirmation"
    if not record["eligible"]:
        return "skipped"
    if list_id in record["memberships"]:
        return "sent"
    return None


def _follow_delivery(row, plan, connection, now):
    """Resolve the delivery state for a submission via read-only provider reads."""
    activity_id = row["activity_id"]
    revision = plan["provider_revision"]
    list_id = plan["list_id"]

    if activity_id and now >= row["poll_deadline"]:
        return "unconfirmed"

    if activity_id:
        status = constant_contact_crm.activity_status(
            connection, revision, activity_id
        )
        if status in ("submitted", "problem"):
            return status

    record = constant_contact_crm.read_contact(
        connection, revision, row["email"]
    )
    outcome = _confirmed_membership(record, list_id)
    if outcome is not None:
        return outcome

    if activity_id:
        return "submitted"
    return "unconfirmed"


def deliver(tx, job, connection, validate, stop_event, clock):
    from werkzeug.exceptions import TooManyRequests

    with tx() as con:
        row, plan = scope(con, job, connection, validate, stop_event)

    if row["activity_id"] or row["write_started"]:
        return _follow_delivery(row, plan, connection, int(clock()))

    marker_set = False

    def before_send():
        nonlocal marker_set
        with tx() as con:
            scope(con, job, connection, validate, stop_event)
            # at-most-once recovery marker: persist intent before network send
            con.execute(
                "UPDATE library_constant_contact_form_delivery SET write_started=1 "
                "WHERE id=? AND state='queued'",
                (row["id"],)
            )
        marker_set = True

    try:
        result = constant_contact_crm.send_registration(
            connection,
            plan["provider_revision"],
            plan["list_id"],
            row["email"],
            row["first_name"],
            row["last_name"],
            before_send=before_send
        )
    except TooManyRequests:
        if marker_set:
            with tx() as con:
                con.execute(
                    "UPDATE library_constant_contact_form_delivery SET write_started=0 "
                    "WHERE id=? AND state='queued' AND activity_id IS NULL",
                    (row["id"],)
                )
        raise

    if result["state"] == "submitted":
        with tx() as con:
            # activity id checkpoint: record submission so polling can recover
            con.execute(
                "UPDATE library_constant_contact_form_delivery SET activity_id=?, poll_deadline=? "
                "WHERE id=? AND state='queued' AND activity_id IS NULL",
                (result["activity_id"], int(clock()) + 1800, row["id"])
            )

    return result["state"]


def run_one(tx, connection, validate, stop_event, clock=time.time):
    def finish(con, job_id, state, due, attempts):
        if state == "submitted":
            con.execute(
                "UPDATE library_constant_contact_form_delivery "
                "SET state='submitted', due=?, attempts=0 "
                "WHERE id=? AND state IN ('queued','submitted')",
                (int(clock()) + 30, job_id),
            )
        elif state == "queued":
            con.execute(
                "UPDATE library_constant_contact_form_delivery "
                "SET due=?, attempts=? "
                "WHERE id=? AND state IN ('queued','submitted')",
                (due, attempts, job_id),
            )
        else:
            con.execute(
                "UPDATE library_constant_contact_form_delivery "
                "SET state=?, attempts=?, due=NULL, email=NULL, first_name=NULL, "
                "last_name=NULL, poll_deadline=NULL "
                "WHERE id=? AND state IN ('queued','submitted')",
                (state, attempts, job_id),
            )

    return event_contact_delivery.run_job(
        tx,
        next_job,
        lambda job: deliver(tx, job, connection, validate, stop_event, clock),
        finish,
        stop_event,
        clock,
    )


def start(app, tx, database, connection, validate):
    if not connection.configured:
        return lambda: None
    return event_contact_delivery._start_worker(
        app,
        database,
        "constant-contact-forms",
        lambda stop_event: run_one(tx, connection, validate, stop_event),
    )
