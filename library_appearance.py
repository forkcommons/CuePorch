"""Stored watch page and embed appearance for archive-delivered videos."""

import base64
import hashlib
import json
import re

from flask import abort, request

from library_thumbnail import thumbnail_type
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
    if work.get("provider") != "vimeo":
        return None
    mapping = {
        "watch": "source_watch_appearance",
        "embed": "source_embed_appearance",
    }
    field = mapping.get(surface)
    if field is None:
        return None
    raw = work.get(field)
    appearance = dict(raw) if isinstance(raw, dict) else {}
    if surface == "embed":
        logo = work.get("source_embed_logo")
        if isinstance(logo, dict):
            digest = logo.get("sha256")
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                appearance = {**appearance, "logo": digest}
    if not appearance:
        return None
    return {**surface_defaults(surface), **appearance}


def resolve_settings(con, work, surface):
    existing = read_settings(con, work["id"], surface)
    if existing is not None:
        return existing
    return imported_settings(work, surface)


def source_logo_blob(root, work, surface, digest):
    if (
        root is None
        or not isinstance(work, dict)
        or work.get("provider") != "vimeo"
        or surface != "embed"
    ):
        return None
    return stored_image_blob(root, work.get("source_embed_logo"), digest)


def stored_image_blob(root, descriptor, digest):
    if not isinstance(descriptor, dict):
        return None
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    if (
        descriptor.get("sha256") != digest
        or type(descriptor.get("bytes")) is not int
    ):
        return None
    size = descriptor["bytes"]
    if size < 1 or size > 4 * 1024 * 1024:
        return None
    path = root / "objects" / digest
    try:
        with path.open("rb") as handle:
            data = handle.read(size + 1)
        mime = thumbnail_type(path)
    except OSError:
        return None
    if not mime:
        return None
    if (
        len(data) != size
        or hashlib.sha256(data).hexdigest() != digest
    ):
        return None
    return mime, data


def brand_image(con, digest, *, root=None, work=None, surface=None):
    if not digest:
        return None
    row = con.execute("SELECT mime,content FROM review_logo WHERE digest=?", (digest,)).fetchone()
    if row is not None:
        mime, content = row["mime"], row["content"]
    else:
        blob = source_logo_blob(root, work, surface, digest)
        if blob is None:
            return None
        mime, content = blob
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime};base64,{encoded}"


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


def register(app, tx, staff, get_work, read_works, title_for, root):
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
                if isinstance(body, dict) and body.get("logo"):
                    blob = source_logo_blob(root, work, surface, body.get("logo"))
                    if blob:
                        con.execute("INSERT OR IGNORE INTO review_logo(digest,mime,content) VALUES(?,?,?)", (body["logo"], *blob))
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
                            previous = resolve_settings(con, entry, surface) or surface_defaults(surface)
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
            images = {digest: brand_image(con, digest, root=root, work=work, surface=surface) for digest in logos}
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
