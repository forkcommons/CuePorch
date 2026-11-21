import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from uuid import uuid4

from flask import abort
from itsdangerous import BadSignature, URLSafeTimedSerializer

import campaign_monitor_crm
import constant_contact_crm
import event_contact_delivery
import keap_crm


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    table: str
    adapter: ModuleType
    list_id: Callable[[object], bool]
    email_limit: int
    name_limit: int

    @property
    def plan(self) -> str:
        return self.table + "_plan"

    @property
    def delivery(self) -> str:
        return self.table + "_delivery"


CONSTANT_CONTACT = Provider(
    "constant-contact",
    "Constant Contact",
    "library_constant_contact_form",
    constant_contact_crm,
    constant_contact_crm.list_id,
    80,
    50,
)
CAMPAIGN_MONITOR = Provider(
    "campaign-monitor",
    "Campaign Monitor",
    "library_campaign_monitor_form",
    campaign_monitor_crm,
    campaign_monitor_crm.identifier,
    254,
    80,
)
KEAP = Provider("keap", "Keap", "library_keap_form", keap_crm, keap_crm._valid_id, 75, 40)
PROVIDERS = (CONSTANT_CONTACT, CAMPAIGN_MONITOR, KEAP)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {plan} (
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    generation TEXT NOT NULL,
    provider_revision TEXT NOT NULL,
    form_revision TEXT NOT NULL,
    list_id TEXT NOT NULL,
    list_name TEXT NOT NULL,
    PRIMARY KEY (kind, subject)
);

CREATE TABLE IF NOT EXISTS {delivery} (
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

CREATE INDEX IF NOT EXISTS {prefix}_due
ON {delivery} (state, due);
"""

SCHEMA = "".join(
    _SCHEMA.format(
        plan=provider.plan,
        delivery=provider.delivery,
        prefix=provider.table,
    )
    for provider in PROVIDERS
)


def get(con, kind, subject, provider=CONSTANT_CONTACT):
    return con.execute(
        f"SELECT * FROM {provider.plan} WHERE kind=? AND subject=?",
        (kind, subject),
    ).fetchone()


def next_job(con, now, provider=CONSTANT_CONTACT):
    row = con.execute(
        f"""
        SELECT d.*, a.status AS action_status
        FROM {provider.delivery} AS d
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


def scope(con, job, connection, validate, stop, provider=CONSTANT_CONTACT):
    if stop.is_set():
        abort(423)
    row = con.execute(
        f"""
        SELECT d.*, a.status AS action_status
        FROM {provider.delivery} AS d
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

    plan = get(con, row["kind"], row["subject"], provider)
    if plan is None or row["generation"] != plan["generation"]:
        abort(409)
    form = validate(con, row["kind"], row["subject"])
    if form is None:
        abort(404)
    if form["revision"] != plan["form_revision"]:
        abort(409)
    connection.authorize(con, plan["provider_revision"])
    return dict(row), plan


def save(con, kind, subject, provider_revision, form_revision, destination, provider=CONSTANT_CONTACT):
    row = get(con, kind, subject, provider)
    list_id, list_name = destination["id"], destination["name"]
    if (row
            and row["provider_revision"] == provider_revision
            and row["form_revision"] == form_revision
            and row["list_id"] == list_id):
        con.execute(
            f"UPDATE {provider.plan} SET list_name=? WHERE kind=? AND subject=?",
            (list_name, kind, subject),
        )
        return
    stop(con, kind, subject, provider)
    con.execute(
        f"INSERT INTO {provider.plan} "
        "(kind, subject, generation, provider_revision, form_revision, list_id, list_name) "
        "VALUES (?,?,?,?,?,?,?)",
        (kind, subject, uuid4().hex, provider_revision, form_revision, list_id, list_name),
    )


def stop(con, kind, subject, provider=CONSTANT_CONTACT):
    cur = con.cursor()

    # 1) Reject matching actions
    cur.execute(
        f"""
        UPDATE action
           SET status   = 'rejected',
               decision = 'canceled'
         WHERE status IN ('waiting', 'eligible')
           AND id IN (
                SELECT id
                  FROM {provider.delivery}
                 WHERE kind    = ?
                   AND subject = ?
                   AND state IN ('queued', 'submitted')
           )
        """,
        (kind, subject),
    )

    # 2) Cancel/track-stop the pending delivery rows and clear PII
    cur.execute(
        f"""
        UPDATE {provider.delivery}
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
        f"""
        DELETE FROM {provider.plan}
         WHERE kind    = ?
           AND subject = ?
        """,
        (kind, subject),
    )


def stop_all(con, kind, subject):
    for provider in PROVIDERS:
        stop(con, kind, subject, provider)


def disconnect(con, provider=CONSTANT_CONTACT):
    cur = con.cursor()
    cur.execute(
        f"""
        SELECT kind, subject
          FROM {provider.plan}
        """
    )
    for kind, subject in cur.fetchall():
        stop(con, kind, subject, provider)


def enqueue(con, action, plan, email, answers, now, signing_key, provider=CONSTANT_CONTACT):
    if not isinstance(email, str):
        return None
    email_s = email.strip()
    if not email_s or len(email_s) > provider.email_limit:
        return None

    def _name(key):
        v = answers.get(key)
        if not isinstance(v, str):
            return None
        v = v.strip()
        if not v or len(v) > provider.name_limit or not v.isprintable():
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
        f"INSERT OR IGNORE INTO {provider.delivery} "
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
                "provider": provider.key,
                "kind": plan["kind"],
                "subject": plan["subject"],
                "list_id": plan["list_id"],
            },
        )
        return job_id

    return None


