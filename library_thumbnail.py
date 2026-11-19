"""Retain and serve an operator-selected thumbnail without changing the media revision."""

import math
import os
import subprocess
import tempfile
import time
from pathlib import Path

from flask import abort, request, send_file

from cueporch import checksum

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_thumbnail(
 work TEXT PRIMARY KEY, object TEXT NOT NULL, changed REAL NOT NULL,
 synced INTEGER NOT NULL DEFAULT 0, attempted REAL NOT NULL DEFAULT 0, error TEXT);
"""


def selected_thumbnail(con, work):
    row = con.execute(
        "SELECT object FROM library_thumbnail WHERE work=?", (work["id"],)
    ).fetchone()
    if row:
        return row["object"]
    poster = next((f for f in work["files"] if f["role"] == "poster"), None)
    return poster["sha256"] if poster else None


def thumbnail_type(path):
    with path.open("rb") as stream:
        header = stream.read(16)
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    kinds = {"png": b"\x89PNG", "jpeg": b"\xff\xd8"}
    return next(("image/" + kind for kind, prefix in kinds.items() if header.startswith(prefix)), None)


def register(app, tx, staff, get_work, root):
    @app.get("/api/library/work/<uid>/thumbnail")
    def show_thumbnail(uid):
        staff()
        with tx() as con:
            work = get_work(con, uid)
            digest = selected_thumbnail(con, work)
        if not digest:
            abort(404)
        path = root / "objects" / digest
        if not path.is_file():
            abort(404)
        media_type = thumbnail_type(path)
        if not media_type:
            abort(415, "The stored thumbnail uses an unsupported format.")
        response = send_file(path, mimetype=media_type)
        response.cache_control.no_store = True
        response.cache_control.private = True
        return response

    @app.post("/api/library/work/<uid>/thumbnail")
    def set_thumbnail(uid):
        staff()
        request.max_content_length = 20 * 1024**2
        upload = request.files.get("image")
        body = request.get_json(silent=True) if upload is None else None
        source = None
        position = None
        if upload is None:
            if not isinstance(body, dict):
                abort(400, "Choose an image or a video frame.")
            position = body.get("seconds")
            if (
                isinstance(position, bool)
                or not isinstance(position, (int, float))
                or not 0 <= position <= 30 * 86400
                or not math.isfinite(position)
            ):
                abort(400, "Choose a valid timestamp.")
            with tx() as con:
                work = get_work(con, uid)
                source = next(
                    (
                        f
                        for f in work["files"]
                        if f["sha256"] == body.get("media")
                        and f["role"] in ("playback", "original")
                    ),
                    None,
                )
                if not source:
                    abort(409, "Reload the current video before choosing a frame.")
        with tempfile.TemporaryDirectory(dir=root / "objects") as tmp:
            scratch = Path(tmp)
            if upload is not None:
                incoming = scratch / "image"
                upload.save(incoming)
                with incoming.open("rb") as stream:
                    header = stream.read(16)
                if header.startswith(b"\x89PNG\r\n\x1a\n"):
                    input_args = ["-f", "png_pipe", "-i", str(incoming)]
                elif header.startswith(b"\xff\xd8"):
                    input_args = ["-f", "jpeg_pipe", "-i", str(incoming)]
                elif header[:4] == b"RIFF" and header[8:12] == b"WEBP":
                    input_args = ["-f", "webp_pipe", "-i", str(incoming)]
                else:
                    abort(415, "Choose a JPEG, PNG or WebP image.")
            else:
                input_args = [
                    "-ss",
                    f"{position:.3f}",
                    "-i",
                    str(root / "objects" / source["sha256"]),
                ]
            output = scratch / "thumbnail.jpg"
            try:
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-hide_banner",
                        "-v",
                        "error",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-threads",
                        "2",
                        *input_args,
                        "-map",
                        "0:V:0",
                        "-frames:v",
                        "1",
                        "-map_metadata",
                        "-1",
                        "-vf",
                        "scale=w='min(960,iw)':h='min(960,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                        "-q:v",
                        "3",
                        "-threads",
                        "2",
                        "-update",
                        "1",
                        str(output),
                    ],
                    capture_output=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                abort(422, "The selected thumbnail could not be decoded in time.")
            if result.returncode or not output.is_file() or not output.stat().st_size:
                abort(422, "Unable to extract a thumbnail from the selected media.")
            digest = checksum(output)
            with tx() as con:
                work = get_work(con, uid)
                if source is not None and source not in work["files"]:
                    abort(409, "The current video changed. Choose the frame again.")
                target = root / "objects" / digest
                if not target.exists():
                    os.replace(output, target)
                con.execute(
                    """INSERT INTO library_thumbnail(work,object,changed) VALUES(?,?,?)
                       ON CONFLICT(work) DO UPDATE SET object=excluded.object,
                         changed=excluded.changed,synced=0,attempted=0,error=NULL
                       WHERE library_thumbnail.object<>excluded.object""",
                    (uid, digest, time.time()),
                )
        return "", 204
