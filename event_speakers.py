"""Occurrence-scoped guest invitations and private WebRTC negotiation."""

from base64 import b64encode
from datetime import datetime
from hashlib import sha256
import hmac
import os
import re
from secrets import token_urlsafe
from time import time
from uuid import uuid4

from flask import abort, make_response, render_template, request, url_for

import event_slide_decks as slides
from library_interactivity import text_field


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_speaker_policy(
 event TEXT NOT NULL, opens INTEGER NOT NULL, review INTEGER NOT NULL, PRIMARY KEY(event,opens));
CREATE TABLE IF NOT EXISTS live_speaker(
 id TEXT PRIMARY KEY, event TEXT NOT NULL, opens INTEGER NOT NULL, name TEXT NOT NULL, expires INTEGER NOT NULL,
 invite TEXT, session TEXT, admitted INTEGER NOT NULL DEFAULT 0, on_air INTEGER NOT NULL DEFAULT 0,
 revision TEXT, offer TEXT, answer TEXT, last_seen INTEGER);
CREATE INDEX IF NOT EXISTS live_speaker_occurrence ON live_speaker(event,opens);
CREATE TABLE IF NOT EXISTS live_speaker_message(
 event TEXT NOT NULL, opens INTEGER NOT NULL, id TEXT NOT NULL, created INTEGER NOT NULL,
 author TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(event,opens,id));
