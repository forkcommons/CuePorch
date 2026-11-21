import json
from time import time
from datetime import datetime, timezone

from flask import abort, request

from browser_account_security import json_mutation, private, same_origin, secure_site
import event_attendance
import event_contact_delivery as outbox
import event_salesforce_engagement
import salesforce_crm
import salesforce_outbound
import salesforce_registration_fields

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


def register_routes(app, tx, staff, origin, connection, read, selected_open, registration_intake, validate_engagement):

    def save_dest(uid, opens, body):
        company = body.get("company", "Individual")
        if not isinstance(company, str):
            abort(422)
        company = company.strip()
        if not company or not company.isprintable() or len(company) > 255:
            abort(422)

        engagement = body.get("engagement", False)
        if type(engagement) is not bool:
            abort(422)

        revision = connection.revision()

        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)

        if engagement and not form["attendance_analytics"]:
            abort(409, 'Enable attendee analytics on the registration form first.')

        cid = salesforce_crm.campaign(connection, revision, body.get("campaign"))["id"]
        related_fields = salesforce_registration_fields.configured(form)
        salesforce_outbound.validate_installation(
            connection, revision, related_fields=related_fields)
        if engagement:
            salesforce_outbound.validate_engagement_installation(connection, revision, kind='webinar')

        with tx() as con:
            connection.authorize(con, revision)
            row_again, slot_again, form_again = registration_intake(con, uid, opens)
            if row_again["revision"] != row["revision"] or form_again["revision"] != form["revision"]:
                abort(409)
            destination = {
                "campaign": cid,
                "company": company,
                "engagement": engagement,
                "related_fields": bool(related_fields),
            }
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

    @app.post('/api/live-events/<uid>/<int:opens>/registration/salesforce/outbound/complete')
    def complete_outbound(uid, opens):
        staff()
        json_mutation(secure_site(origin), 'X-CuePorch-Integration')
        with tx() as con:
            plan = outbox.get(con, PROVIDER, uid, opens)
            if not plan or json.loads(plan['destination']).get('engagement') is not True:
                abort(409, 'Salesforce engagement is not enabled for this registration')
            connection.authorize(con, plan['provider_revision'])
            validate_engagement(con, plan)
            observed = event_salesforce_engagement.completion(con, plan['generation'])
            if observed is None:
                abort(409, 'Salesforce engagement stream end must be confirmed')
            event_salesforce_engagement.complete(
                con, plan['generation'], time(), observed['duration'], manual=True)
            return private(outbox.receipt(
                con, PROVIDER, uid, opens, plan['provider_revision']))

def enqueue(con, action, plan, registrant, answers, now, form=None):
    delivery = outbox.enqueue(con, action, plan, registrant, now)
    if delivery:
        payload = {
            "first_name": answers.get("first_name"),
            "last_name": answers.get("last_name"),
            "registered_at": datetime.fromtimestamp(
                now, timezone.utc).isoformat(timespec="seconds"),
        }
        if (
            form is not None
            and json.loads(plan["destination"]).get("related_fields") is True
        ):
            payload["related_fields"] = salesforce_registration_fields.selected(
                form, answers)
        else:
            payload["related_fields"] = []
        con.execute(
            "INSERT INTO live_event_salesforce_payload (delivery, answers) VALUES (?,?)",
            (delivery, json.dumps(payload)),
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
        related_fields=answers.get("related_fields", ()),
        before_write=before_write)


def start(app, tx, database, connection, validate, *, clock=time, engagement_validate=None):
    if not connection or not connection.configured:
        return lambda: None
    def tick(stop):
        registration_success = outbox._run_one(tx, PROVIDER,
            lambda job: deliver(tx, job, connection, validate, stop, clock),
            stop, clock)
        engagement_success = False
        if engagement_validate is not None:
            engagement_success = event_salesforce_engagement.run_one(
                tx, connection, engagement_validate, stop, clock)
        return registration_success or engagement_success
    return outbox._start_worker(app, database, PROVIDER, tick)


def disconnect(con):
    for event, opens in con.execute(
            "SELECT event, opens FROM live_event_contact_plan WHERE provider=?",
            (PROVIDER,)).fetchall():
        outbox.disable(con, PROVIDER, event, opens)
