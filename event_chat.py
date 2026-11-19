"""Occurrence-scoped audience chat and operator moderation."""

import csv
import hmac
from io import StringIO
import re
import time
import uuid

from flask import abort, make_response, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from library_interactivity import text_field
from library_registration import csv_cell


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_chat_settings(
 event TEXT NOT NULL, opens INTEGER NOT NULL, enabled INTEGER NOT NULL, interval INTEGER NOT NULL,
 PRIMARY KEY(event,opens));
CREATE TABLE IF NOT EXISTS live_chat_message(
 id TEXT PRIMARY KEY, event TEXT NOT NULL, opens INTEGER NOT NULL, viewer TEXT NOT NULL,
 name TEXT NOT NULL, message TEXT NOT NULL, sent REAL NOT NULL, removed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS live_chat_history ON live_chat_message(event,opens,sent);
CREATE INDEX IF NOT EXISTS live_chat_sender ON live_chat_message(event,opens,viewer,sent);
CREATE TABLE IF NOT EXISTS live_chat_ban(
 event TEXT NOT NULL, opens INTEGER NOT NULL, viewer TEXT NOT NULL, PRIMARY KEY(event,opens,viewer));
CREATE TABLE IF NOT EXISTS live_chat_rate(
 event TEXT NOT NULL, opens INTEGER NOT NULL, network TEXT NOT NULL, started INTEGER NOT NULL,
 count INTEGER NOT NULL, PRIMARY KEY(event,opens,network));
"""
LIMIT = 240
MESSAGE_ID = re.compile("[0-9a-f]{32}")


def settings(con, uid, opens):
    row = con.execute("SELECT * FROM live_chat_settings WHERE event=? AND opens=?", (uid, opens)).fetchone()
    return (bool(row["enabled"]), row["interval"]) if row else (False, 0)


def list_chat(con, uid, opens, viewer=None, operator=False):
    enabled, interval = settings(con, uid, opens)
    messages = []
    if enabled or operator:
        rows = con.execute("""SELECT * FROM live_chat_message WHERE event=? AND opens=? AND removed=0
                            ORDER BY sent DESC LIMIT 100""", (uid, opens)).fetchall()
        messages = [dict(id=row["id"], name=row["name"], message=row["message"], sent=row["sent"],
                         mine=bool(viewer and viewer == row["viewer"])) for row in reversed(rows)]
    banned = viewer and con.execute("SELECT 1 FROM live_chat_ban WHERE event=? AND opens=? AND viewer=?",
                                    (uid, opens, viewer)).fetchone()
    return dict(enabled=enabled, interval=interval, messages=messages, banned=bool(banned))


def posted(con, uid, opens, viewer, body, network):
    enabled, interval = settings(con, uid, opens)
    if not enabled:
        abort(409, "Audience chat is off.")
    if con.execute("SELECT 1 FROM live_chat_ban WHERE event=? AND opens=? AND viewer=?", (uid, opens, viewer)).fetchone():
        abort(403, "This viewer cannot post in the chat.")
    message_id = body.get("id", "")
    if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
        abort(422, "Open the event page again before sending a message.")
    name = text_field(body.get("name"), "a display name", 60, True)
    message = text_field(body.get("message"), "a chat message", LIMIT, True)
    if con.execute("SELECT 1 FROM live_chat_message WHERE id=? AND event=? AND opens=? AND viewer=?",
                   (message_id, uid, opens, viewer)).fetchone():
        return
    now = time.time()
    last = con.execute("SELECT MAX(sent) FROM live_chat_message WHERE event=? AND opens=? AND viewer=?",
                       (uid, opens, viewer)).fetchone()[0]
    if last and now - last < max(interval, 1):
        abort(429, "Wait before sending another chat message.")
    minute = int(now // 60)
    used = con.execute("""INSERT INTO live_chat_rate VALUES(?,?,?,?,1)
                          ON CONFLICT(event,opens,network) DO UPDATE SET
                          count=CASE WHEN started=excluded.started THEN count+1 ELSE 1 END,
                          started=excluded.started RETURNING count""", (uid, opens, network, minute)).fetchone()[0]
    if used > 240:
        abort(429, "Too many chat messages. Try again shortly.")
    con.execute("INSERT OR IGNORE INTO live_chat_message VALUES(?,?,?,?,?,?,?,0)",
                (message_id, uid, opens, viewer, name, message, now))


def moderate(con, uid, opens, body, viewer=None):
    identifier = body.get("id")
    row = con.execute("SELECT * FROM live_chat_message WHERE id=? AND event=? AND opens=?",
                      (identifier, uid, opens)).fetchone() if isinstance(identifier, str) else None
    if row is None or (viewer and row["viewer"] != viewer):
        abort(404)
    if not viewer and body["type"] == "ban":
        con.execute("INSERT OR IGNORE INTO live_chat_ban VALUES(?,?,?)", (uid, opens, row["viewer"]))
    con.execute("UPDATE live_chat_message SET removed=1 WHERE id=?", (identifier,))


def download(con, uid, opens):
    data = StringIO(newline="")
    writer = csv.writer(data)
    writer.writerow(("display_name", "message", "unix_time"))
    for row in con.execute("""SELECT name,message,sent FROM live_chat_message
                             WHERE event=? AND opens=? AND removed=0 ORDER BY sent""", (uid, opens)):
        cells = tuple(map(csv_cell, (row["name"], row["message"])))
        writer.writerow((*cells, row["sent"]))
    result = make_response(data.getvalue())
    result.headers.update({"Content-Type": "text/csv; charset=utf-8",
                           "Content-Disposition": 'attachment; filename="cueporch-event-chat.csv"'})
    return result


def register(app, tx, staff, signing_key, operator_access, audience_access):
    signer = URLSafeTimedSerializer(signing_key, salt="cueporch-live-chat")

    def session(scope, token=None):
        if token:
            try:
                saved, subject = signer.loads(token, max_age=12 * 3600)
                if saved == scope and MESSAGE_ID.fullmatch(subject):
                    return token, subject
            except (BadSignature, ValueError, TypeError):
                pass
        subject = uuid.uuid4().hex
        return signer.dumps([scope, subject]), subject

    @app.route("/api/live-events/<uid>/<int:opens>/chat", methods=["GET", "PUT", "POST"])
    def operator_event_chat(uid, opens):
        staff()
        with tx() as con:
            operator_access(con, uid, opens)
            if request.method in ("PUT", "POST"):
                body = request.get_json()
                if not isinstance(body, dict):
                    abort(422, "Choose a chat action.")
                if request.method == "PUT":
                    enabled, interval = body.get("enabled"), body.get("interval")
                    if type(enabled) is not bool or type(interval) is not int or not 0 <= interval <= 120:
                        abort(422, "Choose a chat delay between 0 and 120 seconds.")
                    con.execute("INSERT OR REPLACE INTO live_chat_settings VALUES(?,?,?,?)", (uid, opens, enabled, interval))
                elif body.get("type") in ("remove", "ban"):
                    moderate(con, uid, opens, body)
                else:
                    abort(422, "Choose a chat action.")
            return list_chat(con, uid, opens, operator=True)

    @app.get("/api/live-events/<uid>/<int:opens>/chat/export")
    def export_event_chat(uid, opens):
        staff()
        with tx() as con:
            operator_access(con, uid, opens)
            return download(con, uid, opens)

    @app.route("/events/<slug>/<int:opens>/chat", methods=["GET", "POST"])
    def audience_event_chat(slug, opens):
        with tx() as con:
            scope, active = audience_access(con, slug, opens)
            uid = scope[0]
            current = request.headers.get("X-CuePorch-Chat-Session")
            token, viewer = session(scope, current)
            if request.method == "POST":
                if token != current or not active:
                    abort(403, "Chat is unavailable for this viewer.")
                body = request.get_json()
                if not isinstance(body, dict):
                    abort(422, "Choose a chat action.")
                if body.get("type") == "message":
                    network = hmac.digest(signing_key.encode(), str(request.remote_addr).encode(), "sha256").hex()
                    posted(con, uid, opens, viewer, body, network)
                elif body.get("type") == "remove":
                    moderate(con, uid, opens, body, viewer)
                else:
                    abort(422, "Choose a chat action.")
            payload = list_chat(con, uid, opens, viewer)
            return dict(payload, session=token, active=active)

    return session
