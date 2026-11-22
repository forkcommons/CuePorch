"""Public creator profiles scoped to the creator's current public Library videos."""

import json
import re
import time
import uuid

from flask import abort, make_response, render_template, request, url_for

import content_rating
import library_registration
import library_showcases
from browser_account_security import private, same_origin, secure_site
from library_interactivity import text_field, web_link
from library_trash import hidden


SCHEMA = """
CREATE TABLE IF NOT EXISTS creator_profile(
 member TEXT PRIMARY KEY, handle TEXT NOT NULL UNIQUE, name TEXT NOT NULL, bio TEXT NOT NULL,
 links TEXT NOT NULL, visible INTEGER NOT NULL CHECK(visible IN (0,1)), cover TEXT,
 featured TEXT NOT NULL, revision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS creator_video_appearance(
 work TEXT NOT NULL, member TEXT NOT NULL, PRIMARY KEY(work,member));
CREATE INDEX IF NOT EXISTS creator_video_appearance_member ON creator_video_appearance(member);
CREATE TABLE IF NOT EXISTS audience_creator_follow(
 account TEXT NOT NULL, member TEXT NOT NULL,
 PRIMARY KEY(account,member));
CREATE INDEX IF NOT EXISTS creator_followers ON audience_creator_follow(member);
CREATE TABLE IF NOT EXISTS audience_creator_follow_activity(
 account TEXT NOT NULL, member TEXT NOT NULL,
 videos INTEGER NOT NULL CHECK(videos IN (0,1)),
 appearances INTEGER NOT NULL CHECK(appearances IN (0,1)),
 PRIMARY KEY(account,member));
CREATE TABLE IF NOT EXISTS creator_shared_likes(
 member TEXT PRIMARY KEY, account TEXT UNIQUE NOT NULL, since REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audience_creator_follow_likes(
 account TEXT NOT NULL, member TEXT NOT NULL, PRIMARY KEY(account,member));
CREATE TABLE IF NOT EXISTS audience_category_follow(
 account TEXT NOT NULL, key TEXT NOT NULL, label TEXT NOT NULL,
 PRIMARY KEY(account,key));
"""
HANDLE = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])")
VISIBLE = "m.removed IS NULL AND m.role IN ('owner','admin','contributor','contributor_plus') AND p.visible=1"


def for_work(con, uid):
    return con.execute(f"""
        SELECT p.handle,p.name FROM creator_profile p JOIN team_member m ON m.id=p.member
        JOIN team_owned_work o ON o.member=p.member WHERE o.work=? AND {VISIBLE}
    """, (uid,)).fetchone()


def follower_count(con, uid):
    return con.execute("SELECT count(*) FROM audience_creator_follow WHERE member=?", (uid,)).fetchone()[0]


