"""Chapter and viewer interactivity settings for retained video cuts."""

import hashlib
import json
from urllib.parse import urlsplit

from filelock import FileLock
from flask import abort, make_response, request

from catalog import cut_chapters, video_version_key
from cueporch import dump


SCHEMA = """
CREATE TABLE IF NOT EXISTS video_presentation(
 work TEXT NOT NULL,version TEXT NOT NULL,settings TEXT NOT NULL,PRIMARY KEY(work,version));
"""
END_MODES = ("default", "more", "action", "share", "image", "loop", "thumbnail", "empty", "message")
CARD_SECONDS = 6


def readable_chapters(work):
    import re

    files = work.get("files", []) if isinstance(work, dict) else []
    pictures = {}
    for item in (files if isinstance(files, list) else []):
        if not isinstance(item, dict):
            continue
        if item.get("role") != "chapter_picture":
            continue
        uri = item.get("chapter_uri")
        sha256 = item.get("sha256")
        if (
            isinstance(uri, str)
            and isinstance(sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            pictures.setdefault(uri, sha256)

    chapters = []
    for entry in cut_chapters(work):
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("title"), str)
            and type(entry.get("timecode")) in (float, int)
            and 0 <= entry["timecode"] < 1_000_000_000
        ):
            chapter = dict(title=entry["title"], timecode=int(entry["timecode"]))
            uri = entry.get("uri")
            if isinstance(uri, str) and uri in pictures:
                chapter["picture"] = pictures[uri]
            chapters.append(chapter)

    return sorted(chapters, key=lambda item: item["timecode"])


def revision(work):
    return hashlib.sha256(json.dumps(cut_chapters(work), sort_keys=True).encode()).hexdigest()


def validate_chapters(values, duration):
    if not isinstance(values, list) or len(values) > 80:
        abort(422, "A video can have up to 80 chapters.")
    chapters = []
    starts = set()
    for item in values:
        if not isinstance(item, dict):
            abort(422, "Enter a title and time for each chapter.")
        start, title = item.get("timecode"), item.get("title")
        if (type(start) is not int or not 0 <= start < 1_000_000_000
                or (duration and start >= duration)):
            abort(422, "Enter chapter times in whole seconds within the video.")
        if start in starts:
            abort(422, "Each chapter needs its own start time.")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 50:
            abort(422, "Enter chapter titles of 1 to 50 characters.")
        starts.add(start)
        chapters.append(dict(title=title.strip(), timecode=start))
    return sorted(chapters, key=lambda item: item["timecode"])


def text_field(value, label, limit, required=False):
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        abort(422, f"Enter {label} of up to {limit} characters.")
    return value.strip()


def selected_ids(items, label):
    if not isinstance(items, list) or not 1 <= len(items) <= 50 or any(not isinstance(item, str) for item in items):
        abort(422, f"Select between 1 and 50 {label}.")
    return list(dict.fromkeys(items))


def web_link(value):
    value = text_field(value, "a link", 2048)
    if not value:
        return ""
    try:
        url = urlsplit(value)
        if (url.scheme in ("https", "http") and url.hostname and url.username is None
                and not any(char.isspace() or char == "\\" for char in value)):
            return value
    except ValueError:
        pass
    abort(422, "Enter an HTTP or HTTPS link.")


