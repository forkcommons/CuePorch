"""Work version selection keeps the work, public route and earlier viewing copies."""

import os
import tempfile
from pathlib import Path

from flask import abort, request

from catalog import VideoReplacement, update_video_version, video_version_key
from media_tools import prepare_screening


def register(app, tx, staff, get_work, root, remember_work):
    @app.get("/api/library/work/<uid>/versions")
    def video_versions(uid):
        staff()
        with tx() as con:
            work = get_work(con, uid)
        versions = []
        seen = set()
        for index, version in enumerate(
            [work, *reversed(work.get("versions_history", []))]
        ):
            key = video_version_key(version)
            if key not in seen:
                seen.add(key)
                versions.append(
                    {
                        "key": key,
                        "current": index == 0,
                        "saved_at": version.get("version_saved_at"),
                        "duration_seconds": version.get("duration_seconds"),
                        "video_files": [
                            f
                            for f in version["files"]
                            if f["role"] in ("playback", "original")
                        ],
                    }
                )
        return versions

    def apply(uid, previous, guard=None, **edit):
        with tx() as con:
            if guard is not None:
                guard(con)
            get_work(con, uid)
            try:
                updated = update_video_version(root, uid, expected=previous, **edit)
            except ValueError as error:
                abort(409, str(error))
            remember_work(updated)
        return {"current": video_version_key(updated)}

    def replace(uid, guard=None):
        request.max_content_length = int(
            os.environ.get("CUEPORCH_MAX_UPLOAD_BYTES", str(8 * 1024**3))
        )
        original = request.files.get("video")
        extension = (
            Path(original.filename).suffix.lower()
            if original and original.filename
            else ""
        )
        if extension not in (".mp4", ".mov", ".webm", ".mkv"):
            abort(415, "Choose an MP4, MOV, WebM or MKV video.")
        previous = request.form.get("current")
        with tx() as con:
            if guard is not None:
                guard(con)
            work = get_work(con, uid)
            if video_version_key(work) != previous:
                abort(409, "The video changed. Reload the current version.")
        with tempfile.TemporaryDirectory(dir=root / "objects") as tmp:
            scratch = Path(tmp)
            source = scratch / ("master" + extension)
            target = scratch / "screening.mp4"
            original.save(source)
            try:
                duration = prepare_screening(source, target)
            except ValueError as error:
                abort(422, str(error))
            return apply(uid, previous, guard, replacement=VideoReplacement(source, target, duration))

    @app.post("/api/library/work/<uid>/versions")
    def replace_work_video(uid):
        staff()
        return replace(uid)

    @app.post("/api/library/work/<uid>/versions/restore")
    def restore_video_version(uid):
        staff()
        body = request.get_json()
        if not isinstance(body, dict) or not all(
            isinstance(body.get(field), str) for field in ("current", "restore")
        ):
            abort(400, "Choose a retained version to restore.")
        return apply(uid, body["current"], restore_key=body["restore"])

    return replace
