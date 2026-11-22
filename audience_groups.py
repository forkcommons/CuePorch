"""Viewer-owned community groups and their playable Watch programs."""

import re
import time
import uuid

from flask import abort, render_template, request, url_for

import content_rating
from library_discovery import PAGE_SIZE, entry_payload, ordered

SCHEMA = """
CREATE TABLE IF NOT EXISTS audience_group(
 id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, owner TEXT NOT NULL,
 title TEXT NOT NULL, description TEXT NOT NULL, access TEXT NOT NULL,
 revision TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audience_group_member(
 group_id TEXT NOT NULL, account TEXT NOT NULL, joined REAL NOT NULL,
 PRIMARY KEY(group_id,account));
CREATE INDEX IF NOT EXISTS audience_group_member_account
 ON audience_group_member(account,joined);
CREATE TABLE IF NOT EXISTS audience_group_video(
 group_id TEXT NOT NULL, kind TEXT NOT NULL, slug TEXT NOT NULL,
 added REAL NOT NULL, PRIMARY KEY(group_id,kind,slug));
"""
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")
ACCESS = ("public", "members")


def fields(body):
    slug = body.get("slug")
    if not isinstance(slug, str) or SLUG.fullmatch(slug) is None:
        abort(400, "Use a lowercase slug of 3–64 characters.")
    title = body.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
        abort(400, "Enter a group name of up to 120 characters.")
    description = body.get("description", "")
    if not isinstance(description, str) or len(description) > 1500:
        abort(400, "Use up to 1,500 characters for the description.")
    access = body.get("access")
    if access not in ACCESS:
        abort(400, "Choose public or members access.")
    return slug, title.strip(), description.strip(), access


def get_group(con, slug):
    row = con.execute("SELECT * FROM audience_group WHERE slug=?", (slug,)).fetchone()
    if row is None:
        abort(404)
    return row


def member_of(con, row, account_id):
    if account_id is None:
        return False
    if row["owner"] == account_id:
        return True
    return con.execute("SELECT 1 FROM audience_group_member WHERE group_id=? AND account=?",
                       (row["id"], account_id)).fetchone() is not None


def payload(row):
    return dict(slug=row["slug"], title=row["title"], description=row["description"],
                access=row["access"], revision=row["revision"],
                url=url_for("community_group_page", slug=row["slug"]))


def delete(con, uid):
    con.execute("DELETE FROM audience_group_member WHERE group_id=?", (uid,))
    con.execute("DELETE FROM audience_group_video WHERE group_id=?", (uid,))
    con.execute("DELETE FROM audience_group WHERE id=?", (uid,))


def remove_account(con, uid):
    for row in con.execute("SELECT id FROM audience_group WHERE owner=?", (uid,)).fetchall():
        delete(con, row["id"])
    con.execute("DELETE FROM audience_group_member WHERE account=?", (uid,))


