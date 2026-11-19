"""Persist a browser studio's scene order, graphics and brand assets."""

import json
import re
import uuid

from flask import abort, request, send_file

from catalog import video_version_key
from library_actions import title_for
from library_interactivity import text_field
from library_privacy import blocked


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_scene_deck(
 event TEXT PRIMARY KEY, revision TEXT NOT NULL, settings TEXT NOT NULL);
"""
ID = re.compile("[0-9a-f]{32}")
COLOR = re.compile("#[0-9a-fA-F]{6}")
MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
TEMPLATES = (
    dict(title="Opening title", layout="title", background="gradient", color="#244b4d", subtitle=""),
    dict(title="Presenter", layout="screen", background="solid", color="#203235", subtitle=""),
    dict(title="Presentation and camera", layout="inset", background="solid", color="#244b4d", subtitle=""),
    dict(title="Side by side", layout="side", background="solid", color="#203235", subtitle=""),
)


def images(con, works):
    available = {}
    for work in works:
        uid = work["id"]
        if work.get("asset_type") != "image" or blocked(con, uid):
            continue
        candidates = [file for file in work["files"] if file["format"] in MIME and file["role"] in ("playback", "original")]
        selected = next((file for file in candidates if file["role"] == "playback"), candidates[0] if candidates else None)
        if selected:
            available[uid] = dict(title=title_for(con, work), object=selected["sha256"], format=selected["format"])
    return available


def color(value):
    if not isinstance(value, str) or not COLOR.fullmatch(value):
        abort(422, "Choose a six-digit color.")
    return value.lower()


def videos(con, works, selected):
    visible = {}
    for work in works:
        uid = work["id"]
        if work.get("asset_type", "video") != "video" or blocked(con, uid):
            continue
        name = title_for(con, work)
        current = video_version_key(work)
        for cut in (work, *work.get("versions_history", [])):
            version = video_version_key(cut)
            if version != current and (uid, version) not in selected:
                continue
            if any(file["role"] in ("playback", "original") and file["format"] in ("mp4", "webm") for file in cut["files"]):
                visible[uid, version] = name if version == current else name + " (retained cut)"
    return visible


def picture(value, choices):
    if value is not None and (not isinstance(value, str) or value not in choices):
        abort(422, "Choose an available library image.")
    return value


def settings(body, choices, playable):
    if not isinstance(body, dict) or not isinstance(body.get("brand"), dict):
        abort(422, "Enter studio scene settings.")
    brand = body["brand"]
    brand = dict(primary=color(brand.get("primary")), text=color(brand.get("text")), logo=picture(brand.get("logo"), choices))
    raw = body.get("scenes")
    if not isinstance(raw, list) or len(raw) > 50:
        abort(422, "Save up to 50 scenes per event.")
    scenes = []
    seen = set()
    for scene in raw:
        if not isinstance(scene, dict):
            abort(422, "Enter valid scene details.")
        identifier, layout, background = scene.get("id"), scene.get("layout"), scene.get("background")
        if (not isinstance(identifier, str) or not ID.fullmatch(identifier) or identifier in seen
                or layout not in ("title", "screen", "inset", "side") or background not in ("solid", "gradient")):
            abort(422, "Choose a distinct scene using an available layout and background.")
        seen.add(identifier)
        clip = scene.get("clip")
        if clip is not None and (not isinstance(clip, list) or len(clip) != 2 or
                                 any(not isinstance(value, str) for value in clip) or tuple(clip) not in playable):
            abort(422, "Choose an available retained video.")
        scenes.append(dict(id=identifier, layout=layout, background=background, color=color(scene.get("color")),
                           title=text_field(scene.get("title"), "a scene title", 72, True),
                           subtitle=text_field(scene.get("subtitle", ""), "a title subtitle", 120),
                           headline=text_field(scene.get("headline", ""), "lower-third primary text", 72),
                           credit=text_field(scene.get("credit", ""), "lower-third secondary text", 90),
                           image=picture(scene.get("image"), choices), clip=clip))
    return dict(brand=brand, scenes=scenes)


def register(app, tx, staff, root, read_works, occurrence_access):
    @app.route("/api/live-events/<uid>/<int:opens>/scenes", methods=["GET", "PUT"])
    def studio_scene_deck(uid, opens):
        staff()
        with tx() as con:
            occurrence_access(con, uid, opens)
            row = con.execute("SELECT * FROM live_scene_deck WHERE event=?", (uid,)).fetchone()
            works = read_works()
            choices = images(con, works)
            revision = row["revision"] if row else None
            contents = json.loads(row["settings"]) if row else dict(brand=dict(primary="#203235", text="#ffffff", logo=None), scenes=[])
            body = request.get_json() if request.method == "PUT" else None
            submitted = body.get("scenes") if isinstance(body, dict) else []
            scene_lists = (contents["scenes"], submitted if isinstance(submitted, list) else [])
            pins = set()
            for collection in scene_lists:
                for scene in collection:
                    clip = scene.get("clip") if isinstance(scene, dict) else None
                    if isinstance(clip, list) and len(clip) == 2 and all(isinstance(value, str) for value in clip):
                        pins.add(tuple(clip))
            playable = videos(con, works, pins)
            if request.method == "PUT":
                if not isinstance(body, dict) or body.get("revision") != revision:
                    abort(409, "The studio scenes changed. Load them again before saving.")
                changed = settings(body, choices, playable)
                if changed != contents:
                    contents, revision = changed, uuid.uuid4().hex
                    con.execute("INSERT OR REPLACE INTO live_scene_deck VALUES(?,?,?)", (uid, revision, json.dumps(contents)))
            return dict(contents, revision=revision, templates=TEMPLATES,
                        images=sorted((dict(id=key, title=image["title"]) for key, image in choices.items()),
                                      key=lambda image: image["title"].casefold()),
                        videos=sorted((dict(work=key[0], cut=key[1], title=title) for key, title in playable.items()),
                                      key=lambda video: video["title"].casefold()))

    @app.get("/api/live-events/<uid>/<int:opens>/scenes/image/<asset>")
    def studio_scene_image(uid, opens, asset):
        staff()
        with tx() as con:
            occurrence_access(con, uid, opens)
            selected = images(con, (work for work in read_works() if work["id"] == asset)).get(asset)
        if not selected:
            abort(404)
        path = root / "objects" / selected["object"]
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype=MIME[selected["format"]], conditional=True)
