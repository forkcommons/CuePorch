"""Retain editorial trims as versions and make short animated extracts."""

from io import BytesIO
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory

from flask import abort, request, send_file

from catalog import VideoReplacement, update_video_version, video_version_key
from library_interactivity import readable_chapters
from library_languages import save_blob, summary, tracks_for
from media_tools import make_gif, prepare_screening, video_properties
from stock_assets import stock_credit, write_credit_metadata
import subtitles


def timed_attachments(work):
    return tracks_for(work, vtt_only=False), readable_chapters(work), summary(work)["original_language"]


def clip_captions(root, tracks, start_ms, end_ms):
    retained = []
    for track in tracks:
        if track["format"] not in ("vtt", "srt"):
            raise ValueError("Convert the subtitle files to WebVTT before trimming.")
        source = root / "objects" / track["sha256"]
        cues = []
        for cue in subtitles.parse(source.read_bytes(), track["format"]):
            first, last = max(start_ms, cue["start"]), min(end_ms, cue["end"])
            if first < last:
                cues.append(dict(cue, start=first - start_ms, end=last - start_ms))
        if cues:
            retained.append(dict(track, format="vtt", **save_blob(root, subtitles.serialize(cues))))
    return tuple(retained)


def clip_chapters(chapters, start_ms, end_ms):
    retained = {}
    for chapter in chapters:
        time = chapter["timecode"] * 1000
        if time >= end_ms:
            break
        # The last chapter active before the cut remains active at its opening.
        offset = time - start_ms
        second = 0 if offset <= 0 else max(1, (offset + 500) // 1000)
        if second * 1000 < end_ms - start_ms:
            retained[second] = dict(chapter, timecode=second)
    return list(retained.values())


def register(app, tx, staff, get_work, root, remember_work, title_for, video_mime):
    def selection(con, uid):
        work = get_work(con, uid)
        if work.get("asset_type", "video") != "video":
            abort(404)
        files = [item for item in work["files"]
                 if item["role"] in ("original", "playback") and item["format"] in video_mime]
        masters = sorted(files, key=lambda item: item["role"] != "original")
        for item in masters:
            source = root / "objects" / item["sha256"]
            length = video_properties(str(source)).get("duration")
            if length and length > 0:
                return work, source, round(length * 1000)
        abort(422, "A readable video with a known duration is required.")

    @app.get("/api/library/work/<uid>/editing")
    def video_edit_source(uid):
        staff()
        with tx() as con:
            work, _, duration = selection(con, uid)
            playable = [item for item in work["files"] if item["role"] in ("original", "playback")
                        and item["format"] in video_mime]
            preview = min(playable, key=lambda item: item["role"] != "playback")
            return dict(version=video_version_key(work), duration=duration, title=title_for(con, work),
                        preview=preview["sha256"], tracks=summary(work)["tracks"])

    @app.post("/api/library/work/<uid>/editing/<operation>")
    def video_edit_action(uid, operation):
        staff()
        if operation not in ("trim", "gif"):
            abort(404)
        request.max_content_length = 4096
        body = request.get_json()
        if not isinstance(body, dict):
            abort(422, "Choose the start and end of the selection.")
        start_ms, end_ms = body.get("start"), body.get("end")
        with tx() as con:
            work, source, duration = selection(con, uid)
        version = video_version_key(work)
        if body.get("version") != version:
            abort(409, "This cut has changed. Open the editor again.")
        if (type(start_ms) is not int or type(end_ms) is not int
                or not 0 <= start_ms < end_ms <= duration or end_ms - start_ms < 100):
            abort(422, "Choose at least 0.1 seconds within the video.")
        if operation == "gif" and end_ms - start_ms > 6000:
            abort(422, "A GIF can be up to six seconds long.")
        if operation == "trim" and start_ms == 0 and end_ms == duration:
            abort(422, "Choose which part of the video to keep.")
        interval = (start_ms / 1000, end_ms / 1000)
        attachments = timed_attachments(work)

        def attachments_unchanged(latest):
            if timed_attachments(latest) != attachments:
                raise ValueError("Captions or chapters changed while rendering. Open the editor again.")

        with TemporaryDirectory(dir=root / "objects") as directory:
            scratch = Path(directory)
            if operation == "gif":
                path = scratch / "excerpt.gif"
                try:
                    make_gif(source, path, *interval)
                except (ValueError, TimeoutExpired):
                    abort(422, "Unable to make an animated GIF from this selection.")
                return send_file(BytesIO(path.read_bytes()), mimetype="image/gif",
                                 as_attachment=True, download_name="cueporch-excerpt.gif", max_age=0)
            original = scratch / "cut.mp4"
            screening = scratch / "screening.mp4"
            tracks, chapters, language = attachments
            try:
                assets = list(work.get("editor_assets", []))
                credit = stock_credit(work, work)
                if credit is not None:
                    assets.append(credit)
                metadata = write_credit_metadata(scratch / "credits.ffmeta", assets)
                prepare_screening(source, original, interval=interval, full_resolution=True,
                                  credit_metadata=metadata)
                length = video_properties(str(original)).get("duration")
                if not length:
                    raise ValueError("No playable frames were rendered from this selection.")
                prepare_screening(original, screening, credit_metadata=metadata)
                captions = clip_captions(root, tracks, start_ms, end_ms)
            except (OSError, ValueError) as error:
                abort(422, f"Unable to save this selection. {error}" if isinstance(error, ValueError)
                      else "A source file could not be read while saving this selection.")
            replacement = VideoReplacement(
                original, screening, length, captions, clip_chapters(chapters, start_ms, end_ms), language,
                editor_assets=assets or None,
            )
            with tx() as con:
                get_work(con, uid)
                try:
                    updated = update_video_version(root, uid, expected=version, replacement=replacement,
                                                   guard=attachments_unchanged)
                except ValueError as error:
                    abort(409, str(error))
                remember_work(updated)
        return dict(current=video_version_key(updated), duration=round(length * 1000))
