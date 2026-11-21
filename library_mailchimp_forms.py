import hmac
import json
import time
from uuid import uuid4

from flask import abort
from itsdangerous import BadSignature, URLSafeTimedSerializer

import mailchimp_crm
from event_contact_delivery import STEP

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_mailchimp_form_plan (
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    generation TEXT NOT NULL,
    provider_revision TEXT NOT NULL,
    form_revision TEXT NOT NULL,
    destinations TEXT NOT NULL,
    PRIMARY KEY (kind, subject)
);

CREATE TABLE IF NOT EXISTS library_mailchimp_form_delivery (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    generation TEXT NOT NULL,
    audience TEXT NOT NULL,
    recipient_key TEXT NOT NULL,
    email TEXT,
    payload TEXT,
    due INTEGER,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE (generation, audience, recipient_key)
);

CREATE INDEX IF NOT EXISTS library_mailchimp_form_delivery_pending_idx
ON library_mailchimp_form_delivery (state, due, id);
"""


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def get(con, kind, subject):
    return con.execute(
        "SELECT kind, subject, generation, provider_revision, form_revision, destinations "
        "FROM library_mailchimp_form_plan "
        "WHERE kind = ? AND subject = ?",
        (kind, subject),
    ).fetchone()


def stop(con, kind, subject):
    con.execute(
        "UPDATE action SET status = 'rejected', decision = 'canceled' "
        "WHERE status IN ('waiting', 'eligible') "
        "AND id IN ("
        "SELECT id FROM library_mailchimp_form_delivery "
        "WHERE kind = ? AND subject = ? AND state = 'queued'"
        ")",
        (kind, subject),
    )
    con.execute(
        "UPDATE library_mailchimp_form_delivery "
        "SET state = 'canceled', due = NULL, email = NULL, payload = NULL "
        "WHERE kind = ? AND subject = ? AND state = 'queued'",
        (kind, subject),
    )
    con.execute(
        "DELETE FROM library_mailchimp_form_plan WHERE kind = ? AND subject = ?",
        (kind, subject),
    )


def disconnect(con):
    plans = con.execute(
        "SELECT kind, subject FROM library_mailchimp_form_plan"
    ).fetchall()
    for plan in plans:
        stop(con, plan["kind"], plan["subject"])


def save(con, kind, subject, provider_revision, form_revision, destinations):
    destinations_json = _canonical(destinations)
    prior = get(con, kind, subject)
    if (
        prior is not None
        and prior["provider_revision"] == provider_revision
        and prior["form_revision"] == form_revision
        and prior["destinations"] == destinations_json
    ):
        return prior

    stop(con, kind, subject)
    generation = str(uuid4())
    con.execute(
        "INSERT INTO library_mailchimp_form_plan "
        "(kind, subject, generation, provider_revision, form_revision, destinations) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            kind,
            subject,
            generation,
            provider_revision,
            form_revision,
            destinations_json,
        ),
    )
    return get(con, kind, subject)


def enqueue(con, action, plan, email, answers, now, signing_key):
    recipient_key = hmac.digest(
        signing_key,
        email.strip().casefold().encode("utf-8"),
        "sha256",
    ).hex()
    destinations = json.loads(plan["destinations"])
    inserted = 0
    for destination in destinations:
        audience = destination["audience"]
        payload = _canonical(
            mailchimp_crm.mapped_values(answers, destination["mapping"])
        )
        job_id = str(uuid4())
        cursor = con.execute(
            "INSERT OR IGNORE INTO library_mailchimp_form_delivery "
            "(id, kind, subject, generation, audience, recipient_key, email, payload, due, state, attempts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0)",
            (
                job_id,
                plan["kind"],
                plan["subject"],
                plan["generation"],
                audience,
                recipient_key,
                email,
                payload,
                now,
            ),
        )
        if cursor.rowcount:
            inserted += 1
            action(
                con,
                job_id,
                STEP,
                {
                    "provider": "mailchimp",
                    "kind": plan["kind"],
                    "subject": plan["subject"],
                    "audience": audience,
                },
            )
    return inserted


def next_job(con, now):
    row = con.execute(
        """
        SELECT d.*
        FROM library_mailchimp_form_delivery d
        JOIN action a ON a.id = d.id
        WHERE d.state = 'queued' AND d.due <= ?
          AND a.status IN ('eligible', 'rejected')
        ORDER BY d.due, d.id
        LIMIT 1
        """,
        (now,),
    ).fetchone()
    return dict(row) if row else None


def finish(con, job_id, state, due, attempts):
    if state == "queued":
        con.execute(
            "UPDATE library_mailchimp_form_delivery SET state=?, due=?, attempts=? WHERE id=? AND state='queued'",
            (state, due, attempts, job_id),
        )
    else:
        con.execute(
            "UPDATE library_mailchimp_form_delivery SET state=?, due=?, attempts=?, email=NULL, payload=NULL WHERE id=? AND state='queued'",
            (state, due, attempts, job_id),
        )


def scope(con, job, connection, validate, stop_event):
    if stop_event.is_set():
        abort(423)

    row = con.execute(
        """
        SELECT d.*, a.status AS action_status
        FROM library_mailchimp_form_delivery d
        LEFT JOIN action a ON a.id = d.id
        WHERE d.id = ?
        """,
        (job["id"],),
    ).fetchone()
    if not row:
        abort(409)
    row = dict(row)

    action_status = row.get("action_status")
    if action_status == "waiting":
        abort(423)
    if row["state"] != "queued" or action_status != "eligible":
        abort(409)

    plan = get(con, row["kind"], row["subject"])
    if not plan:
        abort(409)
    plan = dict(plan)
    if plan["generation"] != row["generation"]:
        abort(409)

    connection.authorize(con, plan["provider_revision"])

    form = validate(con, row["kind"], row["subject"])
    if not form:
        abort(404)
    if form.get("revision") != plan["form_revision"]:
        abort(409)

    destinations = json.loads(plan["destinations"])
    if not any(d["audience"] == row["audience"] for d in destinations):
        abort(409)

    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        abort(409)

    return plan["provider_revision"], row["audience"], row["email"], payload


def deliver(tx, job, connection, validate, stop):
    with tx() as con:
        revision, audience, email, payload = scope(con, job, connection, validate, stop)

    def before_write():
        with tx() as con:
            scope(con, job, connection, validate, stop)

    result = mailchimp_crm.save_member(
        connection,
        revision,
        audience,
        email,
        payload,
        before_write=before_write,
    )
    return "sent" if result.get("result") == "delivered" else "skipped"


def start(app, tx, database, connection, validate):
    if not connection.configured:
        return lambda: None

    from time import time
    import event_contact_delivery

    def tick(stop_event):
        return event_contact_delivery.run_job(
            tx,
            next_job,
            lambda job: deliver(tx, job, connection, validate, stop_event),
            finish,
            stop_event,
            time,
        )

    return event_contact_delivery._start_worker(app, database, "mailchimp-forms", tick)


class Consent:
    def __init__(self, connection, action, operator):
        self.connection = connection
        self.action = action
        self.signing_key = hmac.digest(
            operator.encode(),
            b"CuePorch Mailchimp form consent",
            "sha256",
        )
        self.signer = URLSafeTimedSerializer(
            self.signing_key,
            salt="CuePorch Mailchimp form consent",
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
                "mailchimp-form",
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
            "mailchimp-form",
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
    entries = body.get("destinations")
    if not isinstance(entries, list) or not entries:
        abort(422, "Choose at least one Mailchimp audience.")

    requested = {}
    for entry in entries:
        if not isinstance(entry, dict):
            abort(422)
        audience = entry.get("audience")
        mapping = entry.get("mapping", {})
        if not mailchimp_crm.audience_id(audience) or not isinstance(mapping, dict):
            abort(422)
        if audience in requested:
            abort(422, "Choose each audience once.")
        requested[audience] = mapping

    revision = connection.revision()
    with tx() as con:
        form = validate(con, kind, uid)
        if form is None:
            abort(409, "Enable registration before configuring delivery.")
        prior = get(con, kind, uid)

    destinations = []
    for audience, mapping in sorted(requested.items()):
        policy = mailchimp_crm.audience(connection, revision, audience)
        if policy["marketing_permissions"]:
            abort(409, "This audience requires marketing permission mapping.")
        fields = mailchimp_crm.audience_schema(connection, revision, audience)
        try:
            saved_mapping = mailchimp_crm.validate_mapping(form, mapping, fields)
        except ValueError:
            abort(422, "Check the required mappings for the selected audience.")
        destinations.append({"audience": audience, "mapping": saved_mapping})

    with tx() as con:
        connection.authorize(con, revision)
        current = validate(con, kind, uid)
        if (
            not current
            or current["revision"] != form["revision"]
            or get(con, kind, uid) != prior
        ):
            abort(
                409, "The form or its Mailchimp delivery changed. Reopen the settings."
            )
        save(con, kind, uid, revision, form["revision"], destinations)


def receipt(con, connection, kind, subject, form):
    plan = get(con, kind, subject)
    conn_row = connection.connection(con)
    available = bool(connection.configured and conn_row)

    enabled = bool(
        available
        and plan is not None
        and form
        and plan["provider_revision"] == conn_row["revision"]
        and plan["form_revision"] == form["revision"]
    )

    if enabled:
        state = "connected"
    elif plan is not None:
        state = "stale"
    else:
        state = "off"

    destinations = []
    if enabled:
        dests = json.loads(plan["destinations"])

        rows = con.execute(
            """
            SELECT d.audience AS audience,
                   CASE WHEN d.state = 'queued' AND a.status = 'waiting'
                        THEN 'waiting' ELSE d.state END AS state,
                   COUNT(*) AS count
            FROM library_mailchimp_form_delivery d
            LEFT JOIN action a ON a.id = d.id
            WHERE d.generation = ?
            GROUP BY d.audience,
                     CASE WHEN d.state = 'queued' AND a.status = 'waiting'
                          THEN 'waiting' ELSE d.state END
            """,
            (plan["generation"],),
        ).fetchall()

        by_audience = {}
        for row in rows:
            by_audience.setdefault(row["audience"], {})[row["state"]] = row["count"]

        for dest in dests:
            audience = dest["audience"]
            mapping = {key: meta["tag"] for key, meta in dest["mapping"].items()}
            destinations.append(
                {
                    "audience": audience,
                    "mapping": mapping,
                    "states": by_audience.get(audience, {}),
                }
            )

    return {
        "available": available,
        "enabled": enabled,
        "state": state,
        "destinations": destinations,
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
        "/api/library/work/<uid>/registration/mailchimp/outbound",
        defaults={"kind": "video"},
        methods=["GET", "PUT", "DELETE"],
    )
    @app.route(
        "/api/showcases/<uid>/registration/mailchimp/outbound",
        defaults={"kind": "showcase"},
        methods=["GET", "PUT", "DELETE"],
    )
    def mailchimp_forms(uid, kind):
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
