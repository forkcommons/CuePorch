"""Manage alternate audio attached to an immutable library video cut."""

import os
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory

from flask import abort, request, send_file

from catalog import video_version_key
from cueporch import checksum
from library_languages import language_tag, update
from media_tools import LOCAL_FFMPEG, run_media, video_properties


KINDS = {"dubbed": "Dubbed audio", "description": "Audio description", "commentary": "Commentary"}
FORMATS = {"wav": "wav", "aac": "aac", "m4a": "mov", "mp4": "mov", "mp3": "mp3",
           "flac": "flac", "ogg": "ogg"}


def tracks_for(work):
    return work.get("language_edits", {}).get(video_version_key(work), {}).get("audio", [])


def get_track(tracks, language, kind):
    return next((track for track in tracks if (track["language"], track["kind"]) == (language, kind)), None)


def render_audio(video, alternate, target, decoder):
    duration = video_properties(video).get("duration")
    if not duration:
        raise ValueError("Unable to determine the playback duration.")
    audio = f"asetpts=N/SR/TB,apad=whole_dur={duration:.3f},atrim=duration={duration:.3f}"
    result = run_media([
        *LOCAL_FFMPEG, "-v", "error", "-xerror", "-i", str(video),
        "-f", decoder, "-i", str(alternate), "-map", "0:V:0", "-map", "1:a:0",
        "-map_metadata", "-1", "-map_chapters", "-1", "-c:v", "copy",
        "-af", audio, "-c:a", "aac", "-ac", "2", "-b:a", "192k", "-movflags", "+faststart", str(target),
    ], timeout=3600)
    if result.returncode or not target.is_file() or not target.stat().st_size:
        raise ValueError("Unable to prepare the alternate audio.")


def object_info(path, format):
    return dict(sha256=checksum(path), bytes=path.stat().st_size, format=format)


def store(root, path, entry):
    target = root / "objects" / entry["sha256"]
    if not target.is_file() or target.stat().st_size != entry["bytes"] or checksum(target) != entry["sha256"]:
        path.replace(target)


def summary(work):
    return dict(version=video_version_key(work), tracks=[
        dict(language=track["language"], kind=track["kind"], digest=track["rendition"]["sha256"],
             revision=track["source"]["sha256"], format=track["source"]["format"])
        for track in tracks_for(work)
    ])


def track_kind(value):
    if not isinstance(value, str) or value not in KINDS:
        abort(400, "Choose dubbed audio, audio description, or commentary.")
    return value


def ensure_revision(existing, expected):
    current = existing["source"]["sha256"] if existing else ""
    if current != expected:
        abort(409, "This audio track changed. Open Languages again.")


def register(app, tx, staff, get_work, root, remember_work, playback):
    def video_work(con, uid):
        work = get_work(con, uid)
        if work.get("asset_type", "video") != "video" or not playback(work):
            abort(404, "This video has no local playback copy.")
        return work

    @app.route("/api/library/work/<uid>/audio-tracks", methods=("GET", "POST"))
    def work_audio(uid):
        staff()
        if request.method in ("GET", "HEAD"):
            with tx() as con:
                work = get_work(con, uid)
                return summary(work) | {"available": work.get("asset_type", "video") == "video" and bool(playback(work))}
        request.max_content_length = int(os.environ.get("CUEPORCH_MAX_UPLOAD_BYTES", str(8 * 1024**3)))
        upload = request.files.get("track")
        format = Path(upload.filename or "").suffix.lower()[1:] if upload else ""
        if format not in FORMATS:
            abort(415, "Choose WAV, M4A, AAC, MP3, MP4, FLAC, or Ogg.")
        language, kind = language_tag(request.form.get("language")), track_kind(request.form.get("kind"))
        version, expected = request.form.get("version"), request.form.get("revision", "")
        with TemporaryDirectory(dir=root / "objects") as directory:
            source, target = Path(directory) / ("source." + format), Path(directory) / "playback.mp4"
            upload.save(source)
            source_info = object_info(source, format)
            if not source_info["bytes"]:
                abort(422, "The audio file is empty.")
            with tx() as con:
                work = video_work(con, uid)
                if video_version_key(work) != version:
                    abort(409, "The current cut has changed. Open Languages again.")
                existing = get_track(tracks_for(work), language, kind)
                if existing and existing["source"] == source_info:
                    return summary(work)
                ensure_revision(existing, expected)
                original = root / "objects" / playback(work)["sha256"]
            try:
                render_audio(original, source, target, FORMATS[format])
            except (OSError, ValueError, TimeoutExpired):
                abort(422, "CuePorch could not decode the audio file for this video.")
            rendition = object_info(target, "mp4")

            def add(current, edit):
                existing = get_track(tracks_for(current), language, kind)
                if existing and existing["source"] == source_info:
                    return
                ensure_revision(existing, expected)
                store(root, source, source_info)
                store(root, target, rendition)
                tracks = [track for track in edit.get("audio", []) if track != existing]
                edit["audio"] = tracks + [dict(language=language, kind=kind, source=source_info, rendition=rendition)]

            with tx() as con:
                video_work(con, uid)
                work = update(root, uid, version, add)
                remember_work(work)
        return summary(work)

    @app.route("/api/library/work/<uid>/audio-tracks/<kind>/<language>", methods=("GET", "DELETE"))
    def work_audio_track(uid, kind, language):
        staff()
        kind, language = track_kind(kind), language_tag(language)
        with tx() as con:
            work = video_work(con, uid)
            track = get_track(tracks_for(work), language, kind)
            if track is None:
                abort(404)
            if request.method in ("GET", "HEAD"):
                item = track["source"]
                response = send_file(root / "objects" / item["sha256"], as_attachment=True,
                                     mimetype="application/octet-stream", conditional=True,
                                     download_name=f"{language}-{kind}.{item['format']}")
                response.headers["X-Content-Type-Options"] = "nosniff"
                return response
            body = request.get_json()
            if not isinstance(body, dict):
                abort(400)

            def remove(current, edit):
                current_track = get_track(tracks_for(current), language, kind)
                if current_track is None:
                    abort(404)
                ensure_revision(current_track, body.get("revision"))
                edit["audio"] = [item for item in edit.get("audio", []) if item != current_track]

            work = update(root, uid, body.get("version"), remove)
            remember_work(work)
            return summary(work)
