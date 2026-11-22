"""Public watch discussions and their optional approval policy."""

import time
import uuid

from flask import abort, request

from audience_library import COLLECTION_ID, VIDEO_KEY
import community_analytics
import creator_blocks


STEP = "public_comments"
APPROVAL_PREFIX = "public-comment-"
PAGE_SIZE = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS audience_comment(
 id TEXT PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL, slug TEXT NOT NULL,
 body TEXT NOT NULL, created REAL NOT NULL, approval TEXT NOT NULL UNIQUE);
CREATE INDEX IF NOT EXISTS audience_comment_video ON audience_comment(kind,slug,created,id);
CREATE INDEX IF NOT EXISTS audience_comment_account ON audience_comment(account,created);
CREATE TABLE IF NOT EXISTS vimeo_viewer_comment (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    parent TEXT,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created REAL NOT NULL,
    source_public INTEGER NOT NULL CHECK (source_public IN (0, 1)),
    source_listed INTEGER NOT NULL DEFAULT 1,
    removed INTEGER NOT NULL DEFAULT 0,
    approval TEXT UNIQUE
);

CREATE INDEX IF NOT EXISTS vimeo_viewer_comment_slug_parent_idx
    ON vimeo_viewer_comment (slug, parent);
"""

PUBLISHED = '''WITH published AS (
    SELECT c.id, c.account, c.kind, c.slug, c.body, c.created, a.name, g.status, NULL AS origin, NULL AS parent
    FROM audience_comment c
    JOIN audience_account a ON a.id = c.account
    JOIN action g ON g.id = c.approval
    WHERE g.status = 'eligible'
    UNION ALL
    SELECT v.id, NULL, 'work', v.slug, v.body, v.created, v.author, g.status, 'vimeo', v.parent
    FROM vimeo_viewer_comment v
    JOIN action g ON g.id = v.approval
    WHERE v.source_public = 1
      AND v.source_listed = 1
      AND v.removed = 0
      AND g.status = 'eligible'
      AND (
          v.parent IS NULL OR EXISTS (
              SELECT 1
              FROM vimeo_viewer_comment p
              JOIN action pg ON pg.id = p.approval
              WHERE p.id = v.parent
                AND p.slug = v.slug
                AND p.source_public = 1
                AND p.source_listed = 1
                AND p.removed = 0
                AND pg.status = 'eligible'
          )
      )
)'''


def save_vimeo_viewer_comment(con, row, slug, parent, public, action):
    approval = "vimeo-comment-" + row["id"] if public else None
    if public:
        action(con, approval, STEP, {"comment": row["id"], "kind": "work", "video": slug})
    con.execute(
        """
        INSERT INTO vimeo_viewer_comment
            (id, slug, parent, author, body, created, source_public, approval)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            author = excluded.author,
            body = excluded.body,
            created = excluded.created,
            source_public = excluded.source_public,
            source_listed = 1,
            approval = COALESCE(excluded.approval, vimeo_viewer_comment.approval)
        WHERE vimeo_viewer_comment.slug = excluded.slug
          AND vimeo_viewer_comment.parent IS excluded.parent
        """,
        (
            row["id"],
            slug,
            parent,
            row["author"],
            row["body"],
            row["created"],
            1 if public else 0,
            approval,
        ),
    )


def import_vimeo_viewer_comments(con, incoming, action):
    for work in incoming:
        if work.get('provider') != 'vimeo':
            continue
        slug = work['id']
        public = work.get('source_privacy') in ('anybody', 'public')
        if not public:
            con.execute("UPDATE vimeo_viewer_comment SET source_public=0 WHERE slug=?", (slug,))
        inventory = work.get('source_viewer_comments', {})
        if inventory.get('state') != 'available':
            continue
        con.execute("UPDATE vimeo_viewer_comment SET source_listed=0 WHERE slug=? AND parent IS NULL", (slug,))
        for root in inventory["roots"]:
            save_vimeo_viewer_comment(con, root, slug, None, public, action)
            if root["replies_available"]:
                con.execute("UPDATE vimeo_viewer_comment SET source_listed=0 WHERE slug=? AND parent=?", (slug, root['id']))
                for reply in root["replies"]:
                    save_vimeo_viewer_comment(con, reply, slug, root['id'], public, action)


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
    entry = dict(id=row["id"], author=row["name"], body=row["body"], created=row["created"],
                 owned=account is not None and row["account"] == account, status=row["status"])
    if "origin" in row.keys() and row["origin"]:
        entry["origin"] = row["origin"]
    return entry


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
                if creator_blocks.video_blocked(con, author, kind, slug):
                    abort(403, "You cannot comment on this video.")
                limit(con, "comment", author)
                uid, now = uuid.uuid4().hex, time.time()
                approval = APPROVAL_PREFIX + uid
                status = action(con, approval, STEP, dict(comment=uid, kind=kind, video=slug))
                con.execute("INSERT INTO audience_comment VALUES(?,?,?,?,?,?,?)",
                            (uid, author, kind, slug, body.strip(), now, approval))
                community_analytics.observe_comment(con, uid, kind, now if status == "eligible" else None)
                return private(dict(status=status), 201)
            query, direction, start = search(con)
            rows = con.execute(PUBLISHED + f"""
                SELECT c.*
                FROM PUBLISHED c
                WHERE c.kind=? AND c.slug=? AND c.parent IS NULL
                  AND (
                    instr(casefold(c.body),?)>0
                    OR instr(casefold(c.name),?)>0
                    OR EXISTS (
                      SELECT 1 FROM PUBLISHED child
                      WHERE child.parent=c.id AND child.slug=c.slug
                        AND (instr(casefold(child.body),?)>0 OR instr(casefold(child.name),?)>0)
                    )
                  )
                ORDER BY c.created {direction}, c.id {direction}
                LIMIT ? OFFSET ?
            """, (kind, slug, query, query, query, query, PAGE_SIZE + 1, start)).fetchall()
            page_rows = rows[:PAGE_SIZE]
            root_ids = [row['id'] for row in page_rows]
            replies = {}
            if root_ids:
                placeholders = ','.join('?' for _ in root_ids)
                child_rows = con.execute(
                    PUBLISHED
                    + f'''
                    SELECT *
                    FROM published
                    WHERE parent IN ({placeholders})
                    ORDER BY created ASC, id ASC
                    ''',
                    root_ids,
                ).fetchall()
                for child in child_rows:
                    replies.setdefault(child['parent'], []).append(payload(child, author))
            comments = [
                payload(row, author) | {'replies': replies.get(row['id'], [])}
                if row['origin'] else payload(row, author)
                for row in page_rows
            ]
            own = con.execute("""SELECT c.*,a.name,g.status FROM audience_comment c
                JOIN audience_account a ON a.id=c.account JOIN action g ON g.id=c.approval
                WHERE c.kind=? AND c.slug=? AND c.account=? AND g.status IN ('waiting','rejected')
                ORDER BY c.created DESC,c.id DESC LIMIT ?""", (kind, slug, author, PAGE_SIZE)).fetchall()
            return private(dict(signed_in=bool(account), comments=comments,
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
            rows = con.execute(
                PUBLISHED + f"""
                SELECT * FROM published
                WHERE (instr(casefold(body), ?) > 0 OR instr(casefold(name), ?) > 0)
                ORDER BY created {direction}, id {direction}
                LIMIT ? OFFSET ?
                """,
                (query, query, PAGE_SIZE + 1, start),
            ).fetchall()
            return private(dict(comments=[payload(row, None) | {"kind": row["kind"], "video": row["slug"]}
                                          for row in rows[:PAGE_SIZE]], more=len(rows) > PAGE_SIZE))

    @app.delete("/api/operator/public-comments/<uid>")
    def operator_public_comment_remove(uid):
        actor = staff()
        if not COLLECTION_ID.fullmatch(uid):
            abort(404)
        with tx() as con:
            row = con.execute("SELECT * FROM audience_comment WHERE id=?", (uid,)).fetchone()
            if row:
                remove(con, row, actor)
                return private("", 204)
            row = con.execute(
                PUBLISHED + "SELECT id FROM published WHERE id=? AND origin='vimeo'", (uid,),
            ).fetchone()
            if not row:
                abort(404)
            con.execute("UPDATE vimeo_viewer_comment SET removed=1 WHERE id=?", (uid,))
            con.execute(
                """UPDATE action SET status='rejected',decision='removed',actor=?
                   WHERE id=(SELECT approval FROM vimeo_viewer_comment WHERE id=?)""",
                (actor, uid),
            )
        return private("", 204)
