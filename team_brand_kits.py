"""Versioned reusable branding and archived video bookends."""

import json
import uuid

from flask import abort, render_template, request

from browser_account_security import private, same_origin, secure_site
from catalog import video_version_key
from library_appearance import COLORS, brand_image, validate

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_brand_kit(
 id TEXT PRIMARY KEY, name TEXT COLLATE NOCASE UNIQUE, revision TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS team_brand_kit_revision(
 revision TEXT PRIMARY KEY, kit TEXT NOT NULL, settings TEXT NOT NULL);
"""
VISUAL_FIELDS = [name for name, _ in COLORS] + ["font", "logo"]


def written_kit(con, kit_id, revision):
    row = con.execute("SELECT settings FROM team_brand_kit_revision WHERE kit=? AND revision=?",
                      (kit_id, revision)).fetchone()
    if row is None:
        abort(409, "This version of the brand kit is unavailable. Choose another kit.")
    return json.loads(row[0])


def current_kits(con):
    rows = con.execute("""SELECT r.settings FROM team_brand_kit k
          JOIN team_brand_kit_revision r ON r.revision=k.revision ORDER BY k.name""")
    return [json.loads(row[0]) for row in rows]


def register(app, tx, staff, origin, root, get_work, video_source):
    @app.get("/operator/brand-kits")
    def brand_kits_page():
        secure_site(origin)
        return private(render_template("brand_kits.html", colors=COLORS, team=request.args.get("team") == "1"))

    def check_mutation():
        secure = secure_site(origin)
        same_origin(secure)
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400, "Enter brand kit settings.")
        return body

    def clip(con, raw, previous):
        if raw is None:
            return None
        if not isinstance(raw, str):
            abort(400, "Choose an archived video for the intro or outro.")
        selected = get_work(con, raw)
        if selected.get("asset_type", "video") != "video":
            abort(400, "Choose an archived video for the intro or outro.")
        if previous and previous["work"] == raw:
            return previous
        _, info = video_source(root, selected)
        end = round(info["duration"] * 1000)
        if end < 100:
            abort(422, "Choose a clip at least 0.1 seconds long.")
        return dict(work=raw, cut=video_version_key(selected), start=0, end=end)

    def save(con, kit_id, body, previous=None):
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            abort(400, "Enter a brand kit name of up to 80 characters.")
        name = name.strip()
        if con.execute("SELECT 1 FROM team_brand_kit WHERE name=? AND id<>?", (name, kit_id)).fetchone():
            abort(409, "That brand kit name is already in use.")
        style = validate(con, body.get("style"), "watch")
        revision = uuid.uuid4().hex
        previous = previous or {}
        kit = dict(id=kit_id, revision=revision, name=name,
                   style={field: style[field] for field in VISUAL_FIELDS},
                   intro=clip(con, body.get("intro"), previous.get("intro")),
                   outro=clip(con, body.get("outro"), previous.get("outro")))
        con.execute("INSERT INTO team_brand_kit_revision VALUES (?,?,?)", (revision, kit_id, json.dumps(kit)))
        con.execute("""INSERT INTO team_brand_kit VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,revision=excluded.revision""", (kit_id, name, revision))
        return kit

    @app.route("/api/library/brand-kits", methods=["GET", "POST"])
    def brand_kits_api():
        staff()
        body = check_mutation() if request.method == "POST" else None
        with tx() as con:
            if body is not None:
                return private(save(con, uuid.uuid4().hex, body), 201)
            kits = current_kits(con)
            brand = con.execute("SELECT logo FROM team_branding WHERE id=1").fetchone()
            workspace_logo = brand[0] if brand else None
            digests = {kit["style"]["logo"] for kit in kits} | {workspace_logo}
            images = {digest: brand_image(con, digest) for digest in digests if digest}
            return private(dict(kits=kits, logos=images, workspace_logo=workspace_logo))

    @app.route("/api/library/brand-kits/<kit_id>", methods=["PUT", "DELETE"])
    def brand_kit_api(kit_id):
        staff()
        body = check_mutation()
        with tx() as con:
            row = con.execute("SELECT revision FROM team_brand_kit WHERE id=?", (kit_id,)).fetchone()
            if row is None:
                abort(404, "This brand kit has been removed.")
            if body.get("revision") != row["revision"]:
                abort(409, "The brand kit changed. Open it again before editing.")
            if request.method == "DELETE":
                con.execute("DELETE FROM team_brand_kit WHERE id=?", (kit_id,))
                return private("", 204)
            return private(save(con, kit_id, body, written_kit(con, kit_id, row["revision"])))
