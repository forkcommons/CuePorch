"""An editable workspace changelog with stable announcements and member feedback."""

import re
import uuid
from datetime import date

from flask import abort, render_template, request

from browser_account_security import json_mutation, private, same_origin, secure_site
from workspace_help import text_field, workspace_identity

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_update(
 slug TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, area TEXT NOT NULL,
 category TEXT NOT NULL, state TEXT NOT NULL, date TEXT NOT NULL, revision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_update_vote(
 slug TEXT NOT NULL REFERENCES workspace_update(slug), voter TEXT NOT NULL,
 vote TEXT NOT NULL CHECK(vote IN ('up','down')), PRIMARY KEY(slug, voter));
"""
AREAS = ("Workspace", "Library", "Playback", "Creation", "Live", "Community", "Analytics")
CATEGORIES = ("New feature", "Improvement", "Fix")
PUBLIC_STATES = ("released", "up-next")
SLUG = re.compile("[a-z0-9]+(?:-[a-z0-9]+)*")


def initialize(con):
    con.executemany(
        "INSERT OR IGNORE INTO workspace_update VALUES(?,?,?,?,?,'up-next',?,?)",
        [
            ("vimeo-account-move", "Vimeo account move",
             "The development build can rehearse a Vimeo migration, verify recovered files and replace embeds. "
             "Full parity and the final signed release are in development.",
             "Library", "Improvement", "2026-11-20", "built-in"),
            ("panoramic-browser-video", "Panoramic playback",
             "The development watch player can pan across 180° and 360° equirectangular videos. Native package "
             "tests and hardware formats remain in development.",
             "Playback", "New feature", "2026-11-20", "built-in"),
        ],
    )


def choices():
    return dict(areas=AREAS, categories=CATEGORIES, team=request.args.get("team") == "1")


def values(body):
    title = text_field(body, "title", 120, "a title")
    description = text_field(body, "description", 8000, "an announcement")
    area, category, state = (body.get(key) for key in ("area", "category", "state"))
    if area not in AREAS or category not in CATEGORIES or state not in (*PUBLIC_STATES, "draft"):
        abort(400, "Choose a product area, category and publication state.")
    published = body.get("date")
    try:
        if not isinstance(published, str) or date.fromisoformat(published).isoformat() != published:
            raise ValueError
    except ValueError:
        abort(400, "Enter an announcement date.")
    return title, description, area, category, state, published, uuid.uuid4().hex


def find(con, slug, public=False):
    entry = con.execute("SELECT * FROM workspace_update WHERE slug=?", (slug,)).fetchone()
    if not entry or (public and entry["state"] not in PUBLIC_STATES):
        abort(404)
    return entry


def register(app, tx, origin, team_member, operator_name):
    @app.get("/whats-new")
    def whats_new():
        state = request.args.get("view", "released")
        if state not in PUBLIC_STATES:
            abort(404)
        area = request.args.get("area", "")
        category = request.args.get("category", "")
        query = request.args.get("q", "")[:160].strip()
        words = query.casefold().split()
        with tx() as con:
            entries = con.execute("SELECT * FROM workspace_update WHERE state=? ORDER BY date DESC,title,slug",
                                  (state,)).fetchall()
        results = [entry for entry in entries if (not area or entry["area"] == area)
                   and (not category or entry["category"] == category)
                   and all(word in (entry["title"] + " " + entry["description"]).casefold() for word in words)]
        return render_template("workspace_updates.html", entries=results, state=state,
                               area=area, category=category, query=query, detail=False, **choices())

    @app.get("/whats-new/<slug>")
    def whats_new_entry(slug):
        with tx() as con:
            entry = find(con, slug, public=True)
        return render_template("workspace_updates.html", entries=[entry], state=entry["state"],
                               detail=True, **choices())

    @app.get("/whats-new/manage")
    def manage_updates():
        secure_site(origin)
        return private(render_template("workspace_updates_manage.html", **choices()))

    def access(con, manage=False):
        secure = secure_site(origin)
        if request.method == "GET":
            same_origin(secure)
            body = None
        else:
            body = json_mutation(secure, "X-CuePorch-Team")
        actor, supervisor = workspace_identity(con, team_member, operator_name)
        if manage and not supervisor:
            abort(403, "Administrator or Owner access is required to edit announcements.")
        return actor, supervisor, body

    @app.route("/api/team/updates", methods=["GET", "POST"])
    def updates_api():
        with tx() as con:
            _, _, body = access(con, manage=True)
            if request.method == "GET":
                entries = con.execute("SELECT * FROM workspace_update ORDER BY date DESC,title,slug")
                return private(dict(entries=[dict(entry) for entry in entries]))
            slug = body.get("slug")
            if not isinstance(slug, str) or len(slug) > 80 or slug == "manage" or not SLUG.fullmatch(slug):
                abort(400, "Use up to 80 lowercase letters, numbers and hyphens for the link name.")
            entry = (slug, *values(body))
            if not con.execute("INSERT OR IGNORE INTO workspace_update VALUES(?,?,?,?,?,?,?,?)", entry).rowcount:
                abort(409, "That announcement address already exists.")
            return private(dict(find(con, slug)), 201)

    @app.route("/api/team/updates/<slug>", methods=["GET", "PUT"])
    def update_api(slug):
        with tx() as con:
            _, _, body = access(con, manage=True)
            current = find(con, slug)
            if request.method == "PUT":
                if body.get("revision") != current["revision"]:
                    abort(409, "The announcement changed. Open it again before saving.")
                con.execute("""UPDATE workspace_update SET title=?,description=?,area=?,category=?,state=?,date=?,revision=?
                            WHERE slug=?""", (*values(body), slug))
            counts = dict(con.execute("SELECT vote,count(*) FROM workspace_update_vote WHERE slug=? GROUP BY vote",
                                      (slug,)).fetchall())
            return private(dict(find(con, slug)) | {"feedback": counts})

    @app.route("/api/team/updates/<slug>/feedback", methods=["GET", "PUT", "DELETE"])
    def update_feedback(slug):
        with tx() as con:
            actor, _, body = access(con)
            find(con, slug, public=True)
            vote = None
            if request.method == "PUT":
                vote = body.get("vote")
                if vote not in ("up", "down"):
                    abort(400, "Choose thumbs up or thumbs down.")
                con.execute("""INSERT INTO workspace_update_vote VALUES(?,?,?)
                    ON CONFLICT(slug,voter) DO UPDATE SET vote=excluded.vote""", (slug, actor, vote))
            elif request.method == "DELETE":
                con.execute("DELETE FROM workspace_update_vote WHERE slug=? AND voter=?", (slug, actor))
            else:
                row = con.execute("SELECT vote FROM workspace_update_vote WHERE slug=? AND voter=?", (slug, actor)).fetchone()
                vote = row[0] if row else None
            return private(dict(vote=vote))

    @app.get("/api/team/update-feedback")
    def updates_feedback():
        with tx() as con:
            actor, supervisor, _ = access(con)
            votes = con.execute("""SELECT v.slug,v.vote FROM workspace_update_vote v
                JOIN workspace_update u ON u.slug=v.slug
                WHERE v.voter=? AND u.state IN ('released','up-next')""", (actor,))
            return private(dict(votes=dict(votes.fetchall()), can_manage=supervisor))
