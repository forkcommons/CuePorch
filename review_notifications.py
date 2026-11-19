"""Account-free review mentions and a cut-specific notification inbox."""

from flask import abort

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_comment_actor(
 comment TEXT PRIMARY KEY, actor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_notice_preferences(
 link TEXT NOT NULL, actor TEXT NOT NULL, mode TEXT NOT NULL,
 PRIMARY KEY(link,actor));
CREATE TABLE IF NOT EXISTS review_notice(
 link TEXT NOT NULL, version TEXT NOT NULL, actor TEXT NOT NULL,
 comment TEXT NOT NULL, reason TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(link,version,actor,comment));
"""
MODES = ("all", "mentions", "off")


def participants(con, uid, version, visible):
    authors = {item["id"]: item["author"] for item in visible}
    seen = set()
    result = []
    for row in con.execute(
        """SELECT a.actor,a.comment FROM review_comment_actor a
           JOIN review_comment c ON c.id=a.comment WHERE c.link=? AND c.version=?
           ORDER BY c.created DESC,c.id""",
        (uid, version),
    ):
        if row["comment"] in authors and row["actor"] not in seen:
            seen.add(row["actor"])
            result.append(
                {
                    "id": row["comment"],
                    "name": authors[row["comment"]],
                    "actor": row["actor"],
                }
            )
    return result


def targets(con, uid, version, visible, text, mentions):
    if (
        not isinstance(mentions, list)
        or len(mentions) > 10
        or any(not isinstance(mention, str) for mention in mentions)
    ):
        abort(400, "Choose up to 10 reviewers to mention.")
    selected = set(mentions)
    if not selected:
        return set()
    body = text.casefold()
    visible_ids = {
        item["id"]
        for item in visible
        if item["id"] in selected and ("@" + item["author"].casefold()) in body
    }
    if not visible_ids:
        return set()
    return {
        row["actor"]
        for row in con.execute(
            """SELECT a.actor,a.comment FROM review_comment_actor a JOIN review_comment c ON c.id=a.comment
               WHERE c.link=? AND c.version=?""",
            (uid, version),
        )
        if row["comment"] in visible_ids
    }


def inbox(con, uid, version, actor, show_resolved):
    if not actor:
        return {"mode": "mentions", "items": []}
    setting = con.execute(
        "SELECT mode FROM review_notice_preferences WHERE link=? AND actor=?",
        (uid, actor),
    ).fetchone()
    unread = con.execute(
        """SELECT n.comment,n.reason,c.author,c.created FROM review_notice n
           JOIN review_comment c ON c.id=n.comment
           LEFT JOIN review_comment p ON p.id=c.parent
           WHERE n.link=? AND n.version=? AND n.actor=? AND n.read=0
           AND (? OR (c.resolved=0 AND coalesce(p.resolved,0)=0))
           ORDER BY c.created DESC,n.comment LIMIT 50""",
        (uid, version, actor, show_resolved),
    )
    return {
        "mode": setting[0] if setting else "mentions",
        "items": [dict(row) for row in unread],
    }


def set_inbox(con, uid, version, actor, body):
    if not actor:
        abort(403)
    if not isinstance(body, dict):
        abort(400)
    if "mode" in body:
        mode = body["mode"]
        if mode not in MODES:
            abort(400)
        con.execute(
            """INSERT INTO review_notice_preferences VALUES (?,?,?)
               ON CONFLICT(link,actor) DO UPDATE SET mode=excluded.mode""",
            (uid, actor, mode),
        )
    if "read" in body:
        if not isinstance(body["read"], str):
            abort(400)
        con.execute(
            "UPDATE review_notice SET read=1 WHERE link=? AND version=? AND actor=? AND comment=?",
            (uid, version, actor, body["read"]),
        )


def record(con, uid, version, comment, actor, reply_to, mention_targets):
    if actor:
        con.execute("INSERT INTO review_comment_actor VALUES (?,?)", (comment, actor))
    recipients = {
        row[0]: "comment"
        for row in con.execute(
            "SELECT actor FROM review_notice_preferences WHERE link=? AND mode='all'",
            (uid,),
        )
    }
    if reply_to:
        parent = con.execute(
            "SELECT actor FROM review_comment_actor WHERE comment=?", (reply_to,)
        ).fetchone()
        if parent:
            recipients[parent[0]] = "reply"
    recipients.update(dict.fromkeys(mention_targets, "mention"))
    recipients.pop(actor, None)
    opted_out = {
        row[0]
        for row in con.execute(
            "SELECT actor FROM review_notice_preferences WHERE link=? AND mode='off'",
            (uid,),
        )
    }
    con.executemany(
        "INSERT INTO review_notice (link,version,actor,comment,reason) VALUES (?,?,?,?,?)",
        (
            (uid, version, recipient, comment, reason)
            for recipient, reason in recipients.items()
            if recipient not in opted_out
        ),
    )


def forget(con, uid):
    con.execute(
        "DELETE FROM review_comment_actor WHERE comment IN (SELECT id FROM review_comment WHERE link=?)",
        (uid,),
    )
    con.execute("DELETE FROM review_notice WHERE link=?", (uid,))
    con.execute("DELETE FROM review_notice_preferences WHERE link=?", (uid,))
