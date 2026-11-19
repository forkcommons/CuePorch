"""Revocable, optional aliases for CuePorch watch links."""

import re
import secrets
import time
from urllib.parse import urlencode

from flask import abort, request

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_share_alias(
 work TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, revision TEXT NOT NULL,
 expires REAL, days INTEGER NOT NULL);
"""
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def resolve(con, slug):
    row = con.execute("SELECT * FROM library_share_alias WHERE slug=?", (slug,)).fetchone()
    if row is None or (row["expires"] is not None and row["expires"] <= time.time()):
        abort(404)
    return row


def current(con, uid, revision):
    return con.execute(
        """SELECT 1 FROM library_share_alias
           WHERE work=? AND revision=? AND (expires IS NULL OR expires>?)""",
        (uid, revision, time.time()),
    ).fetchone() is not None


def register(app, tx, staff, get_work, sharing_for):
    @app.route("/api/library/work/<uid>/share-link", methods=["GET", "PUT", "DELETE"])
    def share_link(uid):
        staff()
        with tx() as con:
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            if request.method == "DELETE":
                con.execute("DELETE FROM library_share_alias WHERE work=?", (uid,))
                return "", 204
            paths, secret = sharing_for(con, work)
            row = con.execute("SELECT * FROM library_share_alias WHERE work=?", (uid,)).fetchone()
            if request.method == "PUT":
                if not paths["watch"]:
                    abort(409, "Choose a viewer link privacy mode before creating a custom link.")
                body = request.get_json()
                slug = body.get("slug") if isinstance(body, dict) else None
                days = body.get("days") if isinstance(body, dict) else None
                if not isinstance(slug, str) or not 3 <= len(slug) <= 70 or not SLUG.fullmatch(slug):
                    abort(400, "Use 3 to 70 lowercase letters or numbers separated by hyphens.")
                if type(days) is not int or days not in (0, 1, 7, 30):
                    abort(400, "Choose a supported expiration.")
                clash = con.execute("SELECT work FROM library_share_alias WHERE slug=?", (slug,)).fetchone()
                if clash and clash["work"] != uid:
                    abort(409, "That link name is already assigned to a video.")
                expires = (time.time() + days * 86400) if days else None
                con.execute(
                    """INSERT INTO library_share_alias VALUES(?,?,?,?,?) ON CONFLICT(work) DO UPDATE SET
                       slug=excluded.slug,revision=excluded.revision,expires=excluded.expires,days=excluded.days""",
                    (uid, slug, secrets.token_urlsafe(18), expires, days),
                )
                row = con.execute("SELECT * FROM library_share_alias WHERE work=?", (uid,)).fetchone()
            alias = None
            if row:
                live = row["expires"] is None or row["expires"] > time.time()
                query = "?" + urlencode({"key": secret}) if secret else ""
                alias = dict(slug=row["slug"], expires=row["expires"], days=row["days"],
                             path=f"/v/{row['slug']}{query}" if live and paths["watch"] else None)
        return dict(**paths, alias=alias)
