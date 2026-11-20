"""Occurrence-scoped attendance links for verified webinar registrations."""

from datetime import datetime
import re
from time import time
import uuid

from flask import abort
from itsdangerous import BadSignature

import library_registration


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_attendance_link(
 id TEXT PRIMARY KEY, event TEXT NOT NULL, opens INTEGER NOT NULL, event_revision TEXT NOT NULL,
 form_revision TEXT NOT NULL, email_key TEXT NOT NULL, expires INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
 UNIQUE(event,opens,event_revision,form_revision,email_key));
"""
IDENTIFIER = re.compile(r"[a-f0-9]{32}")


def expires_at(slot):
    return int(datetime.fromisoformat(slot["end"]).timestamp())


def active(con, uid, scope):
    event, revision, opens, form_revision, embed = scope
    if not uid or embed:
        return False
    return con.execute(
        """SELECT 1 FROM live_attendance_link l JOIN viewer_registrant r ON r.kind='event'
           AND r.subject=l.event AND r.email_key=l.email_key
           WHERE l.id=? AND l.event=? AND l.opens=? AND l.event_revision=? AND l.form_revision=?
             AND l.revoked=0 AND l.expires>?""",
        (uid, event, opens, revision, form_revision, int(time())),
    ).fetchone() is not None


def decode(signer, value):
    try:
        item = signer.loads(value)
    except BadSignature:
        return None
    return item if isinstance(item, str) and IDENTIFIER.fullmatch(item) else None


def enroll(con, event, revision, slot, form, body, on_record=None):
    if not isinstance(body, dict) or not isinstance(body.get("email"), str):
        abort(422, "Enter an attendee email address and registration answers.")
    values = {}
    for name, value in body.items():
        if type(value) is bool:
            values[name] = "yes" if value else "no"
        elif isinstance(value, str):
            values[name] = value
        else:
            abort(422, "Send registration answers as text or checkbox values.")
    failure = library_registration.enroll(con, "event", event, form, values, {}, replace=True, on_record=on_record)
    if failure:
        abort(422, failure)
    registrant, created = issue(con, event, revision, slot, form, values["email"].strip())
    if registrant is None:
        reason = ("This occurrence has ended." if expires_at(slot) <= int(time()) else
                  "This attendee's join link was revoked for the current occurrence.")
        abort(409, reason)
    return registrant, created


def issue(con, event, revision, slot, form, address):
    expires = expires_at(slot)
    if expires <= int(time()):
        return None, False
    scope = (event, slot["id"], revision, form["revision"], address.casefold())
    entry = con.execute("""SELECT id,revoked FROM live_attendance_link WHERE event=? AND opens=?
                           AND event_revision=? AND form_revision=? AND email_key=?""", scope).fetchone()
    if entry and entry["revoked"]:
        return None, False
    uid = entry["id"] if entry else uuid.uuid4().hex
    if not entry:
        con.execute("""INSERT INTO live_attendance_link(id,event,opens,event_revision,form_revision,email_key,expires)
                       VALUES(?,?,?,?,?,?,?)""", (uid, *scope, expires))
    return dict(id=uid, email=address, expires_at=expires), not entry


def revoke(con, event, opens, uid):
    result = con.execute("UPDATE live_attendance_link SET revoked=1 WHERE id=? AND event=? AND opens=?",
                         (uid, event, opens))
    if not result.rowcount:
        abort(404)
