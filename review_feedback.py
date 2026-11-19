"""Comments and review status scoped to a review link and an exact cut."""

import csv
import io
import math
import time
import uuid

from flask import Response, abort, request

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_feedback_permission(
 link TEXT PRIMARY KEY, allow_comment INTEGER NOT NULL,
 allow_status INTEGER NOT NULL, allow_resolved INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS review_comment(
 id TEXT PRIMARY KEY, link TEXT NOT NULL, version TEXT NOT NULL, parent TEXT,
 author TEXT NOT NULL, body TEXT NOT NULL, seconds REAL NOT NULL,
 created REAL NOT NULL, resolved INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS review_comment_cut
 ON review_comment(link,version,seconds,created);
CREATE TABLE IF NOT EXISTS review_decision(
 link TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL,
 PRIMARY KEY(link,version));
"""

OPTIONS = {
    "allow_comment": True,
    "allow_status": False,
    "allow_resolved": False,
}
STATUSES = ("In progress", "Needs review", "Approved")


def settings(con, uid):
    row = con.execute(
        "SELECT * FROM review_feedback_permission WHERE link=?", (uid,)
    ).fetchone()
    return {key: bool(row[key]) for key in OPTIONS} if row else OPTIONS.copy()


def save_settings(con, uid, body):
    values = {option: body.get(option, default) for option, default in OPTIONS.items()}
    if any(type(value) is not bool for value in values.values()):
        abort(400, "Use a valid review permission.")
    con.execute(
        "INSERT OR REPLACE INTO review_feedback_permission VALUES (?,?,?,?)",
        (uid, *values.values()),
    )


def register(app, tx, scoped_access, export_access, confidential):
    def get_comments(con, uid, version, show_resolved):
        return [
            dict(row)
            for row in con.execute(
                """SELECT c.id,c.parent,c.author,c.body,c.seconds,c.created,c.resolved
                   FROM review_comment c LEFT JOIN review_comment p ON p.id=c.parent
                   WHERE c.link=? AND c.version=? AND (? OR (c.resolved=0 AND coalesce(p.resolved,0)=0))
                   ORDER BY c.seconds,c.created,c.id""",
                (uid, version, show_resolved),
            )
        ]

    @app.get("/review/<uid>/feedback/<version>")
    def review_feedback(uid, version):
        with tx() as con:
            _, rights = scoped_access(con, uid, version)
            status = con.execute(
                "SELECT status FROM review_decision WHERE link=? AND version=?",
                (uid, version),
            ).fetchone()
            result = {
                "permissions": rights,
                "status": status[0] if status else STATUSES[0],
                "comments": get_comments(con, uid, version, rights["allow_resolved"]),
            }
        return confidential(result)

    @app.post("/review/<uid>/feedback/<version>")
    def write_comment(uid, version):
        request.max_content_length = 16384
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        author = body.get("author")
        text = body.get("body")
        if (
            not isinstance(author, str)
            or not 1 <= len(author.strip()) <= 80
            or not isinstance(text, str)
            or not 1 <= len(text.strip()) <= 3000
        ):
            abort(400, "Enter a name and a comment.")
        with tx() as con:
            cut, rights = scoped_access(con, uid, version)
            if not rights["allow_comment"]:
                abort(403)
            parent = body.get("parent")
            if parent is not None:
                thread = (
                    con.execute(
                        """SELECT seconds,resolved FROM review_comment
                       WHERE id=? AND link=? AND version=? AND parent IS NULL""",
                        (parent, uid, version),
                    ).fetchone()
                    if isinstance(parent, str)
                    else None
                )
                if not thread:
                    abort(404)
                if thread["resolved"]:
                    abort(409, "This comment thread is resolved.")
                seconds = thread["seconds"]
            else:
                seconds = body.get("seconds")
                duration = cut.get("duration_seconds")
                if (
                    type(seconds) not in (int, float)
                    or not 0 <= seconds <= 30 * 86400
                    or not math.isfinite(seconds)
                    or (duration is not None and seconds > duration + 1)
                ):
                    abort(400, "Choose a time within the video.")
            comment = uuid.uuid4().hex
            con.execute(
                """INSERT INTO review_comment (id,link,version,parent,author,body,seconds,created)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    comment,
                    uid,
                    version,
                    parent,
                    author.strip(),
                    text.strip(),
                    seconds,
                    time.time(),
                ),
            )
        return confidential(({"id": comment}, 201))

    @app.put("/review/<uid>/feedback/<version>/status")
    def update_review_status(uid, version):
        request.max_content_length = 4096
        value = request.get_json()
        status = value.get("status") if isinstance(value, dict) else None
        if status not in STATUSES:
            abort(400)
        with tx() as con:
            _, rights = scoped_access(con, uid, version)
            if not rights["allow_status"]:
                abort(403)
            con.execute(
                "INSERT OR REPLACE INTO review_decision VALUES (?,?,?)",
                (uid, version, status),
            )
        return confidential(("", 204))

    @app.put("/review/<uid>/feedback/<version>/<comment>/resolved")
    def resolve_review_comment(uid, version, comment):
        request.max_content_length = 4096
        value = request.get_json()
        resolved = value.get("resolved") if isinstance(value, dict) else None
        if type(resolved) is not bool:
            abort(400)
        with tx() as con:
            export_access(con, uid, version)
            changed = con.execute(
                """UPDATE review_comment SET resolved=? WHERE link=? AND version=?
                   AND id=? AND parent IS NULL""",
                (resolved, uid, version, comment),
            )
            if not changed.rowcount:
                abort(404)
        return confidential(("", 204))

    @app.get("/review/<uid>/feedback/<version>/comments.csv")
    def export_review_comments(uid, version):
        with tx() as con:
            export_access(con, uid, version)
            comments = get_comments(con, uid, version, True)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(("Seconds", "Name", "Comment", "Thread", "Resolved"))
        for row in comments:
            author, body = [
                "'" + value
                if value.lstrip().startswith(("=", "+", "-", "@"))
                else value
                for value in (row["author"], row["body"])
            ]
            writer.writerow(
                (
                    row["seconds"],
                    author,
                    body,
                    row["parent"] or row["id"],
                    bool(row["resolved"]),
                )
            )
        return confidential(
            Response(
                output.getvalue(),
                mimetype="text/csv",
                headers={
                    "Content-Disposition": 'attachment; filename="review-comments.csv"'
                },
            )
        )
