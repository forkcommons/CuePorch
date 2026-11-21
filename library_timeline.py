"""Durable, versioned editor drafts keyed to archived source media."""

from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import re
import tempfile
import uuid

from flask import abort, request, send_file, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from catalog import video_version_key
from library_languages import tracks_for, track_language
from media_tools import video_properties
import subtitles
from team_brand_kits import written_kit
from timeline_cards import normalize_card
import timeline_stickers
import timeline_templates
import timeline_graphics


SCHEMA = """
CREATE TABLE IF NOT EXISTS video_editor_project(
 work TEXT NOT NULL, actor TEXT NOT NULL, revision TEXT NOT NULL, base TEXT NOT NULL,
 contents TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(work,actor));
"""
SIZES = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720), "4:3": (960, 720)}
DEFAULT = {"ratio": "16:9", "color": "182629", "clips": [], "music": None, "brand": None}
VIDEO_FORMATS = ("mp4", "mov", "webm", "mkv")


def find_cut(work, key):
    for cut in (work, *reversed(work.get("versions_history", []))):
        if video_version_key(cut) == key:
            return cut
    abort(409, "One of the source cuts is no longer available.")


def video_source(root, cut):
    assets = sorted(
        (file for file in cut["files"]
         if file["role"] in ("original", "playback") and file["format"] in VIDEO_FORMATS),
        key=lambda file: file["role"] != "original",
    )
    for file in assets:
        media = root / "objects" / file["sha256"]
        info = video_properties(str(media))
        if info.get("duration", 0) and info.get("width") and media.is_file():
            return media, info
    abort(422, "One of the sources has no readable video copy.")


def valid_range(source, limit):
    first, last = source.get("start"), source.get("end")
    if (type(first) is not int or type(last) is not int or first < 0
            or last - first < 100 or last > limit):
        abort(422, "Keep at least 0.1 seconds within each source clip.")
    return first, last


def normalize(raw, get_work, root, con):
    if not isinstance(raw, dict):
        abort(422, "Enter valid editor settings.")
    ratio, color, raw_clips = raw.get("ratio"), raw.get("color"), raw.get("clips")
    if (not isinstance(ratio, str) or ratio not in SIZES or not isinstance(color, str)
            or not re.fullmatch("[a-fA-F0-9]{6}", color)
            or not isinstance(raw_clips, list) or len(raw_clips) > 128):
        abort(422, "Choose a canvas, a background color and up to 128 source clips.")
    clips = []

    for item in raw_clips:
        if not isinstance(item, dict):
            abort(422, "Choose a source for each clip.")
        graphic = timeline_graphics.normalize_graphic(item.get("graphic"))
        sticker = timeline_stickers.normalize_sticker(item.get("sticker"), get_work, find_cut)
        if "card" in item:
            clip = normalize_card(item)
            if graphic is not None:
                clip["graphic"] = graphic
            if sticker is not None:
                clip["sticker"] = sticker
            clips.append(clip)
            continue
        if not isinstance(item.get("work"), str):
            abort(422, "Choose a source for each clip.")
        work = get_work(item["work"])
        if work.get("asset_type", "video") != "video":
            abort(422, "Only video works can be added to the video track.")
        source = find_cut(work, item.get("cut"))
        _, info = video_source(root, source)
        start, end = valid_range(item, round(info["duration"] * 1000))
        clip = dict(work=work["id"], cut=video_version_key(source), start=start, end=end)
        if graphic is not None:
            clip["graphic"] = graphic
        if sticker is not None:
            clip["sticker"] = sticker
        clips.append(clip)

    music = raw.get("music")
    if music is not None:
        if not isinstance(music, dict) or not isinstance(music.get("work"), str):
            abort(422, "Choose an audio work for the music track.")
        work = get_work(music["work"])
        start, gain = music.get("start"), music.get("gain")
        if (work.get("asset_type") != "audio" or type(start) is not int or start < 0
                or type(gain) is not int or not 0 <= gain <= 100
                or (work.get("duration_seconds") and start >= work["duration_seconds"] * 1000)):
            abort(422, "Choose a valid audio offset and volume.")
        music = dict(work=work["id"], cut=video_version_key(work), start=start, gain=gain)
    brand = raw.get("brand")
    if brand is not None:
        if not isinstance(brand, dict) or not all(isinstance(brand.get(key), str) for key in ("id", "revision")):
            abort(422, "Choose a saved brand kit for this timeline.")
        brand = written_kit(con, brand["id"], brand["revision"])
    return dict(ratio=ratio, color=color.lower(), clips=clips, music=music, brand=brand)


def project(con, root, work, actor):
    uid = work["id"]
    row = con.execute("SELECT * FROM video_editor_project WHERE work=? AND actor=?", (uid, actor)).fetchone()
    current = video_version_key(work)
    if row:
        job = con.execute("SELECT id,state,result,error FROM video_editor_job WHERE work=? AND actor=? AND revision=?",
                          (uid, actor, row["revision"])).fetchone()
        return dict(revision=row["revision"], base=row["base"], current=current,
                    draft=True, settings=json.loads(row["contents"]), updated=row["updated"], job=dict(job) if job else None)
    _, info = video_source(root, work)
    defaults = deepcopy(DEFAULT)
    defaults["clips"].append(dict(work=uid, cut=current, start=0, end=round(info["duration"] * 1000)))
    return dict(revision=None, base=current, current=current, draft=False, settings=defaults, updated=None)


