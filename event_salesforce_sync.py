import json
from time import time

from flask import abort, request

from browser_account_security import json_mutation, private, same_origin, secure_site
from team_accounts import note
import event_attendance
import event_hubspot_sync as sync
import salesforce_crm


def register(app, tx, staff, mail_origin, salesforce, read, selected_open,
             registration_intake, enroll_contacts):
    def apply_contacts(con, row, slot, form, fields, actor, source):
        result = enroll_contacts(con, row, slot, form, fields, source=source)
        if result["added"]:
            note(con, actor, "live_event_attendees_imported", row["id"],
                 f"slot {slot['id']}: added {result['added']} attendees")
        return dict(result, occurrence=slot["id"])

    @app.post("/api/live-events/<uid>/<int:opens>/registration/salesforce")
    def salesforce_registration(uid, opens):
        actor = staff()
        body = json_mutation(secure_site(mail_origin), "X-CuePorch-Integration")
        revid = salesforce.revision()
        with tx() as con:
            row, slot, form = registration_intake(con, uid, opens)
            row_rev, form_rev = row["revision"], form["revision"]
        fields = salesforce_crm.campaign_members(
            salesforce, revid, body.get("campaign")
        )
        with tx() as con:
            salesforce.authorize(con, revid)
            row, slot, form = registration_intake(con, uid, opens)
            if row["revision"] != row_rev or form["revision"] != form_rev:
                abort(409)
            return private(
                apply_contacts(con, row, slot, form, fields, actor, "salesforce")
            )

    def save_schedule(uid, opens, actor, body):
        revision = salesforce.revision()
        with tx() as con:
            row, _, form = registration_intake(con, uid, opens)
            expected = (row["revision"], form["revision"])
        campaign = salesforce_crm.campaign(salesforce, revision, body.get("campaign"))["id"]
        with tx() as con:
            salesforce.authorize(con, revision)
            row, slot, form = registration_intake(con, uid, opens)
            if expected != (row["revision"], form["revision"]):
                abort(409, "The event or registration form changed.")
            now = int(time())
            closes = event_attendance.expires_at(slot)
            if now >= min(closes, opens + 7200):
                abort(409, "This occurrence is past its import window.")
            zone = json.loads(row["settings"])["schedule"]["timezone"]
            saved = sync.set_sync(
                con, uid, opens, segment=campaign, connection=revision, actor=actor,
                event_revision=row["revision"], form_revision=form["revision"],
                closes=closes, zone=zone, now=now,
                first_due=max(now, sync.local_midnight(opens, zone)), provider="salesforce",
            )
            return private(sync_receipt(con, saved))

    def sync_receipt(con, job):
        receipt = sync.receipt(job)
        if "segment" in receipt:
            receipt["campaign"] = receipt.pop("segment")
        receipt["available"] = bool(salesforce.configured and salesforce.connection(con))
        return receipt

    @app.route("/api/live-events/<uid>/<int:opens>/registration/salesforce/sync", methods=["GET", "PUT", "DELETE"])
    def salesforce_sync(uid, opens):
        actor = staff()
        secure = secure_site(mail_origin)
        if request.method == "GET":
            same_origin(secure)
            with tx() as con:
                _, event = read(con, uid)
                selected_open(event, opens)
                row = sync.get(con, uid, opens, provider="salesforce")
                return private(sync_receipt(con, row))
        body = json_mutation(secure, "X-CuePorch-Integration")
        if request.method == "PUT":
            return save_schedule(uid, opens, actor, body)
        with tx() as con:
            _, event = read(con, uid)
            selected_open(event, opens)
            sync.remove(con, uid, opens, provider="salesforce")
            return private(sync_receipt(con, None))

    return apply_contacts
