"""Workspace identity displayed on team, creator and public catalog pages."""

import hashlib
import re
import uuid
from io import BytesIO

from flask import Response, abort, render_template, request, url_for
from PIL import Image, ImageOps

from browser_account_security import private, same_origin, secure_site


SCHEMA = """
CREATE TABLE IF NOT EXISTS team_branding(
 id INTEGER PRIMARY KEY CHECK(id=1), label TEXT NOT NULL, accent TEXT NOT NULL,
 logo TEXT REFERENCES review_logo(digest), revision TEXT NOT NULL);
"""
ACCENT = re.compile(r"#[0-9a-f]{6}")
LOGO_BYTES = 5_000_000
TYPES = {"PNG": "image/png", "JPEG": "image/jpeg"}


def read(con, institution):
    row = con.execute("SELECT label,accent,logo,revision FROM team_branding WHERE id=1").fetchone()
    if not row:
        return dict(label=institution, accent="#005b50", logo=None, revision="")
    settings = dict(row)
    if settings["logo"]:
        settings["logo"] = url_for("workspace_logo", digest=settings["logo"])
    return settings


def logo_bytes(upload):
    content = upload.read(LOGO_BYTES + 1)
    if len(content) > LOGO_BYTES:
        abort(413, "Use a JPEG or PNG image up to 5 MB.")
    try:
        with Image.open(BytesIO(content), formats=("PNG", "JPEG")) as image:
            mime = TYPES[image.format]
            if image.width * image.height > 25_000_000:
                abort(413, "Use a logo smaller than 25 million pixels.")
            oriented = ImageOps.exif_transpose(image)
            oriented.thumbnail((1024, 1024))
            normalized = Image.new("RGBA" if image.format == "PNG" else "RGB", oriented.size)
            normalized.paste(oriented.convert(normalized.mode))
            target = BytesIO()
            normalized.save(target, format=image.format)
    except (OSError, ValueError, Image.DecompressionBombError):
        abort(415, "Choose a readable JPEG or PNG image.")
    return mime, target.getvalue()


def register(app, tx, staff, institution, origin):
    @app.get("/team/settings")
    def workspace_settings_page():
        secure_site(origin)
        return private(render_template("team_settings.html"))

    @app.get("/team/brand/<digest>")
    def workspace_logo(digest):
        with tx() as con:
            logo = con.execute("""SELECT image.mime,image.content FROM team_branding b
                JOIN review_logo image ON image.digest=b.logo WHERE b.id=1 AND b.logo=?""", (digest,)).fetchone()
        if logo is None:
            abort(404)
        response = Response(logo["content"], mimetype=logo["mime"])
        response.cache_control.no_store = True
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/team/settings", methods=("GET", "PUT"))
    def workspace_settings():
        staff()
        if request.method in ("GET", "HEAD"):
            with tx() as con:
                return private(read(con, institution))
        secure = secure_site(origin)
        same_origin(secure)
        if request.headers.get("X-CuePorch-Team") != "1" or request.mimetype != "multipart/form-data":
            abort(403)
        request.max_content_length = LOGO_BYTES + 65_536
        label = request.form.get("name", "").strip()
        accent = request.form.get("accent", "").lower()
        if not 1 <= len(label) <= 32:
            abort(400, "Enter a workspace name of 1 to 32 characters.")
        if not ACCENT.fullmatch(accent):
            abort(400, "Choose a six-digit accent color.")
        upload = request.files.get("logo")
        image = logo_bytes(upload) if upload else None
        with tx() as con:
            current = con.execute("SELECT logo,revision FROM team_branding WHERE id=1").fetchone()
            if request.form.get("revision") != (current["revision"] if current else ""):
                abort(409, "Workspace branding changed. Reload settings before saving.")
            logo = current["logo"] if current and request.form.get("remove_logo") != "yes" else None
            if image:
                mime, content = image
                logo = hashlib.sha256(content).hexdigest()
                con.execute("INSERT OR IGNORE INTO review_logo VALUES(?,?,?)", (logo, mime, content))
            con.execute("""INSERT INTO team_branding VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                label=excluded.label,accent=excluded.accent,logo=excluded.logo,revision=excluded.revision""",
                (label, accent, logo, uuid.uuid4().hex))
            return private(read(con, institution))

    def workspace_brand():
        with tx() as con:
            return read(con, institution)

    return workspace_brand
