"""Operator-configured webinar notifications sent through the workspace TLS relay."""

from datetime import datetime, timezone
import json
from threading import Event, Thread
from time import time
from urllib.parse import urlencode
import uuid

from filelock import FileLock, Timeout
from flask import abort, request

from browser_account_security import private, secure_site
import mail_relay
from team_accounts import note


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event_mail_setting(
 event TEXT PRIMARY KEY, revision TEXT NOT NULL, settings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_event_mail_job(
 link TEXT NOT NULL, kind TEXT NOT NULL, offset INTEGER NOT NULL, due INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(link,kind,offset));
CREATE INDEX IF NOT EXISTS live_event_mail_due ON live_event_mail_job(state,due);
"""
DEFAULT = dict(confirmation=False, reminder_minutes=[], follow_up_hours=None)
RETRY_SECONDS = (15, 60, 300)
WATCHABLE = ("public", "password", "unlisted")


def settings(con, event):
    row = con.execute("SELECT revision,settings FROM live_event_mail_setting WHERE event=?", (event,)).fetchone()
    return (row["revision"], json.loads(row["settings"])) if row else (None, DEFAULT)


def enabled(config):
    return bool(config["confirmation"] or config["reminder_minutes"] or config["follow_up_hours"] is not None)


def validate(config):
    if not isinstance(config, dict) or type(config.get("confirmation")) is not bool:
        abort(422, "Choose whether to send registration confirmations.")
    intervals = config.get("reminder_minutes")
    if (not isinstance(intervals, list) or len(intervals) > 10
            or any(type(offset) is not int or not 1 <= offset <= 90 * 1440 for offset in intervals)
            or len(set(intervals)) != len(intervals)):
        abort(422, "Use up to 10 distinct reminders from 1 minute to 90 days before the event.")
    follow_up = config.get("follow_up_hours")
    if follow_up is not None and (type(follow_up) is not int or not 0 <= follow_up <= 168):
        abort(422, "Set the follow-up delay to 0 through 168 hours, or leave it off.")
    return dict(confirmation=config["confirmation"], reminder_minutes=sorted(intervals), follow_up_hours=follow_up)


def schedule(con, link, opens, expires, config, now, confirmation=False):
    planned = []
    if confirmation and config["confirmation"] and expires > now:
        planned.append(("confirmation", 0, now))
    planned.extend(("reminder", minutes, opens - 60 * minutes) for minutes in config["reminder_minutes"]
                   if opens - 60 * minutes >= now)
    follow_up = config["follow_up_hours"]
    if follow_up is not None and expires > now:
        planned.append(("follow-up", follow_up, expires + 3600 * follow_up))
    con.executemany("""INSERT INTO live_event_mail_job(link,kind,offset,due) VALUES(?,?,?,?)
                    ON CONFLICT(link,kind,offset) DO UPDATE SET state='queued',attempts=0,due=excluded.due
                    WHERE live_event_mail_job.state='canceled' AND excluded.kind<>'confirmation'""",
                    ((link, kind, offset, when) for kind, offset, when in planned))


def kind_enabled(config, kind, offset):
    if kind == "confirmation":
        return config["confirmation"]
    if kind == "reminder":
        return offset in config["reminder_minutes"]
    return config["follow_up_hours"] == offset


def message(origin, row, definition, signer, replay, unlisted_key):
    title = " ".join(definition["title"].split())
    occurrence = datetime.fromtimestamp(row["opens"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    path = f"{origin}/events/{row['slug']}"
    if row["kind"] == "follow-up":
        watch = urlencode({"occurrence": row["opens"], **({"key": unlisted_key} if unlisted_key else {})})
        replay_hint = f"\n\nWatch the replay\n{path}?{watch}" if replay else ""
        return f"Thank you for registering for {title}", f"Thank you for registering for {title}.{replay_hint}"
    subject = f"You're registered for {title}" if row["kind"] == "confirmation" else f"Reminder about {title}"
    return subject, f"{title}\n{occurrence}\n\nYour personal join link\n{path}/join#{signer.dumps(row['link'])}"


def next_job(con, now):
    return con.execute("""SELECT j.*,l.event,l.opens,l.expires,CASE WHEN l.revoked=1 OR EXISTS(SELECT 1 FROM live_event_hubspot_member m WHERE m.link=l.id AND m.removed=1) THEN 1 ELSE 0 END AS revoked,l.event_revision,l.form_revision,r.email,
                       e.slug,e.revision AS current_revision,e.settings AS event_settings,e.publication,
                       f.revision AS current_form,o.delivery,a.status AS approval,x.link_key
                       FROM live_event_mail_job j JOIN live_attendance_link l ON l.id=j.link
                       LEFT JOIN live_event e ON e.id=l.event LEFT JOIN action a ON a.id=e.publication
                       LEFT JOIN viewer_form f ON f.kind='event' AND f.subject=l.event
                       LEFT JOIN viewer_registrant r ON r.kind='event' AND r.subject=l.event AND r.email_key=l.email_key
                       LEFT JOIN live_occurrence o ON o.event=l.event AND o.opens=l.opens
                       LEFT JOIN live_event_access x ON x.event=l.event
                       WHERE j.state='queued' AND j.due<=? ORDER BY j.due,j.rowid LIMIT 1""", (now,)).fetchone()


def dispatch(con, row, now, origin, signer):
    _, rules = settings(con, row["event"])
    definition = json.loads(row["event_settings"] or "{}")
    late = (row["kind"] == "confirmation" and row["expires"] <= now
            or row["kind"] == "reminder" and row["opens"] <= now)
    eligible = (row["email"] and not row["revoked"] and not late
                and row["event_revision"] == row["current_revision"] and row["form_revision"] == row["current_form"]
                and definition.get("visibility") in WATCHABLE and row["delivery"] not in ("withdrawing", "withdrawn")
                and kind_enabled(rules, row["kind"], row["offset"]))
    if not eligible or row["approval"] not in ("eligible", "waiting"):
        return "canceled", None
    if row["approval"] == "waiting":
        return "waiting", None
    key = row["link_key"] if definition["visibility"] == "unlisted" else None
    return "ready", message(origin, row, definition, signer, row["delivery"] == "replay", key)


def finish(con, row, state, due, attempts):
    con.execute("""UPDATE live_event_mail_job SET state=?,due=?,attempts=? WHERE link=? AND kind=? AND offset=?""",
                (state, due, attempts, row["link"], row["kind"], row["offset"]))


def start(app, tx, database, origin, signer):
    config = mail_relay.configured(origin)
    lease = str(database) + ".event-mail.lock"
    if not config:
        return lambda: None, lease
    stop = Event()

    def work():
        while not stop.is_set():
            try:
                with FileLock(lease, timeout=0):
                    now = int(time())
                    with tx() as con:
                        row = next_job(con, now)
                        if row:
                            state, mail = dispatch(con, row, now, origin, signer)
                            if state != "ready":
                                finish(con, row, "queued" if state == "waiting" else state, now + 15, row["attempts"])
                    if row and state == "ready":
                        accepted = mail_relay.deliver(config, row["email"], *mail)
                        attempts = row["attempts"] + 1
                        retry = not accepted and attempts <= len(RETRY_SECONDS)
                        due = int(time()) + RETRY_SECONDS[attempts - 1] if retry else row["due"]
                        with tx() as con:
                            finish(con, row, "accepted" if accepted else "queued" if retry else "failed", due, attempts)
                    if row:
                        continue
            except Timeout:
                pass
            except Exception:
                app.logger.exception("The webinar email queue could not be processed.")
            stop.wait(1)

    worker = Thread(target=work, name="CuePorch webinar email", daemon=True)
    worker.start()

    def close():
        stop.set()
        worker.join(8)

    return close, lease


def register(app, tx, database, origin, staff, read, signer):
    close, lease = start(app, tx, database, origin, signer)

    @app.route("/api/live-events/<uid>/registration/email", methods=["GET", "PUT"])
    def event_mail_settings(uid):
        secure_site(origin)
        actor = staff()
        ready = bool(mail_relay.configured(origin))
        if request.method == "PUT":
            body = request.get_json()
            if not isinstance(body, dict):
                abort(422, "Supply webinar email settings.")
            updated = validate(body.get("settings"))
            try:
                with FileLock(lease, timeout=10), tx() as con:
                    event, definition = read(con, uid)
                    revision, current = settings(con, uid)
                    if revision != body.get("revision"):
                        abort(409, "These email settings changed. Reload them before saving.")
                    if enabled(updated) and (not ready or definition["visibility"] not in WATCHABLE):
                        abort(409, "Use a configured secure mail relay and a separate event watch page.")
                    if current != updated:
                        con.execute("""INSERT INTO live_event_mail_setting VALUES(?,?,?) ON CONFLICT(event) DO UPDATE
                                       SET revision=excluded.revision,settings=excluded.settings""",
                                    (uid, uuid.uuid4().hex, json.dumps(updated)))
                        now = int(time())
                        queued = con.execute("""SELECT j.link,j.kind,j.offset,j.attempts,j.due FROM live_event_mail_job j
                                                JOIN live_attendance_link l ON l.id=j.link
                                                WHERE l.event=? AND j.state='queued'""", (uid,)).fetchall()
                        for job in queued:
                            if not kind_enabled(updated, job["kind"], job["offset"]):
                                finish(con, job, "canceled", job["due"], job["attempts"])
                        links = con.execute("""SELECT id,opens,expires FROM live_attendance_link WHERE event=?
                                            AND event_revision=? AND revoked=0 AND expires>?""",
                                            (uid, event["revision"], now)).fetchall()
                        for link in links:
                            schedule(con, link["id"], link["opens"], link["expires"], updated, now)
                        note(con, actor, "live_event_email_changed", uid)
            except Timeout:
                abort(409, "A delivery is finishing. Reload email settings and try again.")
        with tx() as con:
            read(con, uid)
            revision, current = settings(con, uid)
            counts = con.execute("""SELECT j.state,COUNT(*) AS total FROM live_event_mail_job j JOIN live_attendance_link l
                                 ON l.id=j.link WHERE l.event=? GROUP BY j.state""", (uid,)).fetchall()
        return private(dict(ready=ready, revision=revision, settings=current,
                            deliveries={row["state"]: row["total"] for row in counts}))

    return close
