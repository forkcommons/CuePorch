"""Timed chapter editing for retained video cuts."""

import hashlib
import json

from filelock import FileLock
from flask import abort, request

from catalog import cut_chapters, video_version_key
from cueporch import dump


def readable_chapters(work):
    chapters = [
        dict(title=entry["title"], timecode=int(entry["timecode"]))
        for entry in cut_chapters(work)
        if isinstance(entry, dict) and isinstance(entry.get("title"), str)
        and type(entry.get("timecode")) in (float, int)
        and 0 <= entry["timecode"] < 1_000_000_000
    ]
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


def register(app, tx, staff, get_work, title_for, root, remember_work):
    @app.route("/api/library/work/<uid>/chapters", methods=["GET", "PUT"])
    def video_chapters(uid):
        staff()
        with tx() as con:
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            if request.method == "PUT":
                request.max_content_length = 64 * 1024
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
            return dict(title=title_for(con, work), version=video_version_key(work),
                        revision=revision(work), chapters=readable_chapters(work))
