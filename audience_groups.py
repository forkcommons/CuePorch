"""Viewer-owned community groups and their playable Watch programs."""

import json
import re
import time
import uuid

from flask import abort, render_template, request, url_for

from browser_account_security import email_key
import content_rating
import creator_blocks
import group_albums
from library_discovery import PAGE_SIZE, entry_payload, ordered
import mail_relay

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
CREATE TABLE IF NOT EXISTS audience_group_follow(
 account TEXT NOT NULL, group_id TEXT NOT NULL,
 PRIMARY KEY(account,group_id));
CREATE INDEX IF NOT EXISTS audience_group_follow_group ON audience_group_follow(group_id);
CREATE TABLE IF NOT EXISTS audience_group_video(
 group_id TEXT NOT NULL, kind TEXT NOT NULL, slug TEXT NOT NULL,
 added REAL NOT NULL, PRIMARY KEY(group_id,kind,slug));
CREATE TABLE IF NOT EXISTS audience_group_invite(
 group_id TEXT NOT NULL, address TEXT NOT NULL, expires REAL NOT NULL,
 PRIMARY KEY(group_id,address));
CREATE TABLE IF NOT EXISTS audience_group_moderator(
 group_id TEXT NOT NULL, account TEXT NOT NULL,
 PRIMARY KEY(group_id,account));
