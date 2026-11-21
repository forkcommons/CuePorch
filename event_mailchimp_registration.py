from time import time

from flask import abort, request

import event_attendance
import event_contact_delivery as outbox
import mailchimp_crm
from browser_account_security import json_mutation, private, same_origin, secure_site

PROVIDER = "mailchimp"


def disconnect(con):
    rows = con.execute(
        "SELECT event, opens FROM live_event_contact_plan WHERE provider=?",
        (PROVIDER,),
    ).fetchall()
    for row in rows:
        outbox.disable(con, PROVIDER, row["event"], row["opens"])


def deliver(tx, job, connection, validate, stop, clock):
    with tx() as con:
        row = connection.connection(con)
        if row is None:
            abort(409)
        revision = row["revision"]
        _, destination, email, answers = outbox.scope(
            con, job, revision, int(clock()), validate, stop
        )

    def before_write():
        with tx() as con2:
            connection.authorize(con2, revision)
            outbox.scope(con2, job, revision, int(clock()), validate, stop)

    result = mailchimp_crm.save_member(
        connection,
        revision,
        destination["audience"],
        email,
        mailchimp_crm.mapped_values(answers, destination["mapping"]),
        before_write=before_write,
    )
    return "sent" if result["result"] == "delivered" else "skipped"


def start(app, tx, database, connection, validate):
    if not connection.configured:
        return lambda: None

    def tick(stop):
        return outbox._run_one(
            tx,
            PROVIDER,
            lambda job: deliver(tx, job, connection, validate, stop, time),
            stop,
            time,
        )

    return outbox._start_worker(app, database, PROVIDER, tick)


def register_routes(
    app, tx, staff, origin, connection, read, selected_open, registration_intake
):
    def set_mailchimp_destination(uid, opens, body):
        list_id = body.get("audience")
        if not mailchimp_crm.audience_id(list_id):
            abort(422)
        revision = connection.revision()
        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)
            row_revision, form_revision = row["revision"], form["revision"]
        audience = mailchimp_crm.audience(connection, revision, list_id)
        if audience.get("marketing_permissions") is True:
            abort(409)
        try:
            mapping = mailchimp_crm.validate_mapping(
                form,
                body.get("mapping", {}),
                mailchimp_crm.audience_schema(connection, revision, list_id),
            )
        except ValueError:
            abort(422)
        with tx() as con:
            connection.authorize(con, revision)
            row, slot, form = registration_intake(con, uid, opens)
            if row["revision"] != row_revision or form["revision"] != form_revision:
                abort(409)
            outbox.set_destination(
                con,
                PROVIDER,
                uid,
                opens,
                provider_revision=revision,
                event_revision=row["revision"],
                form_revision=form["revision"],
                deadline=int(event_attendance.expires_at(slot)),
                destination={"audience": list_id, "mapping": mapping},
            )

    @app.route(
        "/api/live-events/<uid>/<int:opens>/registration/mailchimp/outbound",
        methods=["GET", "PUT", "DELETE"],
    )
    def mailchimp_outbound(uid, opens):
        staff()
        secure = secure_site(origin)
        if request.method == "GET":
            same_origin(secure)
        else:
            body = json_mutation(secure, "X-CuePorch-Integration")
            if request.method == "PUT":
                set_mailchimp_destination(uid, opens, body)
        with tx() as con:
            row, event = read(con, uid)
            selected_open(event, opens)
            if request.method == "DELETE":
                outbox.disable(con, PROVIDER, uid, opens)
            row = connection.connection(con)
            revision = row["revision"] if row else None
            return private(outbox.receipt(con, PROVIDER, uid, opens, revision))
