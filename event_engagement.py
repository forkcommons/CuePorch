"""Connect occurrence-scoped Q&A and polls to the current audience policy."""

import csv
from io import StringIO
from time import time

from flask import Response, abort, request

import event_polls
import event_questions
from library_registration import csv_cell


SCHEMA = event_polls.SCHEMA + event_questions.SCHEMA + """
CREATE TABLE IF NOT EXISTS live_event_shared_activity(
 feature TEXT NOT NULL, activity TEXT NOT NULL, viewer TEXT NOT NULL, sharing TEXT NOT NULL,
 PRIMARY KEY(feature,activity,viewer));
CREATE INDEX IF NOT EXISTS live_event_shared_activity_sharing ON live_event_shared_activity(sharing);
CREATE TABLE IF NOT EXISTS live_event_shared_qa_vote(
 question TEXT NOT NULL, viewer TEXT NOT NULL, sharing TEXT NOT NULL, sent REAL NOT NULL,
 PRIMARY KEY(question,viewer));
CREATE INDEX IF NOT EXISTS live_event_shared_qa_vote_sharing ON live_event_shared_qa_vote(sharing);
"""


ROW_LIMIT = 500


def activity_snapshot(con, job, completed_at):
    import json

    event = job["event"]
    opens = job["opens"]
    sharing = job["sharing"]

    played_count, live_ms, replay_ms = con.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN p.phase = 'live' THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN p.phase = 'live' THEN p.watched ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN p.phase = 'replay' THEN p.watched ELSE 0 END), 0)
          FROM live_attendee_session s
          JOIN live_playback p
            ON p.id = s.session
           AND p.event = ?
           AND p.opens = ?
           AND p.phase IN ('live', 'replay')
           AND p.played = 1
           AND p.seen <= ?
         WHERE s.sharing = ?
        """,
        (event, opens, completed_at, sharing),
    ).fetchone()

    attended_live = played_count > 0

    question_rows = con.execute(
        """
        SELECT q.sent AS sent, q.id AS id, q.question AS question
          FROM live_question q
          JOIN live_event_shared_activity sa
            ON sa.feature = 'qa'
           AND sa.activity = q.id
           AND sa.viewer = q.viewer
           AND sa.sharing = ?
         WHERE q.event = ?
           AND q.opens = ?
           AND q.name IS NOT NULL
           AND q.archived = 0
           AND q.state IN ('published', 'answered')
           AND q.sent <= ?
         ORDER BY q.sent ASC, q.id ASC
         LIMIT ?
        """,
        (sharing, event, opens, completed_at, ROW_LIMIT),
    ).fetchall()

    poll_rows = con.execute(
        """
        SELECT v.sent AS sent, p.id AS id, p.question AS question,
               p.choices AS choices, v.choice AS choice
          FROM live_poll p
          JOIN live_poll_vote v
            ON v.poll = p.id
          JOIN live_event_shared_activity sa
            ON sa.feature = 'poll'
           AND sa.activity = p.id
           AND sa.viewer = v.viewer
           AND sa.sharing = ?
         WHERE p.event = ?
           AND p.opens = ?
           AND v.sent <= ?
         ORDER BY v.sent ASC, p.id ASC
         LIMIT ?
        """,
        (sharing, event, opens, completed_at, ROW_LIMIT),
    ).fetchall()

    upvotes = [
        row[0].strip()
        for row in con.execute(
            '''
            SELECT q.question
            FROM live_event_shared_qa_vote AS sqv
            JOIN live_question AS q ON q.id = sqv.question
            JOIN live_question_vote AS v
              ON v.question = sqv.question
             AND v.viewer = sqv.viewer
            WHERE sqv.sharing = ?
              AND q.event = ?
              AND q.opens = ?
              AND q.name IS NOT NULL
              AND q.archived = 0
              AND q.state IN ('published', 'answered')
              AND q.sent <= ?
              AND sqv.sent <= ?
            ORDER BY sqv.sent, q.id
            LIMIT ?
            ''',
            (sharing, event, opens, completed_at, completed_at, ROW_LIMIT),
        )
        if row[0] is not None
    ]
    questions = []
    for row in question_rows:
        text = (row["question"] or "").strip()
        if text:
            questions.append(text)

    polls = []
    for row in poll_rows:
        try:
            choices = json.loads(row["choices"])
        except (TypeError, ValueError):
            continue
        index = row["choice"]
        if (
            not isinstance(choices, list)
            or type(index) is not int
            or not 0 <= index < len(choices)
            or not isinstance(choices[index], str)
        ):
            continue
        question = (row["question"] or "").strip()
        answer = choices[index].strip()
        if question and answer:
            polls.append(f"{question}: {answer}")

    return {
        "attended_live": attended_live,
        "live_ms": live_ms,
        "replay_ms": replay_ms,
        "questions": questions,
        "upvotes": upvotes,
        "polls": polls,
    }

def report(rows, filename):
    output = StringIO(newline="")
    writer = csv.writer(output)
    for row in rows:
        writer.writerow(csv_cell(value) for value in row)
    return Response(output.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store",
    })


def register(app, tx, staff, session, operator_access, audience_access, *, share_activity=None, share_upvotes=None):
    def overview(con, uid, opens, viewer=None, operator=False):
        return dict(qa=event_questions.overview(con, uid, opens, viewer, operator),
                    polls=event_polls.overview(con, uid, opens, viewer, operator))

    @app.route("/api/live-events/<uid>/<int:opens>/engagement", methods=["GET", "PUT", "POST"])
    def operator_event_engagement(uid, opens):
        staff()
        with tx() as con:
            operator_access(con, uid, opens)
            if request.method in ("PUT", "POST"):
                body = request.get_json()
                if not isinstance(body, dict):
                    abort(422, "Choose an engagement action.")
                feature = body.get("feature")
                if request.method == "PUT" and feature == "qa":
                    event_questions.save(con, uid, opens, body)
                elif request.method == "POST" and feature == "qa":
                    event_questions.operator_action(con, uid, opens, body)
                elif request.method == "POST" and feature == "poll":
                    event_polls.operator_action(con, uid, opens, body)
                else:
                    abort(422, "Choose Q&A or a poll.")
            return overview(con, uid, opens, operator=True)

    @app.get("/api/live-events/<uid>/<int:opens>/engagement/<feature>/export")
    def export_event_engagement(uid, opens, feature):
        staff()
        with tx() as con:
            operator_access(con, uid, opens)
            if feature == "qa":
                questions = con.execute("""SELECT q.*,
                        (SELECT COUNT(*) FROM live_question_vote WHERE question=q.id) AS votes
                        FROM live_question q WHERE q.event=? AND q.opens=? ORDER BY q.sent""", (uid, opens)).fetchall()
                rows = [("display_name", "question", "answer", "submitted_unix_time", "votes", "state", "archived")]
                rows.extend((row["name"] or "Anonymous", row["question"], row["answer"] or "", row["sent"],
                             row["votes"], row["state"], bool(row["archived"])) for row in questions)
                return report(rows, "cueporch-event-questions.csv")
            if feature == "poll":
                rows = [("question", "option", "votes", "correct", "state")]
                for poll in event_polls.overview(con, uid, opens, operator=True):
                    rows.extend((poll["question"], choice, poll["counts"][index], poll["correct"] == index, poll["state"])
                                for index, choice in enumerate(poll["choices"]))
                return report(rows, "cueporch-event-polls.csv")
            abort(404)

    @app.route("/events/<slug>/<int:opens>/engagement", methods=["GET", "POST"])
    def audience_event_engagement(slug, opens):
        with tx() as con:
            scope, active = audience_access(con, slug, opens)
            uid = scope[0]
            current = request.headers.get("X-CuePorch-Engagement-Session")
            token, viewer = session(scope, current)
            if request.method == "POST":
                if not active or not current or token != current:
                    abort(403, "Engagement is unavailable for this viewer.")
                body = request.get_json()
                if not isinstance(body, dict):
                    abort(422, "Choose an engagement action.")
                if body.get("feature") == "qa":
                    activity = event_questions.audience_action(con, uid, opens, viewer, body)
                elif body.get("feature") == "poll":
                    activity = event_polls.audience_action(con, uid, opens, viewer, body)
                else:
                    abort(422, "Choose Q&A or a poll.")
                if activity is not None and body["feature"] == "qa" and body["type"] == "vote":
                    if not body["enabled"]:
                        con.execute(
                            "DELETE FROM live_event_shared_qa_vote WHERE question = ? AND viewer = ?",
                            (activity, viewer),
                        )
                    elif share_upvotes is not None:
                        sharing = share_upvotes(con, scope)
                        if sharing:
                            con.execute(
                                "INSERT INTO live_event_shared_qa_vote(question, viewer, sharing, sent) VALUES (?, ?, ?, ?)",
                                (activity, viewer, sharing, time()),
                            )
                elif activity is not None and share_activity is not None:
                    sharing = share_activity(con, scope)
                    if sharing:
                        con.execute(
                            "INSERT INTO live_event_shared_activity(feature, activity, viewer, sharing) VALUES (?, ?, ?, ?)",
                            (body["feature"], activity, viewer, sharing),
                        )
            return dict(overview(con, uid, opens, viewer), session=token, active=active)
