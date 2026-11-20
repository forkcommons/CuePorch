"""Ingest local assets into the existing private institutional catalog."""

import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path, PureWindowsPath

from flask import abort, request

from catalog import build_intake, merge_collection
from cueporch import checksum, work_id
from library_folder_privacy import closest as folder_privacy
from library_privacy import DEFAULTS, actor_defaults, apply_pending, apply_settings, get_privacy, requested_settings
from library_trash import hidden
from media_tools import prepare_screening
from review_settings import new_upload
from team_accounts import actor_id

IMAGE_INPUT = {"png": "png_pipe", "jpg": "jpeg_pipe", "jpeg": "jpeg_pipe", "webp": "webp_pipe"}
AUDIO_INPUT = {"mp3": "mp3", "wav": "wav", "flac": "flac", "ogg": "ogg", "m4a": "mov"}
VIDEO_INPUT = {"mp4": "mov", "mov": "mov", "webm": "matroska", "mkv": "matroska"}
DOCUMENTS = {"pdf", "docx", "txt"}


def prepare_asset(source, scratch, kind, decoder):
    if kind == "video":
        return "screening.mp4", prepare_screening(source, scratch / "screening.mp4", decoder)
    image = kind == "image"
    name = "preview.png" if image else "preview.mp3"
    options = (
        ["-map", "0:v:0", "-frames:v", "1", "-vf",
         "scale='min(1600,iw)':'min(1600,ih)':force_original_aspect_ratio=decrease",
         "-update", "1"]
        if image
        else ["-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "4"]
    )
    target = scratch / name
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
         "-f", decoder, "-i", str(source), "-map_metadata", "-1",
         *options, "-threads", "2", str(target)],
        capture_output=True,
    )
    if result.returncode or not target.is_file() or not target.stat().st_size:
        raise ValueError("Unable to decode the supplied file")
    return name, None


