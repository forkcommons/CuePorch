"""Durable, signed webhook delivery for configured event integrations."""

from contextlib import closing
from hashlib import sha256
import hmac
import json
import os
import secrets
from threading import Event, Thread
from time import time
from urllib.parse import urlsplit
import uuid

from filelock import FileLock, Timeout
from flask import abort, request
import requests

from browser_account_security import private, secure_site
from library_analytics import source_origin
from team_accounts import note


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_webhook(
 id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, secret TEXT NOT NULL, topics TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS live_webhook_delivery(
 id TEXT PRIMARY KEY, subscription TEXT NOT NULL, topic TEXT NOT NULL, event TEXT NOT NULL, occurrence INTEGER NOT NULL,
 marker TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0, due INTEGER NOT NULL, result INTEGER,
 UNIQUE(subscription,topic,event,occurrence,marker));
CREATE INDEX IF NOT EXISTS live_webhook_due ON live_webhook_delivery(state,due);
CREATE TABLE IF NOT EXISTS live_webhook_feed(
 event TEXT NOT NULL, occurrence INTEGER NOT NULL, state TEXT NOT NULL, PRIMARY KEY(event,occurrence));
CREATE TABLE IF NOT EXISTS live_webhook_registrant(
 id TEXT PRIMARY KEY, event TEXT NOT NULL, email_key TEXT NOT NULL, UNIQUE(event,email_key));
"""
TOPICS = (
    "live-event-updated", "live-event-clip-created", "live-event-started", "live-event-ended",
    "live-event-archive-available", "registration-created", "registration-updated",
)
RETRY_SECONDS = (10, 30, 120, 300, 900)


def configured_origin(endpoint):
    try:
        if any(ord(char) < 33 for char in endpoint) or "\\" in endpoint:
            return None
        parts = urlsplit(endpoint)
        origin = source_origin(endpoint)
        choices = {source_origin(value) for value in os.environ.get("CUEPORCH_EVENT_WEBHOOK_ORIGINS", "").split()}
        if (origin.startswith("https://") and origin in choices and parts.path.startswith("/")
                and parts.username is None and parts.password is None and not parts.fragment and not parts.query):
            return origin
    except (ValueError, TypeError):
        pass
    return None


def enqueue(con, topic, event, opens=0, marker="", data=None):
    now = int(time())
    recipients = con.execute("""SELECT s.id FROM live_webhook s WHERE EXISTS
                               (SELECT 1 FROM json_each(s.topics) WHERE value=?)""", (topic,)).fetchall()
    for recipient in recipients:
        uid = uuid.uuid4().hex
        payload = dict(id=uid, type=topic, event=event, occurrence=opens or None, occurred_at=now, data=data or {})
        con.execute("""INSERT OR IGNORE INTO live_webhook_delivery(id,subscription,topic,event,occurrence,marker,payload,due)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (uid, recipient["id"], topic, event, opens, marker, json.dumps(payload, separators=(",", ":")), now))


def subscribed(con, topics):
    placeholders = ",".join("?" for _ in topics)
    return con.execute(f"SELECT 1 FROM live_webhook, json_each(topics) WHERE value IN ({placeholders}) LIMIT 1",
                       topics).fetchone() is not None


def observe(con, event, opens, state):
    previous = con.execute("SELECT state FROM live_webhook_feed WHERE event=? AND occurrence=?",
                           (event, opens)).fetchone()
    if previous and previous["state"] == "ended":
        return
    if state == "live" and not previous:
        enqueue(con, "live-event-started", event, opens)
        con.execute("INSERT INTO live_webhook_feed VALUES(?,?,'live')", (event, opens))
    elif state in ("preparing_replay", "ended", "replay"):
        enqueue(con, "live-event-ended", event, opens)
        con.execute("""INSERT INTO live_webhook_feed VALUES(?,?,'ended') ON CONFLICT(event,occurrence)
                       DO UPDATE SET state=excluded.state""", (event, opens))


def send(session, item):
    if not configured_origin(item["endpoint"]):
        return None
    payload = item["payload"].encode("utf8")
    stamp = str(int(time()))
    digest = hmac.new(item["secret"].encode("ascii"), stamp.encode("ascii") + b"." + payload, sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-CuePorch-Delivery": item["id"],
               "X-CuePorch-Event": item["topic"], "X-CuePorch-Timestamp": stamp, "X-CuePorch-Signature": "v1=" + digest}
    try:
        with session.post(item["endpoint"], data=payload, headers=headers, timeout=(3, 6),
                          allow_redirects=False, stream=True) as result:
            return result.status_code
    except requests.RequestException:
        return None


