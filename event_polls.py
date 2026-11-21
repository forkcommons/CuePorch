"""Stage and publish per-occurrence audience polls and quizzes."""

import json
import time
import uuid

from flask import abort

from library_interactivity import text_field


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_poll(
 id TEXT PRIMARY KEY, event TEXT NOT NULL, opens INTEGER NOT NULL, question TEXT NOT NULL,
 choices TEXT NOT NULL, correct INTEGER, state TEXT NOT NULL, changed REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS live_poll_one_open ON live_poll(event,opens) WHERE state='open';
CREATE TABLE IF NOT EXISTS live_poll_vote(
 poll TEXT NOT NULL, viewer TEXT NOT NULL, choice INTEGER NOT NULL, sent REAL NOT NULL,
 PRIMARY KEY(poll,viewer));
"""


def overview(con, uid, opens, viewer=None, operator=False):
    query = ("SELECT * FROM live_poll WHERE event=? AND opens=? " + ("ORDER BY changed DESC" if operator else
             "AND state IN ('open','results') ORDER BY state='open' DESC,changed DESC LIMIT 1"))
    results = []
    for row in con.execute(query, (uid, opens)):
        identifier = row["id"]
        choices = json.loads(row["choices"])
        poll = dict(id=identifier, question=row["question"], choices=choices, state=row["state"], correct=None, counts=None, choice=None)
        if operator or row["state"] == "results":
            poll["correct"] = row["correct"]
            counts = dict(con.execute("SELECT choice,COUNT(*) FROM live_poll_vote WHERE poll=? GROUP BY choice", (identifier,)))
            poll["counts"] = [counts.get(index, 0) for index in range(len(choices))]
        if viewer is not None:
            vote = con.execute("SELECT choice FROM live_poll_vote WHERE poll=? AND viewer=?", (identifier, viewer)).fetchone()
            poll["choice"] = vote["choice"] if vote else None
        results.append(poll)
    return results


def read(con, uid, opens, identifier):
    row = con.execute("SELECT * FROM live_poll WHERE event=? AND opens=? AND id=?",
                      (uid, opens, identifier)).fetchone() if isinstance(identifier, str) else None
    if row is None:
        abort(404)
    return row


def poll_text(value, label):
    value = text_field(value, label, 120, True)
    if len(value.splitlines()) > 3:
        abort(422, "Keep each poll entry to three lines.")
    return value


def operator_action(con, uid, opens, body):
    kind = body.get("type")
    if kind == "draft":
        question = poll_text(body.get("question"), "a poll question")
        choices = body.get("choices")
        if not isinstance(choices, list) or not 2 <= len(choices) <= 4:
            abort(422, "Enter two to four options.")
        choices = [poll_text(choice, "an option") for choice in choices]
        if len(set(choice.casefold() for choice in choices)) != len(choices):
            abort(422, "Use a different label for each option.")
        correct = body.get("correct")
        if correct is not None and (type(correct) is not int or not 0 <= correct < len(choices)):
            abort(422, "Choose an available correct answer.")
        identifier = body.get("id")
        if identifier:
            row = read(con, uid, opens, identifier)
            if row["state"] != "draft":
                abort(409, "Only a draft can be edited.")
            con.execute("UPDATE live_poll SET question=?,choices=?,correct=?,changed=? WHERE id=?",
                        (question, json.dumps(choices), correct, time.time(), identifier))
        else:
            con.execute("INSERT INTO live_poll VALUES(?,?,?,?,?,?,'draft',?)",
                        (uuid.uuid4().hex, uid, opens, question, json.dumps(choices), correct, time.time()))
        return
    poll = read(con, uid, opens, body.get("id"))
    transitions = {"open": ("draft", "open"), "results": ("open", "results"), "archive": ("results", "archived")}
    if not isinstance(kind, str) or kind not in transitions or poll["state"] != transitions[kind][0]:
        abort(409, "This poll changed or the action is unavailable.")
    if kind == "open" and con.execute("SELECT 1 FROM live_poll WHERE event=? AND opens=? AND state='open'", (uid, opens)).fetchone():
        abort(409, "Publish the current poll's results before opening another.")
    con.execute("UPDATE live_poll SET state=?,changed=? WHERE id=?", (transitions[kind][1], time.time(), poll["id"]))


def audience_action(con, uid, opens, viewer, body):
    poll = read(con, uid, opens, body.get("id"))
    if poll["state"] != "open":
        abort(409, "This poll is closed.")
    choice = body.get("choice")
    if type(choice) is not int or not 0 <= choice < len(json.loads(poll["choices"])):
        abort(422, "Select an available answer.")
    inserted = con.execute("INSERT OR IGNORE INTO live_poll_vote VALUES(?,?,?,?)", (poll["id"], viewer, choice, time.time()))
    return poll["id"] if inserted.rowcount == 1 else None