def register(app, tx, staff, team_contributor, action, root, database, institution, remember_work):
    def check_folder(con, folder):
        if folder and (
            hidden(con, "folder", folder)
            or not con.execute("SELECT 1 FROM library_folder WHERE id=?", (folder,)).fetchone()
        ):
            abort(404, "The selected folder no longer exists.")

    def upload_receipt(con, uid, kind):
        policy = get_privacy(con, uid) if kind == "video" else None
        pending = kind == "video" and con.execute(
            "SELECT 1 FROM library_privacy_change WHERE work=?", (uid,)
        ).fetchone() is not None
        return dict(id=uid, asset_type=kind, pending=pending,
                    visibility=json.loads(policy["settings"])["visibility"] if policy else "private")

    @app.post("/api/library/uploads")
    def upload():
        return ingest(staff())

    @app.post("/api/team/uploads")
    def contributor_upload():
        member = team_contributor()
        return ingest(actor_id(member), member["id"])

    def ingest(actor, member=None):
        request.max_content_length = int(os.environ.get("CUEPORCH_MAX_UPLOAD_BYTES", str(8 * 1024**3)))
        incoming = request.files.get("file")
        if not incoming or not incoming.filename:
            abort(400, "Choose a file to upload.")
        try:
            identity = str(uuid.UUID(request.form.get("upload_id", "")))
        except ValueError:
            abort(400, "The upload identifier is invalid.")
        if member:
            identity = str(uuid.uuid5(uuid.UUID(member), identity))
        name = PureWindowsPath(incoming.filename).name
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .")
        name_path = Path(name)
        fmt = name_path.suffix.lower().lstrip(".")
        title = name_path.stem[:128].strip()
        if not title:
            abort(400, "The file name must include a title.")
        if fmt in VIDEO_INPUT:
            kind, decoder = "video", VIDEO_INPUT[fmt]
        elif fmt in AUDIO_INPUT:
            kind, decoder = "audio", AUDIO_INPUT[fmt]
        elif fmt in IMAGE_INPUT:
            kind, decoder = "image", IMAGE_INPUT[fmt]
        elif fmt in DOCUMENTS:
            kind, decoder = "document", None
        else:
            abort(415, "Choose MP4, MOV, WebM, MKV, JPEG, PNG, WebP, MP3, WAV, FLAC, Ogg, M4A, PDF, DOCX, or TXT.")
        folder = request.form.get("folder") or None
        overrides = request.form.get("privacy") if kind == "video" else None
        if member and (folder or overrides is not None or kind != "video"):
            abort(403, "Team workspace uploads are private videos in My Library.")
        if overrides is not None:
            try:
                overrides = json.loads(overrides)
            except ValueError:
                abort(400, "Use valid upload privacy settings.")
        provider = "cueporch-upload"
        uid = work_id(institution, provider, identity)
        with tempfile.TemporaryDirectory(dir=database.parent) as tmp:
            scratch = Path(tmp)
            source = scratch / ("original." + fmt)
            incoming.save(source)
            if not source.stat().st_size:
                abort(422, "The selected file is empty.")
            digest = checksum(source)
            with tx() as con:
                if member:
                    ensure_member(con, member)
                prior, placed = prior_upload(con, uid, digest)
                if placed:
                    if member and not con.execute("SELECT 1 FROM team_owned_work WHERE work=? AND member=?",
                                                  (uid, member)).fetchone():
                        abort(409, "The upload identifier is already in use.")
                    return upload_receipt(con, uid, prior["asset_type"]), 200
                check_folder(con, folder)
                if kind == "video" and not member:
                    upload_privacy(con, actor, folder, overrides)

            files = [dict(role="original", path=source.name, format=fmt, name=name[:255])]
            duration = None
            if decoder:
                try:
                    rendition, duration = prepare_asset(source, scratch, kind, decoder)
                except ValueError:
                    abort(422, "CuePorch could not decode the media. Check the file or export another copy.")
                files.append(dict(role="playback", path=rendition, format=Path(rendition).suffix[1:]))
            staged = build_intake(
                scratch, institution, identity, title, files,
                dict(asset_type=kind, source_creator=actor, source_privacy="private",
                     source_embed_policy="private", duration_seconds=duration,
                     source_upload_sha256=digest),
                provider=provider,
            )
            with tx() as con:
                if member:
                    ensure_member(con, member)
                prior, placed = prior_upload(con, uid, digest)
                if placed:
                    if member and not con.execute("SELECT 1 FROM team_owned_work WHERE work=? AND member=?",
                                                  (uid, member)).fetchone():
                        abort(409, "The upload identifier is already in use.")
                    return upload_receipt(con, uid, prior["asset_type"]), 200
                check_folder(con, folder)
                if kind == "video":
                    choices, password = (DEFAULTS, None) if member else upload_privacy(con, actor, folder, overrides)
                work = prior or merge_collection(staged, root)[uid]
                if member and work.get("source_creator") != actor:
                    abort(409, "The upload identifier is already in use.")
                con.execute("INSERT INTO library_placement(work,folder) VALUES(?,?)", (uid, folder))
                if member:
                    con.execute("INSERT INTO team_owned_work VALUES(?,?)", (uid, member))
                if kind == "video":
                    new_upload(con, work)
                    apply_settings(con, action, work, choices, password)
                    apply_pending(con)
                receipt = upload_receipt(con, uid, kind)
            remember_work(work)
        return receipt, 201

    def ensure_member(con, uid):
        if team_contributor(con)["id"] != uid:
            abort(403)

    def upload_privacy(con, actor, folder, overrides):
        rule = folder_privacy(con, folder)
        if rule:
            return json.loads(rule["settings"]), rule["password_hash"]
        settings, password = actor_defaults(con, actor)
        return requested_settings(overrides, password) if overrides is not None else (settings, password)

    def prior_upload(con, uid, digest):
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf8"))
        prior = next((work for work in manifest["works"] if work["id"] == uid), None)
        if prior is None:
            return None, False
        if prior.get("source_upload_sha256") != digest:
            abort(409, "This upload identifier has already been used for a different file.")
        if hidden(con, "work", uid):
            abort(409, "This upload is in Recently deleted. Restore it to use it again.")
        placed = con.execute("SELECT 1 FROM library_placement WHERE work=?", (uid,)).fetchone()
        return prior, placed is not None