class Consent:
    def __init__(self, connection, action, operator, provider=CONSTANT_CONTACT):
        self.connection = connection
        self.action = action
        self.provider = provider
        salt = f"CuePorch {provider.label} form consent"
        self.signing_key = hmac.digest(
            operator.encode(),
            salt.encode(),
            "sha256",
        )
        self.signer = URLSafeTimedSerializer(
            self.signing_key,
            salt=salt,
        )

    def _plan(self, con, kind, subject, form):
        if kind not in ("video", "showcase") or not isinstance(form, dict):
            return None
        if not self.connection.configured:
            return None
        row = self.connection.connection(con)
        plan = get(con, kind, subject, provider=self.provider)
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
                f"{self.provider.key}-form",
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
            f"{self.provider.key}-form",
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
                provider=self.provider,
            )

        return on_record


def configure(tx, connection, validate, kind, uid, body, provider=CONSTANT_CONTACT):
    revision = connection.revision()
    with tx() as con:
        list_id = body.get("list_id")
        if not provider.list_id(list_id):
            abort(422, f"Choose a {provider.label} list.")
        form = validate(con, kind, uid)
        if (not form or form["first_name"] == "off" or
                form["last_name"] == "off"):
            abort(409, "Enable the form with first and last name fields.")
        prior = get(con, kind, uid, provider=provider)

    destination = provider.adapter.contact_list(connection, revision, list_id)

    with tx() as con:
        connection.authorize(con, revision)
        form2 = validate(con, kind, uid)
        if (get(con, kind, uid, provider=provider) != prior or not form2 or
                form2["revision"] != form["revision"]):
            abort(409, "The form was changed concurrently. Please try again.")
        save(con, kind, uid, revision, form["revision"], destination, provider=provider)


def receipt(con, connection, kind, subject, form, provider=CONSTANT_CONTACT):
    plan = get(con, kind, subject, provider=provider)
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
        f"FROM {provider.delivery} d "
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


