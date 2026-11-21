"""Scope a folder review to the videos currently placed in its visible branch."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_folder(
 id TEXT PRIMARY KEY, folder TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_folder_item(
 parent TEXT NOT NULL, work TEXT NOT NULL, link TEXT NOT NULL UNIQUE,
 PRIMARY KEY(parent,work));
"""


def members(con, folder, work=None):
    return {row[0] for row in con.execute("""
        WITH RECURSIVE branch(id) AS (
            SELECT id FROM library_folder
            WHERE id=? AND NOT EXISTS (SELECT 1 FROM library_deleted_item
                WHERE kind='folder' AND item=library_folder.id)
            UNION
            SELECT f.id FROM library_folder f JOIN branch b ON b.id=f.parent
            WHERE NOT EXISTS (SELECT 1 FROM library_deleted_item WHERE kind='folder' AND item=f.id)
        )
        SELECT p.work FROM library_placement p JOIN branch b ON b.id=p.folder
        WHERE (? IS NULL OR p.work=?)
            AND NOT EXISTS (SELECT 1 FROM library_deleted_item WHERE kind='work' AND item=p.work)
            AND NOT EXISTS (SELECT 1 FROM case_hold WHERE work=p.work AND active=1)
    """, (folder, work, work))}


def folder_for(con, uid):
    return con.execute("SELECT folder FROM review_folder WHERE id=?", (uid,)).fetchone()


def parent_for(con, uid):
    row = con.execute("SELECT parent FROM review_folder_item WHERE link=?", (uid,)).fetchone()
    return row["parent"] if row else None


def scope_active(con, uid, now):
    row = con.execute("""
        SELECT i.work,f.folder,r.enabled,r.expires
        FROM review_folder_item i
        LEFT JOIN review_folder f ON f.id=i.parent
        LEFT JOIN review_link r ON r.id=i.parent
        WHERE i.link=?
    """, (uid,)).fetchone()
    if row is None:
        return True
    if row["folder"] is None or not row["enabled"] or row["expires"] <= now:
        return False
    return row["work"] in members(con, row["folder"], row["work"])