def register_audience(app, tx, viewer, mutation, listing, entry_payload, ordered, page_size, account_preferences,
                      followed_program, current_team):
    @app.route("/api/viewer/like-sharing", methods=("GET", "PUT"))
    def viewer_like_sharing():
        body = mutation() if request.method == "PUT" else None
        if body is not None and not isinstance(body.get("enabled"), bool):
            abort(400, "Choose whether to share new likes.")
        with tx() as con:
            account = viewer(con)["id"]
            team = current_team(con)
            candidate = con.execute(f"""SELECT p.member,p.handle,p.name FROM creator_profile p
                JOIN team_member m ON m.id=p.member WHERE p.member=? AND {VISIBLE}""",
                (team["id"],)).fetchone() if team else None
            if body is not None:
                if body["enabled"]:
                    if candidate is None or candidate["handle"] != body.get("handle"):
                        abort(409, "Sign in to the creator profile you want to use for sharing.")
                    links = con.execute("""SELECT member,account FROM creator_shared_likes
                        WHERE member=? OR account=?""", (candidate["member"], account)).fetchall()
                    if any(tuple(link) != (candidate["member"], account) for link in links):
                        abort(409, "An account is already linked for sharing likes.")
                    con.execute("INSERT OR IGNORE INTO creator_shared_likes VALUES(?,?,?)",
                                (candidate["member"], account, time.time()))
                else:
                    con.execute("DELETE FROM creator_shared_likes WHERE account=?", (account,))
            linked = con.execute("""SELECT p.handle,p.name FROM creator_shared_likes s
                JOIN creator_profile p ON p.member=s.member WHERE s.account=?""", (account,)).fetchone()
            return private(dict(
                enabled=linked is not None,
                viewer=dict(linked) if linked else None,
                candidate=dict(handle=candidate["handle"], name=candidate["name"]) if candidate else None,
            ))

    @app.route("/api/viewer/follow/<handle>", methods=("GET", "PUT", "DELETE"))
    def viewer_follow_creator(handle):
        if not HANDLE.fullmatch(handle):
            abort(404)
        if request.method in ("PUT", "DELETE"):
            mutation()
        with tx() as con:
            account = viewer(con)["id"]
            profile = con.execute(f"""SELECT p.member,({VISIBLE}) AS visible FROM creator_profile p
                JOIN team_member m ON m.id=p.member WHERE p.handle=?""", (handle,)).fetchone()
            if request.method == "DELETE":
                if profile:
                    con.execute("DELETE FROM audience_creator_follow_activity WHERE account=? AND member=?",
                                (account, profile["member"]))
                    con.execute("DELETE FROM audience_creator_follow_likes WHERE account=? AND member=?",
                                (account, profile["member"]))
                    con.execute("DELETE FROM audience_creator_follow WHERE account=? AND member=?",
                                (account, profile["member"]))
                return private(dict(following=False,
                                    followers=follower_count(con, profile["member"]) if profile and profile["visible"] else None))
            if not profile or not profile["visible"]:
                abort(404)
            member = profile["member"]
            if request.method == "PUT":
                con.execute("INSERT OR IGNORE INTO audience_creator_follow VALUES(?,?)", (account, member))
            following = con.execute("SELECT 1 FROM audience_creator_follow WHERE account=? AND member=?",
                                    (account, member)).fetchone() is not None
            return private(dict(following=following, followers=follower_count(con, member)))

    @app.put("/api/viewer/feed/creator/<handle>")
    def viewer_feed_creator(handle):
        if not HANDLE.fullmatch(handle):
            abort(404)
        body = mutation()
        videos, appearances = body.get("videos"), body.get("appearances")
        if (not isinstance(videos, bool) or not isinstance(appearances, bool)
                or ("likes" in body and not isinstance(body["likes"], bool))):
            abort(400, "Choose the creator activity to follow.")
        with tx() as con:
            account = viewer(con)["id"]
            profile = con.execute(f"""SELECT p.member FROM creator_profile p
                JOIN team_member m ON m.id=p.member WHERE p.handle=? AND {VISIBLE}""", (handle,)).fetchone()
            if profile is None:
                abort(404)
            key = (account, profile["member"])
            if not con.execute("SELECT 1 FROM audience_creator_follow WHERE account=? AND member=?", key).fetchone():
                abort(409, "Follow this creator before changing their Feed activity.")
            previous_likes = con.execute("SELECT 1 FROM audience_creator_follow_likes WHERE account=? AND member=?",
                                         key).fetchone() is not None
            likes = body.get("likes", previous_likes)
            following = videos or appearances or likes
            if following:
                con.execute("""INSERT INTO audience_creator_follow_activity VALUES(?,?,?,?)
                    ON CONFLICT(account,member) DO UPDATE
                    SET videos=excluded.videos,appearances=excluded.appearances""", (*key, videos, appearances))
            else:
                con.execute("DELETE FROM audience_creator_follow_activity WHERE account=? AND member=?", key)
                con.execute("DELETE FROM audience_creator_follow WHERE account=? AND member=?", key)
            if likes:
                con.execute("INSERT OR IGNORE INTO audience_creator_follow_likes VALUES(?,?)", key)
            else:
                con.execute("DELETE FROM audience_creator_follow_likes WHERE account=? AND member=?", key)
        return private(dict(following=following, videos=videos, appearances=appearances, likes=likes))

    @app.route("/api/viewer/feed/category", methods=("GET", "PUT", "DELETE"))
    def viewer_feed_category():
        if request.method in ("PUT", "DELETE"):
            data = mutation()
        else:
            data = None
        if request.method == "GET":
            name = request.args.get("name", "")
        else:
            name = (data or {}).get("name", "")
        if not isinstance(name, str):
            abort(400)
        name = name.strip()
        if not (1 <= len(name) <= 120):
            abort(400)
        key = name.casefold()
        with tx() as con:
            account = viewer(con)["id"]
            if request.method == "GET":
                following = con.execute(
                    "SELECT 1 FROM audience_category_follow WHERE account=? AND key=?",
                    (account, key)
                ).fetchone() is not None
                return private(dict(following=following))
            if request.method == "DELETE":
                con.execute(
                    "DELETE FROM audience_category_follow WHERE account=? AND key=?",
                    (account, key)
                )
                return private(dict(following=False))
            preference = account_preferences(con, account)
            canonical_label = None
            for (kind, _), entry in listing(con).items():
                if kind != "work":
                    continue
                if content_rating.filtered(preference, entry):
                    continue
                for label in entry["categories"]:
                    if label.casefold() == key:
                        canonical_label = label
                        break
                if canonical_label is not None:
                    break
            if canonical_label is None:
                abort(404)
            con.execute(
                "INSERT OR IGNORE INTO audience_category_follow(account, key, label) VALUES(?,?,?)",
                (account, key, canonical_label)
            )
            return private(dict(following=True))

    @app.route("/api/viewer/feed/channel", methods=("GET", "PUT", "DELETE"))
    def viewer_feed_channel():
        data = mutation() if request.method in ("PUT", "DELETE") else None
        if request.method == "GET":
            name = request.args.get("name", "")
        else:
            name = (data or {}).get("name", "")
        if not isinstance(name, str) or library_showcases.SLUG.fullmatch(name) is None:
            abort(400)
        with tx() as con:
            account = viewer(con)["id"]
            row = con.execute(
                "SELECT * FROM video_showcase WHERE slug=?", (name,)
            ).fetchone()
            if row is None or not library_showcases.channel_visible(
                library_showcases.state(row)
            ):
                abort(404)
            channel = row["id"]
            if request.method == "PUT":
                con.execute(
                    "INSERT OR IGNORE INTO audience_channel_follow(account, channel)"
                    " VALUES (?, ?)",
                    (account, channel),
                )
                following = True
            elif request.method == "DELETE":
                con.execute(
                    "DELETE FROM audience_channel_follow WHERE account=? AND channel=?",
                    (account, channel),
                )
                following = False
            else:
                following = con.execute(
                    "SELECT 1 FROM audience_channel_follow WHERE account=? AND channel=?",
                    (account, channel),
                ).fetchone() is not None
            return private(dict(following=following))

    @app.get("/watch/feed")
    def viewer_feed():
        page = max(1, request.args.get("page", 1, type=int))
        category_follows = []
        creator_follows = []
        channel_follows = []
        group_follows = []
        rows = []
        with tx() as con:
            account = viewer(con, required=False)
            if account:
                owned = {row["work"]: row for row in con.execute(f"""
                    SELECT o.work,p.handle,p.name,t.published_at
                    FROM audience_creator_follow f JOIN creator_profile p ON p.member=f.member
                    JOIN team_member m ON m.id=p.member JOIN team_owned_work o ON o.member=p.member
                    LEFT JOIN audience_creator_follow_activity a ON a.account=f.account AND a.member=f.member
                    LEFT JOIN library_publication_time t ON t.work=o.work
                    WHERE f.account=? AND {VISIBLE} AND COALESCE(a.videos,1)=1
                """, (account["id"],))}
                category_rows = con.execute("SELECT key,label FROM audience_category_follow WHERE account=? ORDER BY key", (account["id"],)).fetchall()
                category_keys = {row["key"] for row in category_rows}
                category_follows = category_rows
                creator_follows = con.execute(f"""
                    SELECT p.handle,p.name,COALESCE(a.videos,1) AS videos,
                           COALESCE(a.appearances,0) AS appearances, fl.member IS NOT NULL AS likes
                    FROM audience_creator_follow f
                    JOIN creator_profile p ON p.member=f.member
                    JOIN team_member m ON m.id=p.member
                    LEFT JOIN audience_creator_follow_activity a ON a.account=f.account AND a.member=f.member
                    LEFT JOIN audience_creator_follow_likes fl ON fl.account=f.account AND fl.member=f.member
                    WHERE f.account=? AND {VISIBLE}
                    ORDER BY p.name COLLATE NOCASE,p.handle
                """, (account["id"],)).fetchall()
                appearance_works = {row["work"] for row in con.execute(f"""
                    SELECT v.work FROM creator_video_appearance v
                    JOIN audience_creator_follow f ON f.member=v.member
                    JOIN creator_profile p ON p.member=v.member
                    JOIN team_member m ON m.id=p.member
                    JOIN audience_creator_follow_activity a ON a.account=f.account AND a.member=f.member
                    WHERE f.account=? AND a.appearances=1 AND {VISIBLE}
                """, (account["id"],))}
                liked_works = {(row["kind"], row["slug"]): row["ts"] for row in con.execute(f"""
                    SELECT l.kind,l.slug,MAX(l.liked) AS ts
                    FROM audience_creator_follow_likes fl
                    JOIN creator_shared_likes s ON s.member=fl.member
                    JOIN audience_like l ON l.account=s.account
                    JOIN creator_profile p ON p.member=fl.member
                    JOIN team_member m ON m.id=p.member
                    WHERE fl.account=? AND l.liked>=s.since AND {VISIBLE}
                    GROUP BY l.kind,l.slug
                """, (account["id"],))}
                channel_works = set()
                for row in con.execute("SELECT s.* FROM audience_channel_follow f JOIN video_showcase s ON s.id=f.channel WHERE f.account=?", (account["id"],)):
                    values = library_showcases.state(row)
                    if not library_showcases.channel_visible(values):
                        continue
                    channel_follows.append(dict(slug=row["slug"], title=values["title"]))
                    form = library_registration.active(con, "showcase", row["id"])
                    if not form or form["timing"] != "before":
                        channel_works.update(values["videos"])
                channel_follows.sort(key=lambda item: (item["title"].casefold(), item["slug"]))
                group_follows, group_works = followed_program(con, account["id"])
                if owned or category_keys or channel_works or group_works or appearance_works or liked_works:
                    preference = account_preferences(con, account["id"])
                    entries = ordered(
                        entry for (kind, slug), entry in listing(con).items()
                        if ((kind, slug) in group_works or (kind, slug) in liked_works or (
                            kind == "work" and (slug in owned or slug in channel_works or slug in appearance_works
                            or any(name.casefold() in category_keys for name in entry["categories"]))
                        ))
                        and not content_rating.filtered(preference, entry)
                    )
                    if category_keys or channel_works or group_works or appearance_works or liked_works:
                        publication_times = {row["work"]: row["published_at"] for row in con.execute("SELECT work,published_at FROM library_publication_time").fetchall()}
                        entries.sort(key=lambda entry: max(
                            publication_times.get(entry["slug"]) or 0,
                            group_works.get((entry["kind"], entry["slug"]), 0),
                            liked_works.get((entry["kind"], entry["slug"]), 0),
                        ), reverse=True)
                    else:
                        entries.sort(key=lambda entry: owned[entry["slug"]]["published_at"] or 0, reverse=True)
                    rows = [dict(entry_payload(entry), creator=owned.get(entry["slug"])) for entry in entries]
        start = (page - 1) * page_size
        return private(render_template("viewer_feed.html", rows=rows[start:start + page_size],
                                       signed_in=bool(account), page=page, next_page=start + page_size < len(rows),
                                       category_follows=category_follows, creator_follows=creator_follows,
                                       channel_follows=channel_follows, group_follows=group_follows))


