"""Public watch discussions and their optional approval policy."""

import time
import uuid

from flask import abort, request

from audience_library import COLLECTION_ID, VIDEO_KEY
import community_analytics


STEP = "public_comments"
APPROVAL_PREFIX = "public-comment-"
PAGE_SIZE = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS audience_comment(
 id TEXT PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL, slug TEXT NOT NULL,
 body TEXT NOT NULL, created REAL NOT NULL, approval TEXT NOT NULL UNIQUE);
CREATE INDEX IF NOT EXISTS audience_comment_video ON audience_comment(kind,slug,created,id);
CREATE INDEX IF NOT EXISTS audience_comment_account ON audience_comment(account,created);
"""


def remove_account(con, account):
    con.execute("""UPDATE action SET status='rejected',decision='withdrawn' WHERE status='waiting'
                   AND id IN (SELECT approval FROM audience_comment WHERE account=?)""", (account,))
    con.execute("DELETE FROM audience_comment WHERE account=?", (account,))


def remove(con, row, actor=None):
    con.execute("""UPDATE action SET status='rejected',decision=?,actor=? WHERE id=?""",
                ("removed" if actor else "withdrawn", actor, row["approval"]))
    con.execute("DELETE FROM audience_comment WHERE id=?", (row["id"],))


def offset():
    page = request.args.get("page", "1")
    if not page.isascii() or not page.isdigit() or len(page) > 9 or int(page) < 1:
        abort(400, "Choose a valid page.")
    return (int(page) - 1) * PAGE_SIZE


def search(con):
    query = request.args.get("q", "").strip()
    sort = request.args.get("sort", "newest")
    if len(query) > 120 or sort not in ("newest", "oldest"):
        abort(400, "Choose a valid search and sort order.")
    con.create_function("casefold", 1, str.casefold, deterministic=True)
    return query.casefold(), "DESC" if sort == "newest" else "ASC", offset()


def payload(row, account):
    return dict(id=row["id"], author=row["name"], body=row["body"], created=row["created"],
                owned=row["account"] == account, status=row["status"])


def register(app, tx, viewer, mutation, listing, private, limit, action, staff):
    def eligible(con, kind, slug):
        if not VIDEO_KEY.fullmatch(slug) or (kind, slug) not in listing(con, (kind, slug)):
            abort(404)

    @app.route("/api/viewer/comments/<any(work,meeting):kind>/<slug>", methods=("GET", "POST"))
    def viewer_comments(kind, slug):
        post = request.method == "POST"
        if post:
            body = mutation().get("body")
            if not isinstance(body, str) or not 1 <= len(body.strip()) <= 2000 or "\0" in body:
                abort(400, "Enter a comment of up to 2,000 characters.")
        with tx() as con:
            eligible(con, kind, slug)
            account = viewer(con, required=post)
            author = account["id"] if account else None
            if post:
                limit(con, "comment", author)
                uid, now = uuid.uuid4().hex, time.time()
                approval = APPROVAL_PREFIX + uid
                status = action(con, approval, STEP, dict(comment=uid, kind=kind, video=slug))
                con.execute("INSERT INTO audience_comment VALUES(?,?,?,?,?,?,?)",
                            (uid, author, kind, slug, body.strip(), now, approval))
                community_analytics.observe_comment(con, uid, kind, now if status == "eligible" else None)
                return private(dict(status=status), 201)
            query, direction, start = search(con)
            rows = con.execute(f"""SELECT c.*,a.name,g.status FROM audience_comment c
                JOIN audience_account a ON a.id=c.account JOIN action g ON g.id=c.approval
                WHERE c.kind=? AND c.slug=? AND g.status='eligible'
                AND (instr(casefold(c.body),?)>0 OR instr(casefold(a.name),?)>0)
                ORDER BY c.created {direction},c.id {direction} LIMIT ? OFFSET ?""",
                               (kind, slug, query, query, PAGE_SIZE + 1, start)).fetchall()
            own = con.execute("""SELECT c.*,a.name,g.status FROM audience_comment c
                JOIN audience_account a ON a.id=c.account JOIN action g ON g.id=c.approval
                WHERE c.kind=? AND c.slug=? AND c.account=? AND g.status IN ('waiting','rejected')
                ORDER BY c.created DESC,c.id DESC LIMIT ?""", (kind, slug, author, PAGE_SIZE)).fetchall()
            return private(dict(signed_in=bool(account), comments=[payload(row, author) for row in rows[:PAGE_SIZE]],
                                own_unpublished=[payload(row, author) for row in own], more=len(rows) > PAGE_SIZE))

    @app.delete("/api/viewer/comments/<any(work,meeting):kind>/<slug>/<uid>")
    def viewer_comment_remove(kind, slug, uid):
        if not COLLECTION_ID.fullmatch(uid) or not VIDEO_KEY.fullmatch(slug):
            abort(404)
        mutation()
        with tx() as con:
            account = viewer(con)
            row = con.execute("SELECT * FROM audience_comment WHERE id=? AND kind=? AND slug=? AND account=?",
                              (uid, kind, slug, account["id"])).fetchone()
            if not row:
                abort(404)
            remove(con, row)
        return private("", 204)

    @app.get("/api/operator/public-comments")
    def operator_public_comments():
        staff()
        with tx() as con:
            query, direction, start = search(con)
            rows = con.execute(f"""SELECT c.*,a.name,g.status FROM audience_comment c
                JOIN audience_account a ON a.id=c.account JOIN action g ON g.id=c.approval
                WHERE g.status='eligible' AND (instr(casefold(c.body),?)>0 OR instr(casefold(a.name),?)>0)
                ORDER BY c.created {direction},c.id {direction} LIMIT ? OFFSET ?""",
                               (query, query, PAGE_SIZE + 1, start)).fetchall()
            return private(dict(comments=[payload(row, None) | {"kind": row["kind"], "video": row["slug"]}
                                          for row in rows[:PAGE_SIZE]], more=len(rows) > PAGE_SIZE))

    @app.delete("/api/operator/public-comments/<uid>")
    def operator_public_comment_remove(uid):
        actor = staff()
        if not COLLECTION_ID.fullmatch(uid):
            abort(404)
        with tx() as con:
            row = con.execute("SELECT * FROM audience_comment WHERE id=?", (uid,)).fetchone()
            if not row:
                abort(404)
            remove(con, row, actor)
        return private("", 204)
