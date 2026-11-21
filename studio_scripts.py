"""Persistent local script generation for the Record and Live teleprompters."""

import json
import time
from uuid import uuid4

from flask import abort, request
from werkzeug.exceptions import HTTPException

from browser_account_security import private
import local_model
import team_ai_policy


STEP = "ai_script"
SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_script_job(
 id TEXT PRIMARY KEY, event TEXT, opens INTEGER, requested_by TEXT NOT NULL, requested_member TEXT,
 created REAL NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL, result TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS studio_script_lookup ON studio_script_job(requested_by,event,opens,created);
"""
TEMPLATES = {
    "Announcement": "Announce an upcoming event, explain who can attend and share the date, place and next step.",
    "Bug report": "Explain the problem, how to reproduce it, what you expected and what happened instead.",
    "How-to": "Introduce a task, show the steps in order and explain how to tell when it worked.",
    "Interview": "Introduce a guest, ask open questions and close with a point for the audience to remember.",
    "Internal communications": "Explain a change to the team, who it affects, when it starts and what people need to do.",
    "Marketing": "Introduce a product or service, show how it helps the audience and suggest a clear next step.",
}
LENGTHS = {"Short": "up to 120 words", "Medium": "about 120 to 280 words", "Long": "about 280 to 530 words"}
TONES = ("Confident", "Funny", "Direct", "Inspiring", "Casual", "Formal")
CONTROLS = dict(script_templates=TEMPLATES, script_lengths=LENGTHS, script_tones=TONES)
OUTPUT = {"type": "object", "properties": {"script": {"type": "string", "minLength": 1, "maxLength": 30000}},
          "required": ["script"], "additionalProperties": False}


def read_options(body):
    if not isinstance(body, dict):
        abort(400, "Enter a script brief and style.")
    brief, template, tone, length, mode, prior = (body.get(key) for key in
                                                   ("brief", "template", "tone", "length", "mode", "prior"))
    if (not isinstance(brief, str) or len(brief.strip()) > 4000 or "\x00" in brief
            or (mode == "fresh" and not brief.strip())):
        abort(400, "Enter a script brief of 1 to 4,000 characters.")
    if (template not in ("", *TEMPLATES) or tone not in TONES
            or not isinstance(length, str) or length not in LENGTHS or mode not in ("fresh", "longer", "shorter")):
        abort(400, "Use the listed script template, tone, duration and revision choices.")
    if mode != "fresh" and (not isinstance(prior, str) or not 1 <= len(prior.strip()) <= 30000 or "\x00" in prior):
        abort(400, "Add a script to the teleprompter before asking for a longer or shorter version.")
    return dict(brief=brief.strip(), template=template, tone=tone, length=length, mode=mode,
                prior=prior.strip() if mode != "fresh" else None)


def generate(options):
    mode = options["mode"]
    revision = "Write a new script." if mode == "fresh" else (
        "Make the supplied script longer while retaining its purpose and facts." if mode == "longer"
        else "Make the supplied script shorter while retaining its purpose and key facts."
    )
    prompt = ("Write teleprompter text in the language of the brief or supplied script. Use spoken lines and plain text. "
              "Avoid inventing factual details; use placeholders where information is missing. " + revision
              + f" Use a {options['tone'].lower()} tone and a target length of {LENGTHS[options['length']]}."
              + "\nScript instructions and source text:\n" + json.dumps(options, ensure_ascii=False))
    result = local_model.generate(prompt, OUTPUT)
    text = result.get("script") if isinstance(result, dict) else None
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 30000 or "\x00" in text:
        raise local_model.ModelError("The local model did not return a usable script.")
    return text.strip()


def register(app, tx, database, action, actor, occurrence_access):
    def show(con, name, member, uid, opens):
        row = con.execute("""SELECT j.id,j.state,j.result,j.error,a.status AS gate FROM studio_script_job j
            JOIN action a ON a.id=j.id WHERE requested_by=? AND event IS ? AND opens IS ?
            ORDER BY j.created DESC LIMIT 1""", (name, uid, opens)).fetchone()
        result = dict(row) if row else None
        if result:
            gate = result.pop("gate")
            if result["state"] == "queued" and gate in ("waiting", "rejected"):
                result["state"] = gate
        return private(dict(job=result, configured=bool(local_model.runtime()),
                            restriction=team_ai_policy.restriction(con, STEP, member)))

    @app.route("/api/studio/scripts", methods=("GET", "POST"), defaults={"uid": None, "opens": None},
               endpoint="studio_record_scripts")
    @app.route("/api/live-events/<uid>/<int:opens>/studio-scripts", methods=("GET", "POST"),
               endpoint="studio_event_scripts")
    def studio_scripts(uid, opens):
        request.max_content_length = 192 * 1024
        with tx() as con:
            name, member = actor(con)
            if uid:
                occurrence_access(con, uid, opens)
            if request.method == "POST":
                denied = team_ai_policy.restriction(con, STEP, member)
                if denied:
                    abort(403, denied)
                if not local_model.runtime():
                    abort(409, "Configure a local Ollama model before generating scripts.")
                options = read_options(request.get_json())
                pending = con.execute("""SELECT 1 FROM studio_script_job j JOIN action a ON a.id=j.id
                    WHERE requested_by=? AND event IS ? AND opens IS ? AND state IN ('queued','running')
                    AND a.status<>'rejected'""", (name, uid, opens)).fetchone()
                if pending:
                    abort(409, "A script is already queued for this Studio.")
                job = "studio-script-" + uuid4().hex
                action(con, job, STEP, dict(studio="live" if uid else "record", event=uid, occurrence=opens,
                                            template=options["template"], tone=options["tone"], length=options["length"]))
                con.execute("""INSERT INTO studio_script_job(id,event,opens,requested_by,requested_member,created,
                    options,state) VALUES(?,?,?,?,?,?,?,'queued')""",
                    (job, uid, opens, name, member, time.time(), json.dumps(options)))
            return show(con, name, member, uid, opens)

    def update(con, job, state, error=None, script=None):
        con.execute("UPDATE studio_script_job SET state=?,error=?,result=? WHERE id=?",
                    (state, error, script, job["id"]))

    def permitted(con, job):
        restriction = team_ai_policy.restriction(con, STEP, job["requested_member"])
        if restriction:
            update(con, job, "blocked", restriction)
            return False
        if job["event"]:
            try:
                occurrence_access(con, job["event"], job["opens"])
            except HTTPException:
                update(con, job, "blocked", "This event occurrence is no longer available.")
                return False
        return True

    def process(stopping):
        with tx() as con:
            job = con.execute("""SELECT j.*,a.status AS gate FROM studio_script_job j JOIN action a ON a.id=j.id
                WHERE j.state IN ('queued','running') AND a.status IN ('eligible','rejected') ORDER BY j.created LIMIT 1""").fetchone()
            if not job:
                return
            if job["gate"] == "rejected":
                update(con, job, "rejected")
                return
            if not permitted(con, job):
                return
            update(con, job, "running")
        try:
            script = generate(json.loads(job["options"]))
        except local_model.ModelError as error:
            if not stopping.is_set():
                with tx() as con:
                    update(con, job, "failed", str(error))
            return
        if not stopping.is_set():
            with tx() as con:
                if permitted(con, job):
                    update(con, job, "ready", script=script)

    return local_model.start_worker(app, database.parent / f".{database.name}-studio-script.lock",
                                    "CuePorch studio script worker", process)