def register(app, tx, origin, member, mutation, listing, entry_payload, read_works, title_for, note):
    def creator(con, secure):
        row = member(con, secure)
        if row["role"] == "viewer":
            abort(403, "A creator membership is required.")
        return row

    def owned_works(con, uid):
        owned = {row[0] for row in con.execute("SELECT work FROM team_owned_work WHERE member=?", (uid,))}
        return {work["id"]: work for work in read_works()
                if work["id"] in owned and not hidden(con, "work", work["id"])}

    def draft(con, row, works):
        profile = con.execute("SELECT * FROM creator_profile WHERE member=?", (row["id"],)).fetchone()
        if profile:
            settings = {key: profile[key] for key in ("handle", "name", "bio", "revision")}
            settings.update(links=json.loads(profile["links"]), visible=bool(profile["visible"]),
                            cover=profile["cover"] if profile["cover"] in works else None,
                            featured=[uid for uid in json.loads(profile["featured"]) if uid in works])
        else:
            settings = dict(handle="", name=row["name"], bio="", links=[], visible=False,
                            cover=None, featured=[], revision="")
        settings["url"] = url_for("creator_page", handle=settings["handle"]) if settings["handle"] else None
        settings["videos"] = sorted((dict(id=uid, title=title_for(con, work)) for uid, work in works.items()),
                                   key=lambda item: (item["title"].casefold(), item["id"]))
        return settings

    @app.get("/team/profile")
    def creator_editor():
        with tx() as con:
            creator(con, secure_site(origin))
        return private(render_template("creator_editor.html"))

    @app.route("/api/team/profile", methods=("GET", "PUT"))
    def creator_settings():
        secure = secure_site(origin)
        same_origin(secure)
        with tx() as con:
            row = creator(con, secure)
            uid = row["id"]
            works = owned_works(con, uid)
            if request.method == "PUT":
                body = mutation(secure)
                current = con.execute("SELECT revision FROM creator_profile WHERE member=?", (uid,)).fetchone()
                if body.get("revision") != (current["revision"] if current else ""):
                    abort(409, "This profile changed. Reload before saving.")
                handle = text_field(body.get("handle"), "a creator handle", 40).lower()
                name = text_field(body.get("name"), "a creator name", 120)
                bio = text_field(body.get("bio"), "a biography", 2000)
                visible, cover = body.get("visible"), body.get("cover")
                links, featured = body.get("links"), body.get("featured")
                if not HANDLE.fullmatch(handle) or not name:
                    abort(422, "Enter a name and a handle of 3 to 40 letters, numbers or hyphens.")
                if con.execute("SELECT 1 FROM creator_profile WHERE handle=? AND member<>?", (handle, uid)).fetchone():
                    abort(409, "That creator handle is already in use.")
                if type(visible) is not bool or (cover is not None and (not isinstance(cover, str) or cover not in works)):
                    abort(422, "Choose a profile visibility and a cover from your videos.")
                if not isinstance(links, list) or len(links) > 5:
                    abort(422, "Use up to five profile links.")
                links = list(dict.fromkeys(link for value in links if (link := web_link(value))))
                if (not isinstance(featured, list) or len(featured) > 24
                        or any(not isinstance(work, str) or work not in works for work in featured)):
                    abort(422, "Choose up to 24 featured videos from your library.")
                featured = list(dict.fromkeys(featured))
                con.execute("""INSERT INTO creator_profile VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(member) DO UPDATE SET handle=excluded.handle,name=excluded.name,bio=excluded.bio,
                    links=excluded.links,visible=excluded.visible,cover=excluded.cover,featured=excluded.featured,
                    revision=excluded.revision""",
                    (uid, handle, name, bio, json.dumps(links), visible, cover, json.dumps(featured), uuid.uuid4().hex))
                note(con, "team:" + uid, "creator_profile_saved", uid, "public" if visible else "hidden")
            return private(draft(con, row, works))

    @app.get("/creators/<handle>")
    def creator_page(handle):
        with tx() as con:
            row = con.execute(f"""SELECT p.* FROM creator_profile p JOIN team_member m ON m.id=p.member
                WHERE p.handle=? AND {VISIBLE}""", (handle,)).fetchone()
            if not row:
                abort(404)
            owned = {item[0] for item in con.execute("SELECT work FROM team_owned_work WHERE member=?", (row["member"],))}
            videos = {slug: entry_payload(entry) for (kind, slug), entry in listing(con).items()
                      if kind == "work" and slug in owned}
            cover = videos.get(row["cover"])
            featured = [videos[uid] for uid in json.loads(row["featured"]) if uid in videos]
            followers = follower_count(con, row["member"])
        response = make_response(render_template("creator_profile.html", name=row["name"], bio=row["bio"],
                                                 handle=row["handle"], links=json.loads(row["links"]),
                                                 cover=cover, featured=featured, video_count=len(videos),
                                                 followers=followers))
        response.cache_control.no_store = True
        return response
