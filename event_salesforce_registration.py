import json
from time import time
from datetime import datetime, timezone

from flask import abort, request

from browser_account_security import json_mutation, private, same_origin, secure_site
import event_attendance
import event_contact_delivery as outbox
import salesforce_crm
import salesforce_outbound

PROVIDER = "salesforce"


def connection_revision(con, connection):
    if not connection.configured:
        return None
    row = connection.connection(con)
    if row is None:
        return None
    return row["revision"]


def offer(con, connection, row, slot, form, now):
    revision = connection_revision(con, connection)
    if revision is None or form is None:
        return None
    candidate = outbox.get(con, PROVIDER, row["id"], slot["id"])
    if (
        candidate
        and candidate["provider_revision"] == revision
        and candidate["event_revision"] == row["revision"]
        and candidate["form_revision"] == form["revision"]
        and candidate["deadline"] > now
    ):
        return candidate
    return None


def register_routes(app, tx, staff, origin, connection, read, selected_open, registration_intake):

    def save_dest(uid, opens, body):
        company = body.get("company", "Individual")
        if not isinstance(company, str):
            abort(422)
        company = company.strip()
        if not company or not company.isprintable() or len(company) > 255:
            abort(422)

        revision = connection.revision()

        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)

        cid = salesforce_crm.campaign(connection, revision, body.get("campaign"))["id"]
        salesforce_outbound.validate_installation(connection, revision)

        with tx() as con:
            connection.authorize(con, revision)
            row_again, slot_again, form_again = registration_intake(con, uid, opens)
            if row_again["revision"] != row["revision"] or form_again["revision"] != form["revision"]:
                abort(409)
            destination = {"campaign": cid, "company": company}
            outbox.set_destination(
                con,
                PROVIDER,
                uid,
                opens,
                provider_revision=revision,
                event_revision=row["revision"],
                form_revision=form["revision"],
                deadline=event_attendance.expires_at(slot),
                destination=destination,
            )
            return private(outbox.receipt(con, PROVIDER, uid, opens, revision))

    @app.route("/api/live-events/<uid>/<int:opens>/registration/salesforce/outbound", methods=["GET", "PUT", "DELETE"])
    def salesforce_destination(uid, opens):
        staff()
        secure = secure_site(origin)
        if request.method == "GET":
            same_origin(secure)
        else:
            body = json_mutation(secure, "X-CuePorch-Integration")
            if request.method == "PUT":
                return save_dest(uid, opens, body)
        with tx() as con:
            _, event = read(con, uid)
            selected_open(event, opens)
            if request.method == "DELETE":
                outbox.disable(con, PROVIDER, uid, opens)
            return private(outbox.receipt(con, PROVIDER, uid, opens, connection_revision(con, connection)))

def enqueue(con, action, plan, registrant, answers, now):
    delivery = outbox.enqueue(con, action, plan, registrant, now)
    if delivery:
        con.execute(
            "INSERT INTO live_event_salesforce_payload (delivery, answers) VALUES (?,?)",
            (delivery, json.dumps({
                "first_name": answers.get("first_name"),
                "last_name": answers.get("last_name"),
                "registered_at": datetime.fromtimestamp(
                    now, timezone.utc).isoformat(timespec="seconds"),
            })),
        )


def deliver(tx, job, connection, validate, stop, clock):
    with tx() as con:
        revision = connection_revision(con, connection)
        if revision is None:
            abort(409)
        plan, destination, email, answers = outbox.scope(
            con, job, revision, int(clock()), validate, stop)
        title = json.loads(con.execute(
            "SELECT settings FROM live_event WHERE id=?",
            (job["event"],)).fetchone()["settings"])["title"]
    def before_write():
        with tx() as fresh:
            connection.authorize(fresh, revision)
            outbox.scope(fresh, job, revision, int(clock()), validate, stop)
    return salesforce_outbound.publish_registration(
        connection, revision,
        campaign=destination["campaign"], kind="webinar", title=title,
        source_ref=f"{job['event']}:{job['opens']}",
        registrant=job["registrant"], registered_at=answers["registered_at"],
        address=email, answers=answers, company=destination["company"],
        before_write=before_write)


def start(app, tx, database, connection, validate, *, clock=time):
    if not connection or not connection.configured:
        return lambda: None
    def tick(stop):
        outbox._run_one(tx, PROVIDER,
            lambda job: deliver(tx, job, connection, validate, stop, clock),
            stop, clock)
    return outbox._start_worker(app, database, PROVIDER, tick)


def disconnect(con):
    for event, opens in con.execute(
            "SELECT event, opens FROM live_event_contact_plan WHERE provider=?",
            (PROVIDER,)).fetchall():
        outbox.disable(con, PROVIDER, event, opens)
