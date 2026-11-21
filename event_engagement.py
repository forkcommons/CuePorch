"""Connect occurrence-scoped Q&A and polls to the current audience policy."""

import csv
from io import StringIO

from flask import Response, abort, request

import event_polls
import event_questions
from library_registration import csv_cell


SCHEMA = event_polls.SCHEMA + event_questions.SCHEMA + """
CREATE TABLE IF NOT EXISTS live_event_shared_activity(
 feature TEXT NOT NULL, activity TEXT NOT NULL, viewer TEXT NOT NULL, sharing TEXT NOT NULL,
 PRIMARY KEY(feature,activity,viewer));
CREATE INDEX IF NOT EXISTS live_event_shared_activity_sharing ON live_event_shared_activity(sharing);
"""


def report(rows, filename):
    output = StringIO(newline="")
    writer = csv.writer(output)
    for row in rows:
        writer.writerow(csv_cell(value) for value in row)
    return Response(output.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store",
    })


def register(app, tx, staff, session, operator_access, audience_access, *, share_activity=None):
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
                if activity is not None and share_activity is not None:
                    sharing = share_activity(con, scope)
                    if sharing:
                        con.execute(
                            "INSERT INTO live_event_shared_activity(feature, activity, viewer, sharing) VALUES (?, ?, ?, ?)",
                            (body["feature"], activity, viewer, sharing),
                        )
            return dict(overview(con, uid, opens, viewer), session=token, active=active)
