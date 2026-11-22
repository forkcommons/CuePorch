"""Creator-scoped blocks for viewer interactions."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS creator_viewer_block (
    member TEXT NOT NULL,
    account TEXT NOT NULL,
    blocked REAL NOT NULL,
    PRIMARY KEY (member, account)
);
"""

def blocked(con, member, account):
    cur = con.execute("SELECT 1 FROM creator_viewer_block WHERE member=? AND account=? LIMIT 1", (member, account))
    return cur.fetchone() is not None

def video_blocked(con, account, kind, slug):
    if kind != 'work':
        return False
    cur = con.execute(
        "SELECT 1 FROM team_owned_work w JOIN creator_viewer_block b ON b.member=w.member AND b.account=? WHERE w.work=? LIMIT 1",
        (account, slug)
    )
    return cur.fetchone() is not None

def eligible(con, member, account):
    cur = con.execute(
        """
        SELECT EXISTS(SELECT 1 FROM audience_account WHERE id=?)
           AND (
               EXISTS(SELECT 1 FROM audience_creator_follow WHERE member=? AND account=?)
               OR EXISTS(SELECT 1 FROM audience_comment c JOIN team_owned_work w ON w.work=c.slug AND w.member=? WHERE c.account=? AND c.kind='work')
               OR EXISTS(SELECT 1 FROM creator_viewer_block WHERE member=? AND account=?)
           )
        """,
        (account, member, account, member, account, member, account)
    )
    return bool(cur.fetchone()[0])

def candidates(con, member, page):
    offset = (page - 1) * 30
    cur = con.execute(
        """
        WITH rel AS (
            SELECT account FROM audience_creator_follow WHERE member=?
            UNION
            SELECT c.account FROM audience_comment c JOIN team_owned_work w ON w.work=c.slug AND w.member=? WHERE c.kind='work'
            UNION
            SELECT account FROM creator_viewer_block WHERE member=?
        )
        SELECT a.id, a.name, CASE WHEN b.account IS NOT NULL THEN 1 ELSE 0 END AS blocked
        FROM rel
        JOIN audience_account a ON a.id=rel.account
        LEFT JOIN creator_viewer_block b ON b.member=? AND b.account=rel.account
        ORDER BY blocked DESC, a.name COLLATE NOCASE ASC, a.id ASC
        LIMIT 31 OFFSET ?
        """,
        (member, member, member, member, offset)
    )
    rows = cur.fetchall()
    items = [{"id": r[0], "name": r[1], "blocked": bool(r[2])} for r in rows[:30]]
    return {"items": items, "more": len(rows) > 30}