CREATE TABLE IF NOT EXISTS audience_group_submission_policy(
 group_id TEXT PRIMARY KEY, allow_members INTEGER NOT NULL,
 require_review INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audience_group_submission(
 group_id TEXT NOT NULL, kind TEXT NOT NULL, slug TEXT NOT NULL,
 account TEXT NOT NULL, added REAL NOT NULL,
 PRIMARY KEY(group_id,kind,slug));
CREATE INDEX IF NOT EXISTS audience_group_submission_account
 ON audience_group_submission(account);
""" + group_albums.SCHEMA
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]")
ACCESS = ("public", "members")
INVITE_SECONDS = 7 * 86400


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


def can_moderate(con, row, account_id):
    if account_id is None:
        return False
    if row["owner"] == account_id:
        return True
    return con.execute("""SELECT 1 FROM audience_group_moderator g
        JOIN audience_group_member m ON m.group_id=g.group_id AND m.account=g.account
        WHERE g.group_id=? AND g.account=?""",
        (row["id"], account_id)).fetchone() is not None


def submission_settings(con, group_id):
    row = con.execute(
        "SELECT allow_members,require_review"
        " FROM audience_group_submission_policy WHERE group_id=?",
        (group_id,)).fetchone()
    if row is None:
        return {"allow_members": True, "require_review": False}
    return {"allow_members": bool(row["allow_members"]),
            "require_review": bool(row["require_review"])}


def followed_program(con, aid):
    rows = con.execute("""SELECT g.id,g.slug,g.title,v.kind,v.slug,v.added
        FROM audience_group_follow f JOIN audience_group g ON g.id=f.group_id
        LEFT JOIN audience_group_video v ON v.group_id=g.id
        WHERE f.account=? AND (g.access='public' OR g.owner=? OR EXISTS (
            SELECT 1 FROM audience_group_member m WHERE m.group_id=g.id AND m.account=?
        ))
        ORDER BY g.title COLLATE NOCASE,g.slug
    """, (aid, aid, aid)).fetchall()
    groups = {}
    added = {}
    for gid, group_slug, title, kind, video_slug, timestamp in rows:
        groups[gid] = dict(slug=group_slug, title=title)
        if kind is not None:
            key = (kind, video_slug)
            added[key] = max(added.get(key, 0), timestamp)
    return list(groups.values()), added


def invitation_for(con, group, account):
    if account is None:
        return False
    return con.execute("""SELECT 1 FROM audience_group_invite i
        JOIN account_verified_email e ON e.scope='viewer' AND e.address=i.address AND e.account=?
        WHERE i.group_id=? AND i.address=? AND i.expires>?""",
        (account["id"], group["id"], account["email"], time.time()),
    ).fetchone() is not None


def delete(con, uid):
    con.execute("DELETE FROM audience_group_member WHERE group_id=?", (uid,))
    con.execute("DELETE FROM audience_group_moderator WHERE group_id=?", (uid,))
    con.execute("DELETE FROM audience_group_video WHERE group_id=?", (uid,))
    con.execute("DELETE FROM audience_group_invite WHERE group_id=?", (uid,))
    con.execute("DELETE FROM audience_group_submission_policy WHERE group_id=?", (uid,))
    con.execute("DELETE FROM audience_group_submission WHERE group_id=?", (uid,))
    con.execute("DELETE FROM audience_group_album WHERE group_id=?", (uid,))
    con.execute("DELETE FROM audience_group_follow WHERE group_id=?", (uid,))
    con.execute("DELETE FROM audience_group WHERE id=?", (uid,))


def remove_account(con, uid):
    con.execute("DELETE FROM audience_group_invite WHERE address=(SELECT email FROM audience_account WHERE id=?)",
                (uid,))
    for row in con.execute("SELECT id FROM audience_group WHERE owner=?", (uid,)).fetchall():
        delete(con, row["id"])
    con.execute("DELETE FROM audience_group_member WHERE account=?", (uid,))
    con.execute("DELETE FROM audience_group_moderator WHERE account=?", (uid,))
    con.execute("DELETE FROM audience_group_submission WHERE account=?", (uid,))
    con.execute("DELETE FROM audience_group_follow WHERE account=?", (uid,))


def register(app, tx, viewer, mutation, listing, private, watch_preferences, public_origin):
    invite_mail = mail_relay.configured(public_origin)

    @app.route("/api/viewer/feed/group", methods=("GET", "PUT", "DELETE"))
    def viewer_feed_group():
        data = mutation() if request.method in ("PUT", "DELETE") else request.args
        name = data.get("name")
        if not isinstance(name, str) or SLUG.fullmatch(name) is None:
            abort(400)
        with tx() as con:
            aid = viewer(con)["id"]
            row = get_group(con, name)
            if row["access"] != "public" and not member_of(con, row, aid):
                abort(404)
            key = (aid, row["id"])
            if request.method == "PUT":
                con.execute("INSERT OR IGNORE INTO audience_group_follow VALUES(?,?)", key)
                following = True
            elif request.method == "DELETE":
                con.execute("DELETE FROM audience_group_follow WHERE account=? AND group_id=?", key)
                following = False
            else:
                following = con.execute("SELECT 1 FROM audience_group_follow WHERE account=? AND group_id=?",
                                        key).fetchone() is not None
            return private(dict(following=following))

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
                if not invitation_for(con, row, account):
                    abort(404)
                return private(render_template("community_group_invited.html", group=payload(row)))
            owner = row["owner"] == aid
            moderator = can_moderate(con, row, aid)
            settings = submission_settings(con, row["id"])
            contributes = moderator or (joined and settings["allow_members"])
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
            pending_rows = con.execute("""SELECT kind,slug,account FROM audience_group_submission
                WHERE group_id=? AND (? OR account=?) ORDER BY added,kind,slug""",
                (row["id"], moderator, aid)).fetchall() if joined else []
            blocked_choices = creator_blocks.group_blocked_works(con, row["owner"], aid) if contributes else set()
            pending = []
            pending_keys = set()
            for item in pending_rows:
                key = (item["kind"], item["slug"])
                entry = library.get(key)
                pending.append(dict(
                    kind=key[0], slug=key[1], title=entry["title"] if entry else "Video no longer available",
                    can_approve=bool(entry and entry.get("allow_viewer_collections", True)
                                     and (key[0] != "work" or key[1] not in blocked_choices)),
                ))
                pending_keys.add(key)
            choices = [entry_payload(entry) for entry in ordered(library.values())
                       if entry["kind"] in ("work", "meeting")
                       and (entry["kind"], entry["slug"]) not in included
                       and (entry["kind"], entry["slug"]) not in pending_keys
                       and (entry["kind"] != "work" or entry["slug"] not in blocked_choices)
                       and entry.get("allow_viewer_collections", True)] if contributes else []
            manage_members = con.execute("""SELECT a.id, a.name,
                EXISTS (SELECT 1 FROM audience_group_moderator g
                    WHERE g.group_id=m.group_id AND g.account=m.account) AS is_moderator
                FROM audience_group_member m
                JOIN audience_account a ON a.id=m.account WHERE m.group_id=?
                ORDER BY a.name COLLATE NOCASE, a.id""", (row["id"],)).fetchall() if owner else []
            group_invites = [item["address"] for item in con.execute("""SELECT address FROM audience_group_invite
                WHERE group_id=? AND expires>? ORDER BY address""", (row["id"], time.time()))] if owner else []
            return private(render_template(
                "community_group.html",
                group=payload(row),
                signed_in=bool(account),
                joined=joined,
                owner=owner,
                moderator=moderator,
                contributes=contributes,
                pending=pending,
                **settings,
                videos=videos,
                albums=group_albums.rendered(con, row["id"], videos),
                choices=choices,
                manage_members=manage_members,
                group_invites=group_invites,
                invite_mail_available=bool(invite_mail),
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
            explicit = {}
            for key in ("allow_members", "require_review"):
                if key in body:
                    if not isinstance(body[key], bool):
                        abort(400, "Submission settings must be true or false.")
                    explicit[key] = body[key]
            before = submission_settings(con, row["id"])
            after = before | explicit
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
            if explicit:
                con.execute(
                    "INSERT INTO audience_group_submission_policy"
                    "(group_id,allow_members,require_review) VALUES(?,?,?)"
                    " ON CONFLICT(group_id) DO UPDATE SET"
                    " allow_members=excluded.allow_members,"
                    " require_review=excluded.require_review",
                    (row["id"], after["allow_members"], after["require_review"]),
                )
            if (after["allow_members"] and not after["require_review"]
                    and (before["require_review"] or not before["allow_members"])):
                for kind, slug, account, added in con.execute(
                    "SELECT kind,slug,account,added FROM audience_group_submission WHERE group_id=?",
                    (row["id"],),
                ).fetchall():
                    entry = listing(con, (kind, slug)).get((kind, slug))
                    if (entry and entry.get("allow_viewer_collections", True) and member_of(con, row, account)
                            and not creator_blocks.video_blocked_for_group(con, row["owner"], account, kind, slug)):
                        con.execute("INSERT OR IGNORE INTO audience_group_video VALUES(?,?,?,?)",
                                    (row["id"], kind, slug, added))
                con.execute("DELETE FROM audience_group_submission WHERE group_id=?", (row["id"],))
            return private(payload(get_group(con, newslug)))

    @app.route("/api/viewer/groups/<slug>/membership", methods=("PUT", "DELETE"))
    def modify_membership(slug):
        mutation()
        with tx() as con:
            account = viewer(con)
            aid = account["id"]
            row = get_group(con, slug)
            joined = member_of(con, row, aid)
            invited = request.method == "PUT" and invitation_for(con, row, account)
            if row["access"] == "members" and not joined and not invited:
                abort(404)
            if request.method == "DELETE":
                if row["owner"] == aid:
                    abort(409, "The group owner cannot leave their group.")
                con.execute(
                    "DELETE FROM audience_group_member"
                    " WHERE group_id=? AND account=?",
                    (row["id"], aid),
                )
                con.execute("DELETE FROM audience_group_moderator WHERE group_id=? AND account=?",
                            (row["id"], aid))
                con.execute("DELETE FROM audience_group_submission WHERE group_id=? AND account=?",
                            (row["id"], aid))
            elif row["owner"] != aid:
                con.execute(
                    "INSERT OR IGNORE INTO audience_group_member"
                    "(group_id,account,joined) VALUES(?,?,?)",
                    (row["id"], aid, time.time()),
                )
                if invited:
                    con.execute("DELETE FROM audience_group_invite WHERE group_id=? AND address=?",
                                (row["id"], account["email"]))
        return private("", 204)

    @app.route("/api/viewer/groups/<slug>/invite", methods=("POST", "DELETE"))
    def viewer_group_invite(slug):
        body = mutation()
        address = email_key(body.get("email"))
        send_email = body.get("send_email", False)
        if type(send_email) is not bool:
            abort(400, "Choose whether to email this invitation.")
        with tx() as con:
            row = get_group(con, slug)
            if row["owner"] != viewer(con)["id"]:
                abort(404)
            if body.get("revision") != row["revision"]:
                abort(409, "This group changed. Reload it before saving.")
            if request.method == "DELETE":
                con.execute("DELETE FROM audience_group_invite WHERE group_id=? AND address=?",
                            (row["id"], address))
            else:
                if send_email and not invite_mail:
                    abort(409, "Invitation email needs a configured relay and public HTTPS address.")
                now = time.time()
                con.execute("""INSERT INTO audience_group_invite(group_id,address,expires) VALUES(?,?,?)
                    ON CONFLICT(group_id,address) DO UPDATE SET expires=excluded.expires""",
                    (row["id"], address, now + INVITE_SECONDS))
                con.execute("DELETE FROM audience_group_invite WHERE group_id=? AND expires<=?",
                            (row["id"], now))
            con.execute("UPDATE audience_group SET revision=? WHERE id=?", (uuid.uuid4().hex, row["id"]))
        if request.method == "DELETE":
            return private("", 204)
        path = url_for("community_group_page", slug=slug)
        delivery = None
        if send_email:
            accepted = mail_relay.deliver(invite_mail, address, "CuePorch group invitation",
                f"You were invited to {row['title']}. Sign in to CuePorch and verify your viewer email address, "
                f"then open {public_origin}{path} to accept the invitation.")
            delivery = "accepted" if accepted else "failed"
        return private(dict(url=path, delivery=delivery))

    @app.post("/api/viewer/groups/<slug>/ownership")
    def transfer_ownership(slug):
        body = mutation()
        with tx() as con:
            aid = viewer(con)["id"]
            row = get_group(con, slug)
            if row["owner"] != aid:
                abort(404)
            if body.get("revision") != row["revision"]:
                abort(409, "This group changed. Reload it before saving.")
            new_owner = body.get("member")
            if not isinstance(new_owner, str) or not con.execute("""SELECT 1 FROM audience_group_member m
                    JOIN audience_account a ON a.id=m.account WHERE m.group_id=? AND m.account=?""",
                    (row["id"], new_owner)).fetchone():
                abort(422, "Choose a current group member.")
            con.execute("INSERT OR IGNORE INTO audience_group_member(group_id,account,joined) VALUES(?,?,?)",
                        (row["id"], aid, time.time()))
            con.execute("DELETE FROM audience_group_member WHERE group_id=? AND account=?", (row["id"], new_owner))
            con.execute("DELETE FROM audience_group_moderator WHERE group_id=? AND account IN (?,?)",
                        (row["id"], aid, new_owner))
            con.execute("UPDATE audience_group SET owner=?,revision=? WHERE id=?",
                        (new_owner, uuid.uuid4().hex, row["id"]))
            creator_blocks.remove_group_entries_for_account(con, row["id"], new_owner)
            return private(payload(get_group(con, slug)))

    @app.delete("/api/viewer/groups/<slug>/members/<member>")
    def remove_member(slug, member):
        body = mutation()
        with tx() as con:
            row = get_group(con, slug)
            if row["owner"] != viewer(con)["id"]:
                abort(404)
            if body.get("revision") != row["revision"]:
                abort(409, "This group changed. Reload it before saving.")
            result = con.execute("DELETE FROM audience_group_member WHERE group_id=? AND account=?",
                                 (row["id"], member))
            if result.rowcount:
                con.execute("DELETE FROM audience_group_moderator WHERE group_id=? AND account=?",
                            (row["id"], member))
                con.execute("DELETE FROM audience_group_submission WHERE group_id=? AND account=?",
                            (row["id"], member))
                con.execute("UPDATE audience_group SET revision=? WHERE id=?", (uuid.uuid4().hex, row["id"]))
        return private("", 204)

    @app.put("/api/viewer/groups/<slug>/moderators/<member>")
    def modify_moderator(slug, member):
        body = mutation()
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            abort(400, "Choose whether to grant moderator access.")
        with tx() as con:
            row = get_group(con, slug)
            if row["owner"] != viewer(con)["id"]:
                abort(404)
            if body.get("revision") != row["revision"]:
                abort(409, "This group changed. Reload it before saving.")
            if not con.execute("SELECT 1 FROM audience_group_member WHERE group_id=? AND account=?",
                               (row["id"], member)).fetchone():
                abort(422, "Choose a current group member.")
            if enabled:
                con.execute("INSERT OR IGNORE INTO audience_group_moderator(group_id,account) VALUES(?,?)",
                            (row["id"], member))
            else:
                con.execute("DELETE FROM audience_group_moderator WHERE group_id=? AND account=?",
                            (row["id"], member))
            con.execute("UPDATE audience_group SET revision=? WHERE id=?",
                        (uuid.uuid4().hex, row["id"]))
        return private("", 204)

    @app.route("/api/viewer/groups/<slug>/videos/<any(work,meeting):kind>/<video>", methods=("PUT", "DELETE"))
    def viewer_group_video(slug, kind, video):
        mutation()
        with tx() as con:
            account = viewer(con)
            group = get_group(con, slug)
            aid = account["id"]
            moderator = can_moderate(con, group, aid)
            if not (moderator or member_of(con, group, aid)):
                abort(404)
            key = (group["id"], kind, video)
            if request.method == "DELETE":
                if moderator:
                    con.execute("DELETE FROM audience_group_video WHERE group_id=? AND kind=? AND slug=?", key)
                    con.execute("DELETE FROM audience_group_submission WHERE group_id=? AND kind=? AND slug=?", key)
                elif not con.execute("""DELETE FROM audience_group_submission
                        WHERE group_id=? AND kind=? AND slug=? AND account=?""", (*key, aid)).rowcount:
                    abort(404)
                return private("", 204)
            settings = submission_settings(con, group["id"])
            if not moderator and not settings["allow_members"]:
                abort(403, "Member submissions are disabled.")
            entry = listing(con, (kind, video)).get((kind, video))
            if entry is None:
                abort(404)
            if not entry.get("allow_viewer_collections", True):
                abort(403)
            if creator_blocks.video_blocked_for_group(con, group["owner"], aid, kind, video):
                abort(403, "You cannot add this video to the group.")
            if not moderator and settings["require_review"]:
                published = con.execute(
                    "SELECT 1 FROM audience_group_video WHERE group_id=? AND kind=? AND slug=?", key,
                ).fetchone()
                if published is not None:
                    return private("", 204)
                con.execute("INSERT OR IGNORE INTO audience_group_submission VALUES(?,?,?,?,?)",
                            (*key, aid, time.time()))
                return private("", 202)
            con.execute("INSERT OR IGNORE INTO audience_group_video VALUES(?,?,?,?)", (*key, time.time()))
            con.execute("DELETE FROM audience_group_submission WHERE group_id=? AND kind=? AND slug=?", key)
            return private("", 204)

    @app.post("/api/viewer/groups/<slug>/submissions/<any(work,meeting):kind>/<video>")
    def viewer_group_submission_decision(slug, kind, video):
        body = mutation()
        approve = body.get("approve")
        if not isinstance(approve, bool):
            abort(400, "Choose whether to approve the submission.")
        with tx() as con:
            aid = viewer(con)["id"]
            group = get_group(con, slug)
            if not can_moderate(con, group, aid):
                abort(404)
            key = (group["id"], kind, video)
            row = con.execute("""SELECT account FROM audience_group_submission
                WHERE group_id=? AND kind=? AND slug=?""", key).fetchone()
            if row is None:
                abort(404)
            if approve:
                entry = listing(con, (kind, video)).get((kind, video))
                if (not member_of(con, group, row["account"]) or entry is None
                        or not entry.get("allow_viewer_collections", True)
                        or creator_blocks.video_blocked_for_group(con, group["owner"], row["account"], kind, video)
                        or creator_blocks.video_blocked(con, aid, kind, video)):
                    abort(409, "This video or its contributor is no longer eligible.")
                con.execute("INSERT OR IGNORE INTO audience_group_video VALUES(?,?,?,?)", (*key, time.time()))
            con.execute("DELETE FROM audience_group_submission WHERE group_id=? AND kind=? AND slug=?", key)
        return private("", 204)

    @app.post("/api/viewer/groups/<slug>/albums")
    @app.route("/api/viewer/groups/<slug>/albums/<album>", methods=("PUT", "DELETE"))
    def viewer_group_album(slug, album=None):
        body = mutation()
        with tx() as con:
            group = get_group(con, slug)
            if not can_moderate(con, group, viewer(con)["id"]):
                abort(404)
            if album is not None:
                row = con.execute("SELECT revision FROM audience_group_album WHERE group_id=? AND id=?",
                                  (group["id"], album)).fetchone()
                if row is None:
                    abort(404)
                if body.get("revision") != row["revision"]:
                    abort(409, "This album changed. Reload it before saving.")
                if request.method == "DELETE":
                    con.execute("DELETE FROM audience_group_album WHERE group_id=? AND id=?", (group["id"], album))
                    return private("", 204)
            public = listing(con)
            allowed = {tuple(key) for key in con.execute(
                "SELECT kind,slug FROM audience_group_video WHERE group_id=?", (group["id"],),
            ) if tuple(key) in public}
            title, sort, items = group_albums.parse(body, allowed)
            revision = uuid.uuid4().hex
            if album is None:
                album = uuid.uuid4().hex
                con.execute("INSERT INTO audience_group_album VALUES(?,?,?,?,?,?,?)",
                            (album, group["id"], title, sort, json.dumps(items), revision, time.time()))
                return private(dict(id=album, revision=revision), 201)
            con.execute("""UPDATE audience_group_album
                SET title=?, sort=?, items=?, revision=? WHERE group_id=? AND id=?""",
                (title, sort, json.dumps(items), revision, group["id"], album))
            return private(dict(id=album, revision=revision))