"""
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
ROOM_LIMIT = 7


def digest(token):
    return sha256(token.encode("ascii")).hexdigest()


def credential():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not TOKEN.fullmatch(token):
        abort(404)
    return digest(token)


def ice(guest, expires):
    servers = []
    stun = os.environ.get("CUEPORCH_STUN_URL", "")
    if stun.startswith("stun:"):
        servers.append(dict(urls=stun))
    turn = os.environ.get("CUEPORCH_TURN_URL", "")
    secret = os.environ.get("CUEPORCH_TURN_SECRET", "")
    if turn.startswith(("turn:", "turns:")) and secret:
        username = f"{expires}:{guest}"
        password = b64encode(hmac.digest(secret.encode(), username.encode(), "sha1")).decode("ascii")
        servers.append(dict(urls=turn, username=username, credential=password))
    return servers


def policy(con, event, opens):
    row = con.execute("SELECT review FROM live_speaker_policy WHERE event=? AND opens=?", (event, opens)).fetchone()
    return bool(row and row["review"])


def description(body, kind):
    value = body.get(kind)
    if not isinstance(value, str) or not value.startswith("v=0\r\n") or not len(value) <= 24576 or "\0" in value:
        abort(422, "The media negotiation data is invalid.")
    return value


def confidential(value, status=200):
    response = make_response(value, status)
    response.cache_control.no_store = True
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def chat(con, event, opens, author):
    if request.method == "POST":
        body = request.get_json()
        if not isinstance(body, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(body.get("id"))):
            abort(422, "Enter a speaker message.")
        value = text_field(body.get("text"), "a speaker message", 400, True)
        con.execute("INSERT OR IGNORE INTO live_speaker_message VALUES(?,?,?,?,?,?)",
                    (event, opens, body["id"], int(time()), author, value))
    rows = con.execute("""SELECT id,author,text FROM live_speaker_message WHERE event=? AND opens=?
                          ORDER BY created DESC,rowid DESC LIMIT 80""", (event, opens)).fetchall()
    return confidential(dict(messages=[dict(row) for row in reversed(rows)]))


def register(app, tx, staff, slot_access, database):
    slide_storage = database.parent / "live-slide-decks"

    def current(con, guest, event=None, opens=None):
        row = con.execute("SELECT * FROM live_speaker WHERE id=?", (guest,)).fetchone()
        if not row or row["expires"] <= time() or (event and (event, opens) != (row["event"], row["opens"])):
            abort(404)
        return row

    def admitted(con, guest, presented):
        row = current(con, guest)
        if not hmac.compare_digest(row["session"] or "", presented) or not row["admitted"]:
            abort(404)
        slot_access(con, row["event"], row["opens"])
        return row

    @app.route("/api/live-events/<event>/<int:opens>/speakers/chat", methods=["GET", "POST"])
    def producer_speaker_chat(event, opens):
        name = staff()
        with tx() as con:
            slot_access(con, event, opens)
            return chat(con, event, opens, "Producer" if name == "operator" else name)

    @app.route("/api/guest-speaker/<guest>/chat", methods=["GET", "POST"])
    def guest_speaker_chat(guest):
        presented = credential()
        with tx() as con:
            row = admitted(con, guest, presented)
            return chat(con, row["event"], row["opens"], row["name"])

    @app.route("/api/guest-speaker/<guest>/slides", methods=("POST", "PUT"))
    def guest_speaker_slides(guest):
        presented = credential()

        def authorize(con):
            return admitted(con, guest, presented)

        with tx() as con:
            row = authorize(con)
            event, opens = row["event"], row["opens"]
            if request.method == "PUT":
                if not row["on_air"]:
                    abort(404)
                program = slides.program(con, event, opens)
                changed = slides.step_program(con, event, opens, program, request.get_json())
                return confidential(slides.program_view(changed))
        item, status = slides.import_deck(tx, slide_storage, event, authorize)
        return confidential(item, status)

    @app.get("/api/guest-speaker/<guest>/slides/<stage>/<int:revision>")
    def guest_speaker_slide_image(guest, stage, revision):
        presented = credential()
        with tx() as con:
            row = admitted(con, guest, presented)
            shown = slides.program(con, row["event"], row["opens"]) if row["on_air"] else None
            if not shown or shown["id"] != stage or shown["revision"] != revision:
                abort(404)
        return slides.slide_image(slide_storage, shown["deck"], shown["page"])

    @app.route("/api/live-events/<event>/<int:opens>/speakers", methods=["GET", "POST", "PUT"])
    def event_speakers(event, opens):
        staff()
        now = int(time())
        with tx() as con:
            slot = slot_access(con, event, opens)
            if request.method == "GET":
                rows = con.execute("SELECT * FROM live_speaker WHERE event=? AND opens=? AND expires>? ORDER BY rowid", (event, opens, now))
                speakers = [dict(id=row["id"], name=row["name"], revision=row["revision"], admitted=bool(row["admitted"]),
                                 on_air=bool(row["on_air"]), joined=bool(row["session"]),
                                 connected=(row["last_seen"] or 0) > now - 15,
                                 needs_answer=bool(row["admitted"] and row["offer"]),) for row in rows]
                return confidential(dict(review=policy(con, event, opens), speakers=speakers))
            body = request.get_json()
            if not isinstance(body, dict):
                abort(422)
            if request.method == "PUT":
                require = body.get("review")
                if type(require) is not bool:
                    abort(422)
                con.execute("INSERT OR REPLACE INTO live_speaker_policy VALUES(?,?,?)", (event, opens, require))
                if not require:
                    con.execute("UPDATE live_speaker SET admitted=1 WHERE event=? AND opens=? AND session IS NOT NULL", (event, opens))
                return "", 204
            if con.execute("SELECT count(*) FROM live_speaker WHERE event=? AND opens=? AND expires>?", (event, opens, now)).fetchone()[0] >= ROOM_LIMIT:
                abort(409, "This occurrence already has seven speaker invitations.")
            name = text_field(body.get("name"), "the speaker’s name", 72, True)
            guest, token = uuid4().hex, token_urlsafe(32)
            expires = int(datetime.fromisoformat(slot["end"]).timestamp())
            con.execute("INSERT INTO live_speaker(id,event,opens,name,expires,invite) VALUES(?,?,?,?,?,?)",
                        (guest, event, opens, name, expires, digest(token)))
            return confidential(dict(id=guest, link=url_for("live_speaker_page", guest=guest) + "#" + token), 201)

    @app.route("/api/live-events/<event>/<int:opens>/speakers/<guest>", methods=["GET", "PUT", "DELETE"])
    def event_speaker(event, opens, guest):
        staff()
        with tx() as con:
            slot_access(con, event, opens)
            row = current(con, guest, event, opens)
            if request.method == "DELETE":
                con.execute("DELETE FROM live_speaker WHERE id=?", (guest,))
                return "", 204
            if request.method == "GET":
                if not row["admitted"] or not row["offer"]:
                    abort(404)
                return confidential(dict(revision=row["revision"], offer=row["offer"], ice=ice(guest + "-host", row["expires"])))
            body = request.get_json()
            if not isinstance(body, dict) or not row["session"]:
                abort(409)
            if set(body) == {"admit"} and body["admit"] is True:
                con.execute("UPDATE live_speaker SET admitted=1 WHERE id=?", (guest,))
            elif set(body) == {"on_air"} and type(body["on_air"]) is bool and row["admitted"]:
                con.execute("UPDATE live_speaker SET on_air=? WHERE id=?", (body["on_air"], guest))
            elif set(body) == {"revision", "answer"} and row["admitted"] and row["revision"] == body["revision"] and row["offer"]:
                answer = description(body, "answer")
                if row["answer"] is not None and row["answer"] != answer:
                    abort(409, "Another operator connected this speaker.")
                con.execute("UPDATE live_speaker SET answer=? WHERE id=?", (answer, guest))
            else:
                abort(409, "The speaker's connection has changed.")
            return "", 204

    @app.get("/guest-speaker/<guest>")
    def live_speaker_page(guest):
        if not re.fullmatch(r"[0-9a-f]{32}", guest):
            abort(404)
        response = confidential(render_template("event_speaker.html", endpoint=url_for("live_speaker_guest", guest=guest)))
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    @app.route("/api/guest-speaker/<guest>", methods=["GET", "POST", "PUT", "DELETE"])
    def live_speaker_guest(guest):
        presented = credential()
        with tx() as con:
            row = current(con, guest)
            slot_access(con, row["event"], row["opens"])
            if request.method == "POST":
                if not hmac.compare_digest(row["invite"] or "", presented):
                    abort(404)
                secret = token_urlsafe(32)
                admitted = not policy(con, row["event"], row["opens"])
                con.execute("UPDATE live_speaker SET invite=NULL, session=?, admitted=?, last_seen=? WHERE id=?",
                            (digest(secret), admitted, int(time()), guest))
                return confidential(dict(session=secret), 201)
            if not hmac.compare_digest(row["session"] or "", presented):
                abort(404)
            if request.method == "DELETE":
                con.execute("DELETE FROM live_speaker WHERE id=?", (guest,))
                return "", 204
            con.execute("UPDATE live_speaker SET last_seen=? WHERE id=? AND (last_seen IS NULL OR last_seen<=?)",
                        (int(time()), guest, int(time()) - 3))
            if request.method == "PUT":
                body = request.get_json()
                if not row["admitted"] or not isinstance(body, dict):
                    abort(409)
                offer = description(body, "offer")
                revision = row["revision"] if row["offer"] == offer else uuid4().hex
                if revision != row["revision"]:
                    con.execute("UPDATE live_speaker SET revision=?, offer=?, answer=NULL, on_air=0 WHERE id=?", (revision, offer, guest))
                return confidential(dict(revision=revision))
            shown = slides.program(con, row["event"], row["opens"]) if row["admitted"] and row["on_air"] else None
            return confidential(dict(name=row["name"], admitted=bool(row["admitted"]), on_air=bool(row["on_air"]),
                                     revision=row["revision"], answer=row["answer"], ice=ice(guest, row["expires"]),
                                     slides=slides.program_view(shown)))
