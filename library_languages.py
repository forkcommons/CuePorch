"""Edit subtitle attachments without changing the retained cut or review history."""

import hashlib
import json
from pathlib import Path
import tempfile

from filelock import FileLock
from flask import abort, request, send_file

from catalog import video_version_key
from cueporch import checksum, dump
import subtitles


TEXT_KINDS = ("captions", "subtitles", "descriptions", "metadata")


def tracks_for(work, cut=None, version=None, vtt_only=True):
    cut = work if cut is None else cut
    key = version or video_version_key(cut)
    override = work.get("language_edits", {}).get(key, {})
    candidates = override.get("tracks", cut["files"])
    return [
        item for item in candidates
        if item["role"] == "caption" and (not vtt_only or item["format"] == "vtt")
    ]


def language_tag(value):
    if not isinstance(value, str) or not subtitles.LANGUAGE.fullmatch(value):
        abort(400, "Enter a language code such as en, es or fr-CA.")
    return value.lower()


def track_language(track):
    return (track.get("language") or "und").lower()


def track_kind(track):
    kind = track.get("text_kind")
    return kind if kind in TEXT_KINDS else "captions"


def track_key(track):
    return f"{track['sha256']}:{track_language(track)}:{track_kind(track)}"


def viewer_tracks(work):
    return [dict(track, index=index, key=track_key(track), kind=track_kind(track))
            for index, track in enumerate(tracks_for(work)) if track_kind(track) in ("captions", "subtitles")]


def original_language(work):
    changed = work.get("language_edits", {}).get(video_version_key(work), {})
    return changed.get("original_language", work.get("source_language")) or ""


def save_blob(root, content):
    digest = hashlib.sha256(content).hexdigest()
    target = root / "objects" / digest
    if not target.is_file() or checksum(target) != digest:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            staged = Path(stream.name)
            stream.write(content)
        try:
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)
    return dict(sha256=digest, bytes=len(content))


def update(root, uid, expected, mutate):
    with FileLock(str(root / ".catalog.lock"), timeout=30):
        path = root / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf8"))
        work = next((item for item in manifest["works"] if item["id"] == uid), None)
        if work is None:
            abort(404)
        revision = video_version_key(work)
        if revision != expected:
            abort(409, "The current cut has changed. Open Languages again.")
        changes = work.setdefault("language_edits", {}).setdefault(revision, {})
        mutate(work, changes)
        dump(path, manifest)
    return work


def summary(work):
    version = video_version_key(work)
    return dict(version=version, title=work["title"],
                original_language=original_language(work),
                tracks=[dict(language=track_language(item), kind=track_kind(item),
                             provenance=item.get("provenance"), bytes=item["bytes"])
                        for item in tracks_for(work, version=version)])


def editable_body():
    request.max_content_length = 12 * 1024**2
    body = request.get_json()
    if not isinstance(body, dict):
        abort(400, "Enter valid subtitle details.")
    return body


def store_track(root, work, edit, language, cues, provenance, expected_digest=None, *, kind=None):
    available = tracks_for(work, vtt_only=False)
    existing = next((item for item in available if track_language(item) == language), None)
    if expected_digest is not None and (existing is None or existing["sha256"] != expected_digest):
        abort(409, "Another edit changed this subtitle track. Open it again.")
    try:
        data = subtitles.serialize(cues)
    except ValueError as error:
        abort(400, str(error))
    updated = {"role": "caption", "format": "vtt", "language": language, "text_kind": kind or track_kind(existing or {}),
               "provenance": provenance, **save_blob(root, data)}
    edit["tracks"] = [item for item in available if track_language(item) != language] + [updated]


def register(app, tx, staff, get_work, root, remember_work, title_for, request_caption):
    def video(con, uid):
        work = get_work(con, uid)
        if work.get("asset_type", "video") != "video":
            abort(404)
        return work

    @app.route("/api/library/work/<uid>/languages", methods=["GET", "PUT", "POST"])
    def work_languages(uid):
        staff()
        with tx() as con:
            work = video(con, uid)
            if request.method == "PUT":
                body = editable_body()
                language = language_tag(body.get("language"))
                generate = body.get("generate", True)
                if type(generate) is not bool:
                    abort(400, "Choose whether to generate subtitles automatically.")
                work = update(root, uid, body.get("version"),
                              lambda _work, edit: edit.update(original_language=language))
                remember_work(work)
                if generate:
                    request_caption(con, work)
            elif request.method == "POST":
                request.max_content_length = 8 * 1024**2
                upload = request.files.get("track")
                language = language_tag(request.form.get("language"))
                kind = request.form.get("kind", "subtitles")
                if kind not in ("captions", "subtitles"):
                    abort(422, "Choose subtitles or captions.")
                format = Path(upload.filename or "").suffix.lower() if upload else ""
                if format not in (".vtt", ".srt"):
                    abort(415, "Choose a WebVTT or SRT subtitle file.")
                try:
                    cues = subtitles.parse(upload.read(), format[1:])
                except ValueError as error:
                    abort(422, str(error))
                work = update(root, uid, request.form.get("version"),
                              lambda current, edit: store_track(root, current, edit, language, cues, "Operator upload", kind=kind))
                remember_work(work)
            title = title_for(con, work)
        return summary(work) | {"title": title}

    @app.route("/api/library/work/<uid>/languages/<language>", methods=["GET", "PUT", "DELETE"])
    def subtitle_track(uid, language):
        staff()
        language = language_tag(language)
        with tx() as con:
            work = video(con, uid)
            if request.method == "GET":
                track = next((item for item in tracks_for(work) if track_language(item) == language), None)
                if track is None:
                    abort(404)
                path = root / "objects" / track["sha256"]
                if request.args.get("download") == "1":
                    return send_file(path, mimetype="text/vtt", as_attachment=True,
                                     download_name=language + ".vtt")
                try:
                    cues = subtitles.parse(path.read_bytes(), "vtt")
                except ValueError as error:
                    abort(422, str(error))
                return dict(version=video_version_key(work), revision=track["sha256"], cues=cues)
            body = editable_body()
            revision = body.get("revision")
            if not isinstance(revision, str):
                abort(400, "Open this subtitle track before saving or deleting it.")

            def mutate(current, edit):
                if request.method == "PUT":
                    store_track(root, current, edit, language, body.get("cues"), "Operator edit", revision)
                else:
                    available = tracks_for(current, vtt_only=False)
                    if not any(track_language(item) == language and item["sha256"] == revision for item in available):
                        abort(409, "Another edit changed this subtitle track. Open it again.")
                    edit["tracks"] = [item for item in available if track_language(item) != language]

            work = update(root, uid, body.get("version"), mutate)
            remember_work(work)
            title = title_for(con, work)
        return summary(work) | {"title": title}
