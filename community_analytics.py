"""Optional analytics observations for public video likes and comments."""

import time

from library_analytics import permitted


SCHEMA = """
CREATE TABLE IF NOT EXISTS audience_like_reporting(
 account TEXT NOT NULL, kind TEXT NOT NULL, slug TEXT NOT NULL, PRIMARY KEY(account,kind,slug));
CREATE TABLE IF NOT EXISTS audience_comment_reporting(comment TEXT PRIMARY KEY, published REAL);
CREATE INDEX IF NOT EXISTS audience_comment_report_pending ON audience_comment_reporting(comment) WHERE published IS NULL;
CREATE TRIGGER IF NOT EXISTS unobserve_audience_like AFTER DELETE ON audience_like BEGIN
 DELETE FROM audience_like_reporting WHERE account=OLD.account AND kind=OLD.kind AND slug=OLD.slug;
END;
CREATE TRIGGER IF NOT EXISTS unobserve_audience_comment AFTER DELETE ON audience_comment BEGIN
 DELETE FROM audience_comment_reporting WHERE comment=OLD.id;
END;
"""


def observe_like(con, account, kind, slug):
    if kind == "work" and permitted():
        con.execute("INSERT INTO audience_like_reporting VALUES(?,?,?)", (account, kind, slug))


def observe_comment(con, uid, kind, published):
    if kind == "work" and permitted():
        con.execute("INSERT INTO audience_comment_reporting VALUES(?,?)", (uid, published))


def publish_approved(con, approval=None):
    approved = """SELECT 1 FROM audience_comment c JOIN action a ON a.id=c.approval
                  WHERE c.id=audience_comment_reporting.comment AND a.status='eligible'"""
    params = (time.time(),)
    if approval is not None:
        approved += " AND a.id=?"
        params += (approval,)
    con.execute(f"""UPDATE audience_comment_reporting SET published=? WHERE published IS NULL
                    AND EXISTS ({approved})""", params)
