"""Stored watch page and embed appearance for archive-delivered videos."""

import base64
import json
import re

from flask import abort, request

from library_trash import hidden

SCHEMA = """
CREATE TABLE IF NOT EXISTS video_appearance(
 work TEXT NOT NULL, surface TEXT NOT NULL, settings TEXT NOT NULL, PRIMARY KEY(work,surface));
CREATE TABLE IF NOT EXISTS video_appearance_preset(
 surface TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE, settings TEXT NOT NULL,
 PRIMARY KEY(surface,name));
"""
SURFACES = ("watch", "embed")
COLORS = (("primary", "#164e5a"), ("accent", "#a44913"), ("icon", "#ffffff"),
          ("text", "#152e33"), ("background", "#f5f5ee"))
SWITCHES = {
    "show_title": True, "show_chrome": True, "ambient": False, "crop": False,
    "description": True, "credits": True, "tags": True, "categories": True, "license": True,
    "progress": True, "volume": True, "captions": True, "audio_tracks": True, "chapters": True, "speed": True, "fullscreen": True, "pip": True,
    "allow_seek": True, "right_click": True, "autoplay": False, "muted": False, "loop": False, "viewer_questions": True,
}
DEFAULTS = dict(COLORS) | SWITCHES | dict(theme="light", font="serif", play_position="bottom", logo=None)
PAGE_FIELDS = {
    "theme", "background", "text", "font", "logo", "show_chrome", "ambient", "crop",
}
PALETTE = re.compile(r"#[0-9a-fA-F]{6}")


def surface_defaults(surface):
    return DEFAULTS | {"show_title": surface == "watch"}


def read_settings(con, uid, surface):
    row = con.execute(
        "SELECT settings FROM video_appearance WHERE work=? AND surface=?", (uid, surface)
    ).fetchone()
    return surface_defaults(surface) | json.loads(row[0]) if row else None


def imported_settings(work, surface):
    appearance = work.get("source_embed_appearance")
    if (surface != "embed" or work.get("provider") != "vimeo"
            or not isinstance(appearance, dict) or not appearance):
        return None
    return surface_defaults(surface) | appearance


def resolve_settings(con, work, surface):
    existing = read_settings(con, work["id"], surface)
    if existing is not None:
        return existing
    return imported_settings(work, surface)


def brand_image(con, digest):
    if not digest:
        return None
    row = con.execute("SELECT mime,content FROM review_logo WHERE digest=?", (digest,)).fetchone()
    if row is None:
        return None
    encoded = base64.b64encode(row["content"]).decode("ascii")
    return f"data:{row['mime']};base64,{encoded}"


def validate(con, body, surface):
    if not isinstance(body, dict):
        abort(400, "Choose valid appearance settings.")
    values = surface_defaults(surface)
    for key, default in values.items():
        values[key] = body.get(key, default)
    if any(type(values[key]) is not bool for key in SWITCHES):
        abort(400, "Choose valid switches for the player and page.")
    for key, _ in COLORS:
        value = values[key]
        if not isinstance(value, str) or not PALETTE.fullmatch(value):
            abort(400, "Use six-digit hexadecimal colors.")
    for field, options in (
        ("theme", ("light", "dark")), ("font", ("serif", "sans", "mono")),
        ("play_position", ("bottom", "center", "auto")),
    ):
        if values[field] not in options:
            abort(400, "Choose an available layout, theme or font.")
    logo = values["logo"]
    if logo is not None and (
        not isinstance(logo, str)
        or not con.execute("SELECT 1 FROM review_logo WHERE digest=?", (logo,)).fetchone()
    ):
        abort(400, "Choose an uploaded logo.")
    return values


def store(con, uid, surface, values):
    con.execute(
        """INSERT INTO video_appearance VALUES(?,?,?) ON CONFLICT(work,surface)
           DO UPDATE SET settings=excluded.settings""",
        (uid, surface, json.dumps(values)),
    )


def register(app, tx, staff, get_work, read_works, title_for):
    @app.route("/api/library/work/<uid>/appearance/<surface>", methods=["GET", "PUT", "DELETE"])
    def video_appearance(uid, surface):
        staff()
        if surface not in SURFACES:
            abort(404)
        with tx() as con:
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            if request.method == "DELETE":
                con.execute("DELETE FROM video_appearance WHERE work=? AND surface=?", (uid, surface))
            if request.method == "PUT":
                body = request.get_json()
                values = validate(con, body, surface)
                target_all = body.get("apply_page_to_library", False)
                if type(target_all) is not bool or (target_all and surface != "watch"):
                    abort(400, "Page theme applies to video pages.")
                name = body.get("preset_name")
                if name is not None:
                    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
                        abort(400, "Enter a preset name of up to 80 characters.")
                    con.execute(
                        """INSERT INTO video_appearance_preset VALUES(?,?,?) ON CONFLICT(surface,name)
                           DO UPDATE SET settings=excluded.settings""",
                        (surface, name.strip(), json.dumps(values)),
                    )
                store(con, uid, surface, values)
                if target_all:
                    theme = {field: values[field] for field in PAGE_FIELDS}
                    for entry in read_works():
                        member = entry["id"]
                        if (member != uid and entry.get("asset_type", "video") == "video"
                                and not hidden(con, "work", member)):
                            previous = read_settings(con, member, surface) or surface_defaults(surface)
                            store(con, member, surface, previous | theme)
            current = read_settings(con, uid, surface)
            presets = {
                row["name"]: json.loads(row["settings"]) for row in con.execute(
                    "SELECT name,settings FROM video_appearance_preset WHERE surface=? ORDER BY name",
                    (surface,),
                )
            }
            settings = current or imported_settings(work, surface) or surface_defaults(surface)
            logos = {values["logo"] for values in [settings, *presets.values()] if values["logo"]}
            images = {digest: brand_image(con, digest) for digest in logos}
            title = title_for(con, work)
        return dict(title=title, settings=settings, is_default=current is None,
                    presets=[dict(name=name, settings=values) for name, values in presets.items()],
                    logos=images)

    @app.delete("/api/library/appearance-preset/<surface>/<path:name>")
    def delete_appearance_preset(surface, name):
        staff()
        if surface not in SURFACES:
            abort(404)
        with tx() as con:
            con.execute("DELETE FROM video_appearance_preset WHERE surface=? AND name=?", (surface, name))
        return "", 204
