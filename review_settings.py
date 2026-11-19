"""Institution review defaults and first-upload review link creation."""

import hashlib
import json
import math
import secrets
import time

from flask import abort, request
from werkzeug.security import generate_password_hash

import review_feedback

LIFETIME = 30 * 86400
SCHEMA = """
CREATE TABLE IF NOT EXISTS review_defaults(
 id INTEGER PRIMARY KEY CHECK(id=1), settings TEXT NOT NULL, password_hash TEXT);
CREATE TABLE IF NOT EXISTS review_appearance(link TEXT PRIMARY KEY, settings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_logo(digest TEXT PRIMARY KEY, mime TEXT NOT NULL, content BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS review_upload(work TEXT PRIMARY KEY);
"""

APPEARANCE = {"dark": False, "show_other_videos": False, "logo": None}
DEFAULTS = {
    "lifetime_seconds": LIFETIME,
    "auto_create": False,
    "allow_download": False,
    "allow_versions": False,
    **review_feedback.OPTIONS,
    **APPEARANCE,
}


def read_defaults(con):
    row = con.execute(
        "SELECT settings,password_hash FROM review_defaults WHERE id=1"
    ).fetchone()
    return DEFAULTS | json.loads(row["settings"]) if row else DEFAULTS.copy(), row[
        "password_hash"
    ] if row else None


def appearance(con, uid):
    row = con.execute(
        "SELECT settings FROM review_appearance WHERE link=?", (uid,)
    ).fetchone()
    return APPEARANCE | json.loads(row[0]) if row else APPEARANCE.copy()


def write_appearance(con, uid, body):
    current = appearance(con, uid)
    settings = {key: body.get(key, default) for key, default in current.items()}
    if any(type(settings[key]) is not bool for key in ("dark", "show_other_videos")):
        abort(400, "Use a valid review appearance option.")
    logo = settings["logo"]
    if logo is not None and (
        not isinstance(logo, str)
        or not con.execute(
            "SELECT 1 FROM review_logo WHERE digest=?", (logo,)
        ).fetchone()
    ):
        abort(400, "Choose an uploaded review logo.")
    con.execute(
        "INSERT OR REPLACE INTO review_appearance VALUES(?,?)",
        (uid, json.dumps(settings)),
    )


def insert_link(con, uid, work, name, expires, password_hash, choices):
    con.execute(
        """INSERT INTO review_link
           (id,work,name,expires,password_hash,allow_download,allow_versions,created)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            uid,
            work,
            name,
            expires,
            password_hash,
            choices["allow_download"],
            choices["allow_versions"],
            time.time(),
        ),
    )
    review_feedback.save_settings(con, uid, choices)
    write_appearance(con, uid, choices)


def new_upload(con, work):
    """Apply the current defaults once for each newly received work."""
    stamp = con.execute("INSERT OR IGNORE INTO review_upload VALUES(?)", (work["id"],))
    defaults, password_hash = read_defaults(con)
    if not stamp.rowcount or not defaults["auto_create"]:
        return
    insert_link(
        con,
        secrets.token_urlsafe(32),
        work["id"],
        work["title"][:128],
        time.time() + defaults["lifetime_seconds"],
        password_hash,
        defaults,
    )


def register(app, tx, staff):
    @app.get("/api/review-defaults")
    def review_defaults():
        staff()
        with tx() as con:
            current, password_hash = read_defaults(con)
        return current | {"password": bool(password_hash)}

    @app.put("/api/review-defaults")
    def save_review_defaults():
        staff()
        request.max_content_length = 16384
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        with tx() as con:
            current, password_hash = read_defaults(con)
            updated = {key: body.get(key, default) for key, default in current.items()}
            if any(
                type(updated[key]) is not bool
                for key, default in DEFAULTS.items()
                if isinstance(default, bool)
            ):
                abort(400, "Use a valid review setting.")
            lifetime = updated["lifetime_seconds"]
            if (
                type(lifetime) not in (int, float)
                or not 0 < lifetime < 253402300799 - time.time()
                or not math.isfinite(lifetime)
            ):
                abort(400, "Choose a future expiration date.")
            logo = updated["logo"]
            if logo is not None and (
                not isinstance(logo, str)
                or not con.execute(
                    "SELECT 1 FROM review_logo WHERE digest=?", (logo,)
                ).fetchone()
            ):
                abort(400, "Choose an uploaded review logo.")
            if "password" in body:
                password = body["password"]
                if not isinstance(password, str) or len(password) > 256:
                    abort(400, "Enter a password of up to 256 characters.")
                password_hash = generate_password_hash(password) if password else None
            con.execute(
                "INSERT OR REPLACE INTO review_defaults VALUES(1,?,?)",
                (json.dumps(updated), password_hash),
            )
        return updated | {"password": bool(password_hash)}

    @app.post("/api/review-logos")
    def upload_review_logo():
        staff()
        request.max_content_length = 4 * 1024**2
        upload = request.files.get("logo")
        if not upload:
            abort(400, "Choose a logo image.")
        content = upload.read()
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif content.startswith(b"\xff\xd8"):
            mime = "image/jpeg"
        elif content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            abort(415, "Choose a JPEG, PNG or WebP logo.")
        digest = hashlib.sha256(content).hexdigest()
        with tx() as con:
            con.execute(
                "INSERT OR IGNORE INTO review_logo VALUES(?,?,?)",
                (digest, mime, content),
            )
        return {"logo": digest}, 201