def register(app, tx, viewer, mutation, listing, private, watch_preferences):
    @app.get("/watch/groups")
    def community_groups_page():
        with tx() as con:
            account = viewer(con, required=False)
            aid = account["id"] if account else None
            page = request.args.get("page", 1, type=int)
            page = max(1, min(page, 1_000_000))
            offset = (page - 1) * PAGE_SIZE
            rows = con.execute(
                "SELECT g.* FROM audience_group g "
                "WHERE g.access='public' OR g.owner=? OR EXISTS ("
                "SELECT 1 FROM audience_group_member m "
                "WHERE m.group_id=g.id AND m.account=?) "
                "ORDER BY g.created DESC, g.id LIMIT ? OFFSET ?",
                (aid, aid, PAGE_SIZE + 1, offset),
            ).fetchall()
            return private(render_template(
                "community_groups.html",
                groups=[payload(row) for row in rows[:PAGE_SIZE]],
                signed_in=bool(account),
                page=page,
                has_next=len(rows) > PAGE_SIZE,
            ))

    @app.get("/groups/<slug>")
    def community_group_page(slug):
        with tx() as con:
            account = viewer(con, required=False)
            aid = account["id"] if account else None
            row = get_group(con, slug)
            joined = member_of(con, row, aid)
            if row["access"] == "members" and not joined:
                abort(404)
            owner = row["owner"] == aid
            source = listing(con)
            prefs = watch_preferences(con)
            library = {
                key: entry
                for key, entry in source.items()
                if not content_rating.filtered(prefs, entry)
            }
            selected = con.execute(
                "SELECT kind,slug FROM audience_group_video WHERE group_id=? ORDER BY added,kind,slug",
                (row["id"],),
            ).fetchall()
            selected_keys = [tuple(item) for item in selected]
            videos = [
                entry_payload(library[key])
                for key in selected_keys
                if key in library
            ]
            included = set(selected_keys)
            choices = [entry_payload(entry) for entry in ordered(library.values())
                       if entry["kind"] in ("work", "meeting")
                       and (entry["kind"], entry["slug"]) not in included] if owner else []
            return private(render_template(
                "community_group.html",
                group=payload(row),
                signed_in=bool(account),
                joined=joined,
                owner=owner,
                videos=videos,
                choices=choices,
            ))

    @app.post("/api/viewer/groups")
    def create_group():
        slug, title, description, access = fields(mutation())
        with tx() as con:
            account = viewer(con)
            exists = con.execute("SELECT 1 FROM audience_group WHERE slug=?", (slug,)).fetchone()
            if exists:
                abort(409, "A group already uses this link name.")
            con.execute(
                "INSERT INTO audience_group (id,slug,owner,title,description,access,revision,created) VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, slug, account["id"], title, description, access, uuid.uuid4().hex, time.time()),
            )
            return private(payload(get_group(con, slug)), 201)

    @app.route("/api/viewer/groups/<slug>", methods=("PUT", "DELETE"))
    def modify_group(slug):
        body = mutation()
        with tx() as con:
            account = viewer(con)
            row = get_group(con, slug)
            if account["id"] != row["owner"]:
                abort(404)
            if body.get("revision") != row["revision"]:
                abort(409, "This group changed. Reload it before saving.")
            if request.method == "DELETE":
                delete(con, row["id"])
                return private("", 204)
            newslug, title, description, access = fields(body)
            hit = con.execute(
                "SELECT 1 FROM audience_group WHERE slug=? AND id<>?",
                (newslug, row["id"]),
            ).fetchone()
            if hit:
                abort(409, "A group already uses this link name.")
            con.execute(
                "UPDATE audience_group SET slug=?, title=?, description=?, access=?, revision=? WHERE id=?",
                (newslug, title, description, access, uuid.uuid4().hex, row["id"]),
            )
            return private(payload(get_group(con, newslug)))

    @app.route("/api/viewer/groups/<slug>/membership", methods=("PUT", "DELETE"))
    def modify_membership(slug):
        mutation()
        with tx() as con:
            aid = viewer(con)["id"]
            row = get_group(con, slug)
            joined = member_of(con, row, aid)
            if row["access"] == "members" and not joined:
                abort(404)
            if request.method == "DELETE":
                if row["owner"] == aid:
                    abort(409, "The group owner cannot leave their group.")
                con.execute(
                    "DELETE FROM audience_group_member"
                    " WHERE group_id=? AND account=?",
                    (row["id"], aid),
                )
            elif row["owner"] != aid:
                con.execute(
                    "INSERT OR IGNORE INTO audience_group_member"
                    "(group_id,account,joined) VALUES(?,?,?)",
                    (row["id"], aid, time.time()),
                )
        return private("", 204)

    @app.route("/api/viewer/groups/<slug>/videos/<any(work,meeting):kind>/<video>", methods=("PUT", "DELETE"))
    def viewer_group_video(slug, kind, video):
        mutation()
        with tx() as con:
            account = viewer(con)
            group = get_group(con, slug)
            if group["owner"] != account["id"]:
                abort(404)
            if request.method == "DELETE":
                con.execute("DELETE FROM audience_group_video WHERE group_id=? AND kind=? AND slug=?",
                            (group["id"], kind, video))
                return private("", 204)
            if (kind, video) not in listing(con, (kind, video)):
                abort(404)
            con.execute("INSERT OR IGNORE INTO audience_group_video VALUES(?,?,?,?)",
                        (group["id"], kind, video, time.time()))
            return private("", 204)