def register_routes(app, tx, staff, origin, connection, validate, provider=CONSTANT_CONTACT):
    from flask import request
    from browser_account_security import (
        json_mutation,
        private,
        same_origin,
        secure_site,
    )

    @app.route(
        f"/api/library/work/<uid>/registration/{provider.key}/outbound",
        defaults={"kind": "video"},
        endpoint=f"{provider.key}-video-registration-delivery",
        methods=["GET", "PUT", "DELETE"],
    )
    @app.route(
        f"/api/showcases/<uid>/registration/{provider.key}/outbound",
        defaults={"kind": "showcase"},
        endpoint=f"{provider.key}-showcase-registration-delivery",
        methods=["GET", "PUT", "DELETE"],
    )
    def form_delivery(uid, kind):
        staff()
        secure = secure_site(origin)
        if request.method == "GET":
            same_origin(secure)
        else:
            body = json_mutation(secure, "X-CuePorch-Integration")
            if request.method == "PUT":
                configure(tx, connection, validate, kind, uid, body, provider=provider)
        with tx() as con:
            form = validate(con, kind, uid)
            if request.method == "DELETE":
                stop(con, kind, uid, provider=provider)
            return private(receipt(con, connection, kind, uid, form, provider=provider))


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


def _follow_delivery(row, plan, connection, now, provider):
    """Resolve the delivery state for a submission via read-only provider reads."""
    activity_id = row["activity_id"]
    revision = plan["provider_revision"]
    list_id = plan["list_id"]

    if row["poll_deadline"] is not None and now >= row["poll_deadline"]:
        return "unconfirmed"

    if provider is CAMPAIGN_MONITOR:
        outcome = provider.adapter.read_registration(
            connection, revision, list_id, row["email"]
        )
        if outcome is not None:
            return outcome
        if row["poll_deadline"] is not None:
            return "submitted"
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


def deliver(tx, job, connection, validate, stop_event, clock, provider=CONSTANT_CONTACT):
    from werkzeug.exceptions import TooManyRequests

    with tx() as con:
        row, plan = scope(con, job, connection, validate, stop_event, provider=provider)

    if provider is KEAP:
        return _deliver_keap(tx, job, row, plan, connection, validate, stop_event, clock, provider)

    if row["activity_id"] or row["write_started"]:
        return _follow_delivery(row, plan, connection, int(clock()), provider)

    marker_set = False

    def before_send():
        nonlocal marker_set
        with tx() as con:
            scope(con, job, connection, validate, stop_event, provider=provider)
            # at-most-once recovery marker: persist intent before network send
            con.execute(
                f"UPDATE {provider.delivery} SET write_started=1 "
                "WHERE id=? AND state='queued'",
                (row["id"],)
            )
        marker_set = True

    try:
        result = provider.adapter.send_registration(
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
                    f"UPDATE {provider.delivery} SET write_started=0 "
                    "WHERE id=? AND state='queued' AND activity_id IS NULL",
                    (row["id"],)
                )
        raise

    if isinstance(result, str):
        state = result
        activity_id = None
    else:
        state = result["state"]
        activity_id = result.get("activity_id")

    if state == "submitted":
        with tx() as con:
            # activity id checkpoint: record submission so polling can recover
            con.execute(
                f"UPDATE {provider.delivery} SET activity_id=?, poll_deadline=? "
                "WHERE id=? AND state='queued' AND activity_id IS NULL",
                (activity_id, int(clock()) + 1800, row["id"])
            )

    return state


def run_one(tx, connection, validate, stop_event, clock=time.time, provider=CONSTANT_CONTACT):
    def finish(con, job_id, state, due, attempts):
        if state == "submitted":
            con.execute(
                f"UPDATE {provider.delivery} "
                "SET state='submitted', due=?, attempts=0 "
                "WHERE id=? AND state IN ('queued','submitted')",
                (int(clock()) + 30, job_id),
            )
        elif state == "queued":
            con.execute(
                f"UPDATE {provider.delivery} "
                "SET due=?, attempts=? "
                "WHERE id=? AND state IN ('queued','submitted')",
                (due, attempts, job_id),
            )
        else:
            con.execute(
                f"UPDATE {provider.delivery} "
                "SET state=?, attempts=?, due=NULL, email=NULL, first_name=NULL, "
                "last_name=NULL, poll_deadline=NULL "
                "WHERE id=? AND state IN ('queued','submitted')",
                (state, attempts, job_id),
            )

    return event_contact_delivery.run_job(
        tx,
        lambda con, now: next_job(con, now, provider=provider),
        lambda job: deliver(tx, job, connection, validate, stop_event, clock, provider=provider),
        finish,
        stop_event,
        clock,
    )


