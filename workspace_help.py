"""Searchable workspace help and private technical support conversations."""

import time
import uuid

from flask import abort, render_template, request

from browser_account_security import json_mutation, private, same_origin, secure_site
from team_accounts import MANAGERS, SESSION_MARKER, actor_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS help_request(
 id TEXT PRIMARY KEY, requester TEXT NOT NULL, subject TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','resolved')),
 created REAL NOT NULL, updated REAL NOT NULL, revision INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS help_requests_by_requester ON help_request(requester, updated DESC);
CREATE INDEX IF NOT EXISTS help_requests_by_update ON help_request(updated DESC);
CREATE TABLE IF NOT EXISTS help_request_event(
 id INTEGER PRIMARY KEY, request TEXT NOT NULL REFERENCES help_request(id),
 actor TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS help_events_by_request ON help_request_event(request, id);
"""
TOPICS = (
    ("Move from Vimeo", "Preview the account inventory, import privately and check missing originals and old embeds.",
     "/operator/migration", "migration transfer export"),
    ("Video privacy and embeds", "Open a video in Library to choose link privacy, allowed websites and downloads.",
     "/operator/library", "unlisted private password sharing permissions hosting playback"),
    ("Captions and subtitles", "Open a video’s Languages and subtitles page to upload and edit tracks.",
     "/operator/library", "textflow accessibility languages transcripts"),
    ("Schedule or stream an event", "Open Live events to schedule a session or prepare an encoder or browser studio.",
     "/operator/live-events", "live webinar event rtmp replay broadcast"),
    ("Invite teammates", "Open Team members to invite people and assign access to a shared folder or video.",
     "/team/members", "roles membership administration collaborator"),
    ("Play a video shared with your team", "Sign in to your team account to see the videos that were shared with you.",
     "/team", "team password member sign in shared video"),
    ("Report a problem with a video", "Open the support and appeals page to report a specific work or follow an appeal.",
     "/support", "moderation complaint safety appeal report abuse"),
)
POPULAR = ("move from Vimeo", "privacy", "captions", "live events")
PAGE_SIZE = 50


def text_field(body, key, limit, label):
    value = body.get(key)
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or "\x00" in value:
        abort(400, f"Enter {label} of 1 to {limit:,} characters.")
    return value.strip()


def register(app, tx, origin, team_member, operator_name, contact, hours):
    @app.get("/help")
    def help_center():
        query = request.args.get("q", "")[:160].strip()
        search = query.casefold().split()
        matches = [topic[:3] for topic in TOPICS if all(word in " ".join(topic).casefold() for word in search)]
        return render_template("help_center.html", query=query, results=matches, popular=POPULAR,
                               team=request.args.get("team") == "1", contact=contact, hours=hours)

    @app.get("/help/requests")
    def help_requests_page():
        secure_site(origin)
        return private(render_template("help_requests.html", team=request.args.get("team") == "1",
                                       contact=contact, hours=hours))

    def identity(con):
        if request.headers.get("Authorization") == f"Bearer {SESSION_MARKER}":
            member = team_member(con)
            if member is None:
                abort(401, "Sign in to your team to view requests.")
            return actor_id(member), member["role"] in MANAGERS
        actor = operator_name(con)
        if not actor:
            abort(403, "Enter an operator credential to view requests.")
        return "operator:" + actor, True

    def input_for_request():
        secure = secure_site(origin)
        if request.method == "GET":
            same_origin(secure)
            return None
        return json_mutation(secure, "X-CuePorch-Team")

    def event(con, uid, actor, kind, at, message=""):
        con.execute("INSERT INTO help_request_event(request,actor,kind,message,at) VALUES(?,?,?,?,?)",
                    (uid, actor, kind, message, at))

    def thread(con, uid, actor, supervisor):
        ticket = con.execute("SELECT * FROM help_request WHERE id=?", (uid,)).fetchone()
        if ticket is None or (not supervisor and ticket["requester"] != actor):
            abort(404, "This request is unavailable.")
        history = []
        for row in con.execute("SELECT actor,kind,message,at FROM help_request_event WHERE request=? ORDER BY id", (uid,)):
            sender = "You" if row["actor"] == actor else (
                "Requester" if row["actor"] == ticket["requester"] else "Workspace support"
            )
            history.append(dict(sender=sender, kind=row["kind"], message=row["message"], at=row["at"]))
        return dict(id=uid, subject=ticket["subject"], state=ticket["state"], revision=ticket["revision"],
                    can_manage=supervisor, history=history)

    @app.route("/api/team/support/requests", methods=["GET", "POST"])
    def help_requests_api():
        body = input_for_request()
        with tx() as con:
            actor, supervisor = identity(con)
            if request.method == "POST":
                subject = text_field(body, "subject", 120, "a subject")
                message = text_field(body, "message", 8000, "a description")
                uid = uuid.uuid4().hex
                now = time.time()
                con.execute("INSERT INTO help_request VALUES(?,?,?,'open',?,?,1)", (uid, actor, subject, now, now))
                event(con, uid, actor, "message", now, message)
                return private(thread(con, uid, actor, supervisor), 201)
            inbox = request.args.get("view") == "inbox"
            if inbox and not supervisor:
                abort(403, "The support inbox requires Administrator or Owner access.")
            page = max(1, min(request.args.get("page", 1, type=int), 1_000_000))
            query = "SELECT id,subject,state,updated FROM help_request"
            where = "" if inbox else " WHERE requester=?"
            params = () if inbox else (actor,)
            rows = con.execute(query + where + " ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?",
                               (*params, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)).fetchall()
            return private(dict(requests=[dict(row) for row in rows[:PAGE_SIZE]], page=page,
                                next=len(rows) > PAGE_SIZE, can_manage=supervisor))

    @app.route("/api/team/support/requests/<uid>", methods=["GET", "POST", "PUT"])
    def help_thread_api(uid):
        body = input_for_request()
        with tx() as con:
            actor, supervisor = identity(con)
            ticket = con.execute("SELECT * FROM help_request WHERE id=?", (uid,)).fetchone()
            if ticket is None or (not supervisor and ticket["requester"] != actor):
                abort(404, "This request is unavailable.")
            if request.method == "POST":
                message = text_field(body, "message", 8000, "a reply")
                now = time.time()
                con.execute("UPDATE help_request SET state='open',updated=?,revision=revision+1 WHERE id=?", (now, uid))
                if ticket["state"] == "resolved":
                    event(con, uid, actor, "reopened", now)
                event(con, uid, actor, "message", now, message)
            elif request.method == "PUT":
                if not supervisor:
                    abort(403, "Only workspace support can change request status.")
                if type(body.get("revision")) is not int or body["revision"] != ticket["revision"]:
                    abort(409, "The request changed. Open it again before updating its status.")
                state = body.get("state")
                if state not in ("open", "resolved"):
                    abort(400, "Choose Open or Resolved.")
                if state != ticket["state"]:
                    now = time.time()
                    con.execute("UPDATE help_request SET state=?,updated=?,revision=revision+1 WHERE id=?",
                                (state, now, uid))
                    event(con, uid, actor, "resolved" if state == "resolved" else "reopened", now)
            return private(thread(con, uid, actor, supervisor))