def register(app, tx, staff, get_work, root, signing_key):
    signer = URLSafeTimedSerializer(signing_key, salt="cueporch-timeline-preview")
    timeline_templates.register_templates(app, staff)
    timeline_graphics.register_graphics(app, staff, SIZES)

    @app.post("/api/library/timeline-stickers/preview")
    def preview_timeline_sticker():
        staff()
        request.max_content_length = 4096
        body = request.get_json()
        if (not isinstance(body, dict) or not isinstance(body.get("ratio"), str)
                or body["ratio"] not in SIZES
                or not isinstance(body.get("sticker"), dict)):
            abort(422, "Choose a valid sticker and canvas.")
        with tx() as con:
            sticker = timeline_stickers.normalize_sticker(
                body["sticker"], lambda uid: get_work(con, uid), find_cut)
            work = get_work(con, sticker["work"])
            cut = find_cut(work, sticker["cut"])
            try:
                source = timeline_stickers.sticker_source(root, cut)
            except ValueError as exc:
                abort(422, str(exc))
        width, height = SIZES[body["ratio"]]
        scale = 240 / max(width, height)
        canvas240 = (round(width * scale), round(height * scale))
        with tempfile.TemporaryDirectory() as scratch:
            try:
                path, _x, _y = timeline_stickers.prepare_sticker(
                    source, Path(scratch), canvas240, sticker, preview=True)
            except ValueError as exc:
                abort(422, str(exc))
            data = Path(path).read_bytes()
        response = send_file(BytesIO(data), mimetype="image/png", max_age=0)
        response.cache_control.no_store = True
        response.cache_control.private = True
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def preview_for(con, uid, version):
        work = get_work(con, uid)
        if work.get("asset_type", "video") != "video":
            abort(404)
        cut = find_cut(work, version)
        playable = [item for item in cut["files"]
                    if item["role"] in ("original", "playback") and item["format"] in ("mp4", "webm")]
        return min(playable, key=lambda item: item["role"] != "playback") if playable else None

    @app.post("/api/library/work/<uid>/timeline/preview")
    def issue_timeline_preview(uid):
        staff()
        request.max_content_length = 4096
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get("cut"), str):
            abort(422, "Choose a retained clip.")
        with tx() as con:
            preview = preview_for(con, uid, body["cut"])
        if preview is None:
            abort(422, "A browser-playable copy is unavailable for this cut.")
        token = signer.dumps([uid, body["cut"]])
        return dict(preview=url_for("timeline_preview_media", ticket=token))

    @app.get("/editor-preview/<ticket>")
    def timeline_preview_media(ticket):
        try:
            uid, version = signer.loads(ticket, max_age=600)
        except (ValueError, BadSignature):
            abort(404)
        with tx() as con:
            preview = preview_for(con, uid, version)
        path = root / "objects" / preview["sha256"] if preview else None
        if path is None or not path.is_file():
            abort(404)
        response = send_file(path, mimetype="video/" + preview["format"], conditional=True, max_age=0)
        response.cache_control.no_store = True
        response.cache_control.private = True
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/library/work/<uid>/timeline/transcript")
    def timeline_transcript(uid):
        staff()
        with tx() as con:
            work = get_work(con, uid)
            cut = find_cut(work, request.args.get("cut"))
            choices = {track_language(track): track for track in tracks_for(work, cut, vtt_only=False)
                       if track["format"] in ("vtt", "srt")}
            language = request.args.get("language")
            if language is None:
                return dict(languages=list(choices))
            if language not in choices:
                abort(404)
            track = choices[language]
        path = root / "objects" / track["sha256"]
        try:
            cues = subtitles.parse(path.read_bytes(), track["format"])
        except (ValueError, OSError):
            abort(422, "This subtitle track could not be loaded.")
        return dict(cues=cues)

    @app.route("/api/library/work/<uid>/timeline", methods=["GET", "PUT", "DELETE"])
    def edit_project(uid):
        actor = staff()
        with tx() as con:
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            previous = project(con, root, work, actor)
            if request.method == "GET":
                return previous
            request.max_content_length = 128 * 1024
            body = request.get_json()
            if not isinstance(body, dict) or "revision" not in body or body["revision"] != previous["revision"]:
                abort(409, "A newer draft exists. Reopen the editor to recover it.")
            if request.method == "DELETE":
                con.execute("DELETE FROM video_editor_project WHERE work=? AND actor=?", (uid, actor))
                return project(con, root, work, actor)
            if body.get("base") != previous["current"]:
                abort(409, "The current cut has changed. Reopen the editor before saving.")
            settings = normalize(body.get("settings"), lambda selected: get_work(con, selected), root, con)
            revision = uuid.uuid4().hex
            updated = datetime.now(timezone.utc).isoformat()
            con.execute("""INSERT INTO video_editor_project VALUES(?,?,?,?,?,?)
                           ON CONFLICT(work,actor) DO UPDATE SET revision=excluded.revision,
                             base=excluded.base, contents=excluded.contents, updated=excluded.updated""",
                        (uid, actor, revision, previous["current"], json.dumps(settings), updated))
            return dict(revision=revision, base=previous["current"], current=previous["current"],
                        draft=True, settings=settings, updated=updated)