def image_id(con, value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not con.execute(
        "SELECT 1 FROM review_logo WHERE digest=?", (value,)
    ).fetchone():
        abort(422, "Choose an uploaded image.")
    return value


def presentation(con, work, *, embed=False):
    row = con.execute("SELECT settings FROM video_presentation WHERE work=? AND version=?",
                      (work["id"], video_version_key(work))).fetchone()
    if row:
        return json.loads(row[0])
    ending = {"mode": "default"}
    if embed and work.get("provider") == "vimeo":
        mode = work.get("source_embed_ending")
        if mode in ("empty", "loop", "share", "thumbnail"):
            ending = {"mode": mode}
    return dict(cards=[], ending=ending)


def public_videos(con, read_works, title_for, eligible, except_id):
    hosted = {
        row[0] for row in con.execute(
            """SELECT slug FROM route r WHERE kind='work'
               AND NOT EXISTS (SELECT 1 FROM library_peer_handoff h WHERE h.work=r.slug)"""
        )
    }
    private = {
        row[0] for row in con.execute(
            """SELECT item FROM library_deleted_item WHERE kind='work'
               UNION SELECT work FROM library_privacy_change
               UNION SELECT work FROM case_hold WHERE active=1
               UNION SELECT work FROM library_peer_handoff WHERE state='pending'"""
        )
    }
    settings = dict(con.execute("SELECT work,settings FROM library_privacy"))
    entries = []
    for item in read_works():
        uid = item["id"]
        if uid == except_id or uid in private or item.get("asset_type", "video") != "video":
            continue
        access = json.loads(settings[uid]) if uid in settings else None
        if ((access is not None and access["visibility"] == "public" and eligible(item, access))
                or (access is None and uid in hosted)):
            entries.append(dict(id=uid, title=title_for(con, item)))
    return sorted(entries, key=lambda item: (item["title"].casefold(), item["id"]))


def presentation_settings(con, work, body, videos):
    if not isinstance(body, dict) or not isinstance(body.get("ending"), dict):
        abort(400, "Enter the card and end screen settings.")
    cards = body.get("cards")
    if not isinstance(cards, list) or len(cards) > 100:
        abort(422, "A video can have up to 100 cards.")
    duration = work.get("duration_seconds")
    windows = []
    for card in cards:
        start = card.get("start") if isinstance(card, dict) else None
        if type(start) is not int or not 0 <= start < 1_000_000_000 or (duration and start >= duration):
            abort(422, "Choose a card start time within the video.")
        if any(abs(start - other["start"]) < CARD_SECONDS for other in windows):
            abort(422, "Space cards at least six seconds apart.")
        windows.append(dict(start=start, title=text_field(card.get("title"), "a card title", 80, True),
                            text=text_field(card.get("text", ""), "a card description", 300),
                            link=web_link(card.get("link", "")), image=image_id(con, card.get("image"))))
    ending = body["ending"]
    mode = ending.get("mode")
    if mode not in END_MODES:
        abort(422, "Choose an available end screen.")
    result = {"mode": mode}
    if mode == "more":
        choices = ending.get("videos")
        public = {item["id"] for item in videos}
        if (not isinstance(choices, list) or not 1 <= len(choices) <= 3
                or any(not isinstance(uid, str) or uid not in public for uid in choices)
                or len(set(choices)) != len(choices)):
            abort(422, "Choose one to three distinct public videos.")
        result["videos"] = choices
    if mode in ("more", "action", "share", "image", "message"):
        result["image"] = image_id(con, ending.get("image"))
    if mode == "image" and not result["image"]:
        abort(422, "Choose an image for the end screen.")
    if mode in ("action", "image"):
        result["link"] = web_link(ending.get("link", ""))
    if mode == "action":
        if not result["link"]:
            abort(422, "Enter a link for the call to action.")
        result["label"] = text_field(ending.get("label"), "a button label", 60, True)
    if mode in ("action", "message"):
        result["title"] = text_field(ending.get("title", ""), "an end screen title", 80)
        result["text"] = text_field(ending.get("text", ""), "an end screen message", 300, mode == "message")
    return dict(cards=sorted(windows, key=lambda card: card["start"]), ending=result)


def for_viewer(con, work, public_options, *, embed=False):
    values = presentation(con, work, embed=embed)
    for card in values["cards"]:
        card["end"] = card["start"] + CARD_SECONDS
    ending = values["ending"]
    if ending["mode"] == "more":
        public = {item["id"]: item for item in public_options()}
        ending["videos"] = [public[uid] for uid in ending["videos"] if uid in public]
        if not ending["videos"]:
            ending["mode"] = "empty"
    return values


def artwork(con, work, digest):
    if digest not in assigned_images(presentation(con, work)):
        return None
    return con.execute("SELECT mime,content FROM review_logo WHERE digest=?", (digest,)).fetchone()


def assigned_images(settings):
    return {card["image"] for card in settings["cards"] if card["image"]} | (
        {settings["ending"]["image"]} if settings["ending"].get("image") else set()
    )


def register(app, tx, authorize_editor, get_work, title_for, root, remember_work, read_works, permits):
    @app.route("/api/library/work/<uid>/presentation", methods=["GET", "PUT"])
    def video_presentation(uid):
        if request.method == "PUT":
            request.max_content_length = 3 * 1024**2
        with tx() as con:
            scoped = authorize_editor(con, uid, advanced=True)
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            videos = public_videos(con, read_works, title_for, permits, uid)
            current = presentation(con, work)
            version = video_version_key(work)
            if request.method == "PUT":
                body = request.get_json()
                if not isinstance(body, dict):
                    abort(400, "Enter the interactivity settings.")
                if body.get("version") != version or body.get("previous") != current:
                    abort(409, "This video or its interactivity changed. Reopen the editor.")
                settings = presentation_settings(con, work, body, videos)
                if scoped and not assigned_images(settings) <= assigned_images(current):
                    abort(403, "Ask an Administrator to add new artwork to this video.")
                current = settings
                con.execute("""INSERT INTO video_presentation VALUES(?,?,?)
                            ON CONFLICT(work,version) DO UPDATE SET settings=excluded.settings""",
                            (uid, version, json.dumps(current)))
            return make_response(dict(version=version, settings=current, public_videos=videos),
                                 {"Cache-Control": "private, no-store"})

    @app.route("/api/library/work/<uid>/chapters", methods=["GET", "PUT"])
    def video_chapters(uid):
        if request.method == "PUT":
            request.max_content_length = 64 * 1024
        with tx() as con:
            authorize_editor(con, uid, advanced=True)
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            if request.method == "PUT":
                body = request.get_json()
                if not isinstance(body, dict):
                    abort(400, "Enter chapter details.")
                with FileLock(str(root / ".catalog.lock"), timeout=30):
                    path = root / "manifest.json"
                    manifest = json.loads(path.read_text(encoding="utf8"))
                    work = next((item for item in manifest["works"] if item["id"] == uid), None)
                    if work is None:
                        abort(404)
                    version = video_version_key(work)
                    if (body.get("version"), body.get("revision")) != (version, revision(work)):
                        abort(409, "This video or its chapters changed. Reopen the editor.")
                    chapters = validate_chapters(body.get("chapters"), work.get("duration_seconds"))
                    work.setdefault("chapter_edits", {})[version] = chapters
                    dump(path, manifest)
                remember_work(work)
            return make_response(dict(title=title_for(con, work), version=video_version_key(work),
                                      revision=revision(work), chapters=readable_chapters(work)),
                                 {"Cache-Control": "private, no-store"})