def start(app, tx, database):
    stop = Event()
    lease = str(database) + ".event-webhooks.lock"

    def work():
        with closing(requests.Session()) as session:
            while not stop.is_set():
                try:
                    with FileLock(lease, timeout=0):
                        with tx() as con:
                            item = con.execute("""SELECT d.*,s.endpoint,s.secret FROM live_webhook_delivery d JOIN live_webhook s
                                                 ON s.id=d.subscription WHERE d.state='queued' AND d.due<=?
                                                 ORDER BY d.due,d.rowid LIMIT 1""", (int(time()),)).fetchone()
                        if item:
                            result = send(session, item)
                            attempts = item["attempts"] + 1
                            delivered = result is not None and 200 <= result < 300
                            retry = not delivered and attempts <= len(RETRY_SECONDS)
                            state = "delivered" if delivered else "queued" if retry else "failed"
                            due = int(time()) + RETRY_SECONDS[attempts - 1] if retry else item["due"]
                            with tx() as con:
                                con.execute("UPDATE live_webhook_delivery SET state=?,attempts=?,due=?,result=? WHERE id=?",
                                            (state, attempts, due, result, item["id"]))
                            continue
                except Timeout:
                    pass
                except Exception:
                    app.logger.exception("The live webhook queue could not be processed.")
                stop.wait(1)

    if not os.environ.get("CUEPORCH_EVENT_WEBHOOK_ORIGINS"):
        return lambda: None, lease
    worker = Thread(target=work, name="CuePorch event webhooks", daemon=True)
    worker.start()

    def close():
        stop.set()
        worker.join(10)

    return close, lease


def register(app, tx, origin, owner, database):
    close, lease = start(app, tx, database)

    @app.route("/api/operator/live-api/webhooks", methods=["GET", "POST"])
    def live_webhook_settings():
        secure_site(origin)
        actor = owner()
        if request.method in ("GET", "HEAD"):
            with tx() as con:
                rows = con.execute("SELECT id,endpoint,topics,created FROM live_webhook ORDER BY created DESC,id").fetchall()
            return private(dict(topics=TOPICS, webhooks=[
                dict(id=row["id"], endpoint=row["endpoint"], topics=json.loads(row["topics"]), created=row["created"])
                for row in rows]))
        body = request.get_json()
        endpoint = body.get("endpoint") if isinstance(body, dict) else None
        topics = body.get("topics") if isinstance(body, dict) else None
        if (not isinstance(endpoint, str) or len(endpoint) > 2048 or not configured_origin(endpoint) or
                not isinstance(topics, list) or not topics or any(topic not in TOPICS for topic in topics)):
            abort(422, "Use an operator-configured HTTPS receiver and select event notification types.")
        uid, secret = uuid.uuid4().hex, secrets.token_urlsafe(32)
        with tx() as con:
            con.execute("INSERT INTO live_webhook VALUES(?,?,?,?,?)",
                        (uid, endpoint, secret, json.dumps(sorted(set(topics))), int(time())))
            note(con, actor, "live_webhook_created", uid)
        return private(dict(id=uid, endpoint=endpoint, topics=topics, signing_secret=secret), 201)

    @app.route("/api/operator/live-api/webhooks/<uid>", methods=["GET", "DELETE"])
    def live_webhook_details(uid):
        secure_site(origin)
        actor = owner()
        if request.method in ("GET", "HEAD"):
            with tx() as con:
                if not con.execute("SELECT 1 FROM live_webhook WHERE id=?", (uid,)).fetchone():
                    abort(404)
                rows = con.execute("""SELECT id,topic,event,occurrence,state,attempts,due,result
                                     FROM live_webhook_delivery WHERE subscription=? ORDER BY rowid DESC LIMIT 100""",
                                   (uid,)).fetchall()
            return private(dict(deliveries=[dict(row) for row in rows]))
        try:
            with FileLock(lease, timeout=10), tx() as con:
                result = con.execute("DELETE FROM live_webhook WHERE id=?", (uid,))
                if not result.rowcount:
                    abort(404)
                con.execute("DELETE FROM live_webhook_delivery WHERE subscription=?", (uid,))
                note(con, actor, "live_webhook_revoked", uid)
        except Timeout:
            abort(409, "A delivery is finishing. Try revoking the webhook again.")
        return private("", 204)

    return close
