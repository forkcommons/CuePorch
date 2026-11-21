"""Live Q&A scoped to an event occurrence and its audience."""

import time
import uuid

from flask import abort

from library_interactivity import text_field


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_qa_settings(
 event TEXT NOT NULL, opens INTEGER NOT NULL, enabled INTEGER NOT NULL,
 anonymous INTEGER NOT NULL, review INTEGER NOT NULL, highlighted TEXT,
 PRIMARY KEY(event,opens));
CREATE TABLE IF NOT EXISTS live_question(
 id TEXT PRIMARY KEY, event TEXT NOT NULL, opens INTEGER NOT NULL, viewer TEXT NOT NULL,
 name TEXT, question TEXT NOT NULL, answer TEXT, state TEXT NOT NULL, sent REAL NOT NULL,
 archived INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS live_question_history ON live_question(event,opens,sent);
CREATE TABLE IF NOT EXISTS live_question_vote(
 question TEXT NOT NULL, viewer TEXT NOT NULL, PRIMARY KEY(question,viewer));
"""


def settings(con, uid, opens):
    row = con.execute("""SELECT enabled,anonymous,review,highlighted FROM live_qa_settings
                         WHERE event=? AND opens=?""", (uid, opens)).fetchone()
    if row:
        return dict(enabled=bool(row["enabled"]), anonymous=bool(row["anonymous"]),
                    review=bool(row["review"]), highlighted=row["highlighted"])
    saved = con.execute("""SELECT json_extract(settings,'$.setup.qa.enabled'),
                         json_extract(settings,'$.setup.qa.anonymous'),json_extract(settings,'$.setup.qa.review')
                         FROM live_event WHERE id=?""", (uid,)).fetchone()
    return dict(enabled=bool(saved and saved[0]), anonymous=bool(saved and saved[1]),
                review=bool(saved and saved[2]), highlighted=None)


def save(con, uid, opens, body):
    keys = ("enabled", "anonymous", "review")
    if any(type(body.get(key)) is not bool for key in keys):
        abort(422, "Choose the audience Q&A settings.")
    con.execute("""INSERT INTO live_qa_settings VALUES(?,?,?,?,?,NULL) ON CONFLICT(event,opens) DO UPDATE SET
                   enabled=excluded.enabled,anonymous=excluded.anonymous,review=excluded.review""",
                (uid, opens, *(body[key] for key in keys)))
    if not body["review"]:
        con.execute("UPDATE live_question SET state='published' WHERE event=? AND opens=? AND state='pending'", (uid, opens))


def overview(con, uid, opens, viewer=None, operator=False):
    policy = settings(con, uid, opens)
    questions = []
    if operator or policy["enabled"]:
        for row in con.execute("""SELECT q.*,
                (SELECT COUNT(*) FROM live_question_vote WHERE question=q.id) AS votes,
                EXISTS(SELECT 1 FROM live_question_vote WHERE question=q.id AND viewer=?) AS voted
                FROM live_question q WHERE q.event=? AND q.opens=?
                AND (? OR (q.archived=0 AND (q.state<>'pending' OR q.viewer=?)))
                ORDER BY q.id=? DESC,q.sent DESC LIMIT 200""",
                (viewer, uid, opens, int(operator), viewer, policy["highlighted"])):
            questions.append(dict(id=row["id"], name=row["name"], question=row["question"], answer=row["answer"],
                                  state=row["state"], voted=bool(row["voted"]), votes=row["votes"],
                                  highlighted=policy["highlighted"] == row["id"], sent=row["sent"],
                                  archived=bool(row["archived"])))
    if not operator:
        policy = {key: policy[key] for key in ("enabled", "anonymous")}
    return dict(policy=policy, questions=questions)


def read(con, uid, opens, identifier):
    question = con.execute("SELECT * FROM live_question WHERE id=? AND event=? AND opens=?",
                           (identifier, uid, opens)).fetchone() if isinstance(identifier, str) else None
    if not question:
        abort(404)
    return question


def audience_action(con, uid, opens, viewer, body):
    policy = settings(con, uid, opens)
    if not policy["enabled"]:
        abort(409, "Audience Q&A is off.")
    if body.get("type") == "question":
        question = text_field(body.get("question"), "a question", 240, True)
        if len(question.splitlines()) > 3:
            abort(422, "Keep the question to three lines.")
        anonymous = body.get("anonymous") is True
        if anonymous and not policy["anonymous"]:
            abort(422, "This event requires a display name for questions.")
        name = None if anonymous else text_field(body.get("name"), "a display name", 60, True)
        now = time.time()
        last = con.execute("SELECT MAX(sent) FROM live_question WHERE event=? AND opens=? AND viewer=?",
                           (uid, opens, viewer)).fetchone()[0]
        if last and now - last < 2:
            abort(429, "Wait before submitting another question.")
        identifier = uuid.uuid4().hex
        con.execute("INSERT INTO live_question VALUES(?,?,?,?,?,?,NULL,?,?,0)",
                    (identifier, uid, opens, viewer, name, question, "pending" if policy["review"] else "published", now))
        return None if anonymous else identifier
    elif body.get("type") == "vote":
        question = read(con, uid, opens, body.get("id"))
        enabled = body.get("enabled")
        if question["archived"] or question["state"] not in ("published", "answered") or type(enabled) is not bool:
            abort(422, "Choose a visible question to vote on.")
        if enabled:
            cursor = con.execute("INSERT OR IGNORE INTO live_question_vote VALUES(?,?)", (question["id"], viewer))
            if cursor.rowcount == 1 and question["name"] is not None:
                return question["id"]
            return None
        else:
            con.execute("DELETE FROM live_question_vote WHERE question=? AND viewer=?", (question["id"], viewer))
            return question["id"]
    else:
        abort(422, "Choose an audience Q&A action.")


def operator_action(con, uid, opens, body):
    question = read(con, uid, opens, body.get("id"))
    kind, identifier = body.get("type"), question["id"]
    if kind == "restore" and question["archived"]:
        con.execute("UPDATE live_question SET archived=0 WHERE id=?", (identifier,))
        return
    if question["archived"]:
        abort(409, "Restore the archived question before editing it.")
    if kind == "publish" and question["state"] == "pending":
        con.execute("UPDATE live_question SET state='published' WHERE id=?", (identifier,))
    elif kind == "answer" and question["state"] in ("published", "answered"):
        answer = text_field(body.get("answer", ""), "an answer", 1000)
        con.execute("UPDATE live_question SET state='answered',answer=? WHERE id=?", (answer, identifier))
    elif kind == "highlight" and question["state"] in ("published", "answered"):
        con.execute("UPDATE live_qa_settings SET highlighted=? WHERE event=? AND opens=?",
                    (None if settings(con, uid, opens)["highlighted"] == identifier else identifier, uid, opens))
    elif kind == "archive":
        con.execute("UPDATE live_question SET archived=1 WHERE id=?", (identifier,))
        con.execute("UPDATE live_qa_settings SET highlighted=NULL WHERE event=? AND opens=? AND highlighted=?",
                    (uid, opens, identifier))
    else:
        abort(422, "Choose an available operator Q&A action.")
