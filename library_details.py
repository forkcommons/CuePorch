"""Version-independent editorial metadata for a retained video."""

import json
from urllib.parse import urlsplit

from flask import abort, request

import review_feedback
from browser_account_security import private
from catalog import video_version_key
from library_privacy import playback
from media_tools import video_properties

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_video_details(
 work TEXT PRIMARY KEY, metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS library_embed_pages(work TEXT PRIMARY KEY, pages TEXT NOT NULL);
"""
LICENSES = {"by", "by-sa", "by-nd", "by-nc", "by-nc-sa", "by-nc-nd", "cc0"}
CONTENT = {"language", "drugs", "violence", "nudity"}
RATINGS = {"unrated", "all", "mature"}


def parse_embed_pages(pages):
    if not isinstance(pages, list) or len(pages) > 100:
        abort(400, "Enter up to 100 pages where this video is embedded.")
    cleaned = []
    for value in pages:
        if not isinstance(value, str) or not 1 <= len(url := value.strip()) <= 2048 or any(
            char.isspace() or ord(char) < 32 or char == "\\" for char in url
        ):
            abort(400, "Enter one complete website URL per line.")
        try:
            parts = urlsplit(url)
            valid = (parts.scheme in ("http", "https") and parts.hostname and parts.username is None
                     and (parts.port is None or parts.port > 0))
        except ValueError:
            valid = False
        if not valid:
            abort(400, "Use HTTP or HTTPS page URLs without credentials.")
        cleaned.append(url)
    return list(dict.fromkeys(cleaned))


def names(values):
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        label.strip() for value in values
        if isinstance(label := (value.get("name") or value.get("tag")) if isinstance(value, dict) else value, str)
        and label.strip()
    ))


def source_details(work):
    credits = work.get("credits")
    if isinstance(credits, str):
        credits = [{"name": part.strip(), "role": ""} for part in credits.splitlines() if part.strip()]
    elif isinstance(credits, list):
        credits = [dict(name=entry["name"], role=entry.get("role") or "") for entry in credits
                   if isinstance(entry, dict) and isinstance(entry.get("name"), str)]
    else:
        credits = []
    rating = set(names(work.get("source_content_rating")))
    warnings = sorted(rating & CONTENT)
    license_id = work.get("source_license")
    return dict(
        description=work.get("description") or "",
        tags=names(work.get("source_tags")),
        categories=names(work.get("source_categories")),
        credits=credits,
        license=license_id if isinstance(license_id, str) and license_id in LICENSES else None,
        rating="mature" if warnings else "all" if "safe" in rating else "unrated",
        content=warnings,
        realistic_ai=work.get("source_realistic_ai") is True,
        advertisement="advertisement" in rating,
    )


def details_for(con, work):
    row = con.execute("SELECT metadata FROM library_video_details WHERE work=?", (work["id"],)).fetchone()
    return json.loads(row["metadata"]) if row else source_details(work)


def labels(values, name):
    if not isinstance(values, list) or len(values) > 100 or any(
        not isinstance(value, str) or not 1 <= len(value.strip()) <= 120 for value in values
    ):
        abort(400, f"Enter {name} as text labels of up to 120 characters.")
    return names(values)


def parse_details(body):
    if not isinstance(body, dict):
        abort(400)
    description = body.get("description")
    if not isinstance(description, str) or len(description) > 5000:
        abort(400, "Use up to 5,000 characters for the description.")
    tags = labels(body.get("tags"), "tags")
    categories = labels(body.get("categories"), "categories")
    credits = body.get("credits")
    if not isinstance(credits, list) or len(credits) > 100 or any(
        not isinstance(item, dict) or not isinstance(item.get("name"), str)
        or not 1 <= len(item["name"].strip()) <= 120
        or not isinstance(item.get("role", ""), str) or len(item.get("role", "").strip()) > 120
        for item in credits
    ):
        abort(400, "Enter a name and an optional role for each credit.")
    license_id = body.get("license")
    if license_id is not None and (not isinstance(license_id, str) or license_id not in LICENSES):
        abort(400, "Choose an available copyright license.")
    rating = body.get("rating")
    if not isinstance(rating, str) or rating not in RATINGS:
        abort(400, "Choose a content rating.")
    content = labels(body.get("content", []), "content descriptors")
    if not set(content) <= CONTENT or (content and rating != "mature"):
        abort(400, "Content descriptors are available for mature videos.")
    if any(type(body.get(field)) is not bool for field in ("realistic_ai", "advertisement")):
        abort(400, "Choose a value for each disclosure.")
    return dict(
        description=description, tags=tags, categories=categories, license=license_id,
        credits=[dict(name=item["name"].strip(), role=item.get("role", "").strip()) for item in credits],
        rating=rating, content=content, realistic_ai=body["realistic_ai"], advertisement=body["advertisement"],
    )


def register(app, tx, authorize_editor, root, get_work, title_for, save_title):
    @app.route("/api/library/work/<uid>/editorial-status", methods=["GET", "PUT"])
    def editorial_status(uid):
        with tx() as con:
            authorize_editor(con, uid)
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            version = video_version_key(work)
            if request.method == "PUT":
                body = request.get_json()
                if not isinstance(body, dict) or body.get("version") != version:
                    abort(409, "The current version changed. Reload it before setting a status.")
                review_feedback.set_editorial_status(con, uid, version, body.get("status"))
            state = review_feedback.editorial_status(con, uid, version)
        return private(dict(status=state or review_feedback.STATUSES[0], version=version))

    @app.route("/api/library/work/<uid>/details", methods=["GET", "PUT"])
    def video_details(uid):
        with tx() as con:
            authorize_editor(con, uid)
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            saved_pages = con.execute("SELECT pages FROM library_embed_pages WHERE work=?", (uid,)).fetchone()
            embed_pages = json.loads(saved_pages[0]) if saved_pages else work.get("known_embed_pages", [])
            if not isinstance(embed_pages, list):
                embed_pages = []
            if request.method == "PUT":
                body = request.get_json()
                state = parse_details(body)
                if "embed_pages" in body:
                    embed_pages = parse_embed_pages(body["embed_pages"])
                    con.execute("""INSERT INTO library_embed_pages VALUES(?,?)
                        ON CONFLICT(work) DO UPDATE SET pages=excluded.pages""", (uid, json.dumps(embed_pages)))
                save_title(con, work, body.get("title"))
                con.execute(
                    """INSERT INTO library_video_details VALUES(?,?) ON CONFLICT(work) DO UPDATE SET
                       metadata=excluded.metadata""", (uid, json.dumps(state)),
                )
            else:
                state = details_for(con, work)
            title = title_for(con, work)
        media = next((item for item in work["files"] if item["role"] == "original"), None) or playback(work)
        measured = video_properties(root / "objects" / media["sha256"]) if media else {}
        return private(dict(
            **state, title=title, embed_pages=embed_pages,
            media=dict(**measured, format=media["format"], bytes=media["bytes"]) if media else None,
            duration_seconds=work.get("duration_seconds"),
            projection=work.get("source_projection"),
            added_at=work.get("source_created_time") or work.get("library_added_at"),
        ))