def start(app, tx, database, connection, validate, provider=CONSTANT_CONTACT):
    if not connection.configured:
        return lambda: None
    return event_contact_delivery._start_worker(
        app,
        database,
        provider.key + "-forms",
        lambda stop_event: run_one(
            tx, connection, validate, stop_event, provider=provider
        ),
    )

def _keap_write(tx, job, row, connection, validate, stop_event, clock, provider, stage, contact_id, send):
    from werkzeug.exceptions import HTTPException

    marker_set = False

    def before_send():
        nonlocal marker_set
        with tx() as con:
            current, _ = scope(con, job, connection, validate, stop_event, provider=provider)
            expected_stage = stage if marker_set else row["write_started"]
            if current["write_started"] != expected_stage:
                abort(423)
            if not marker_set:
                con.execute(
                    f"UPDATE {provider.delivery} "
                    "SET write_started=?, activity_id=?, poll_deadline=? "
                    "WHERE id=?",
                    (stage, contact_id, int(clock()) + 1800, row["id"]),
                )
        marker_set = True

    try:
        return send(before_send)
    except HTTPException as exc:
        if exc.code == 429:
            if marker_set:
                with tx() as con:
                    con.execute(
                        f"UPDATE {provider.delivery} "
                        "SET write_started=?, activity_id=?, poll_deadline=? "
                        "WHERE id=? AND write_started=? "
                        "AND state IN ('queued','submitted')",
                        (
                            row["write_started"],
                            row["activity_id"],
                            row["poll_deadline"],
                            row["id"],
                            stage,
                        ),
                    )
            raise
        if marker_set and exc.code in (409, 502, 503, 504):
            return None
        raise

def _deliver_keap(tx, job, row, plan, connection, validate, stop_event, clock, provider):
    from werkzeug.exceptions import HTTPException

    revision = plan['provider_revision']
    tag_id = plan['list_id']
    email = row['email']
    stage = row['write_started'] or 0
    cid = row['activity_id']
    deadline = row['poll_deadline']

    if deadline is not None and clock() >= deadline:
        return 'unconfirmed'

    try:
        if cid:
            contact = keap_crm.read_contact(connection, revision, cid, email)
        else:
            contact = keap_crm.find_contact(connection, revision, email)
    except HTTPException as exc:
        if exc.code in (404, 502, 503, 504) and stage != 0:
            return 'submitted'
        raise

    if contact is not None and not contact.get('primary'):
        return 'skipped'

    if stage == 2:
        if (contact is not None
                and cid is not None
                and contact.get('id') == cid
                and tag_id in (contact.get('tag_ids') or [])):
            return 'sent'
        return 'submitted'

    opt_in = None
    if stage in (0, 1):
        opt_in = keap_crm.marketing_opt_in(connection, revision, email)
        if opt_in is False:
            return 'skipped'

    if contact is not None and tag_id in (contact.get('tag_ids') or []):
        return 'sent'

    if contact is not None:
        contact_id = contact['id']

        def send(before_send):
            return keap_crm.apply_tag(
                connection, revision, tag_id, contact_id,
                before_send=before_send,
            )

        _keap_write(tx, job, row, connection, validate, stop_event, clock,
                    provider, 2, contact_id, send)
        return 'submitted'

    if stage == 1:
        return 'submitted'

    if opt_in is True:
        return 'unconfirmed'

    def send_create(before_send):
        return keap_crm.create_contact(
            connection, revision, email, row['first_name'], row['last_name'],
            before_send=before_send,
        )

    result_id = _keap_write(tx, job, row, connection, validate, stop_event,
                            clock, provider, 1, None, send_create)

    if result_id:
        with tx() as con:
            scope(con, job, connection, validate, stop_event, provider=provider)
            con.execute(
                f"UPDATE {provider.delivery} SET activity_id=? "
                "WHERE id = ? AND write_started = 1",
                (result_id, row['id']),
            )

    return 'submitted'
