"""Intake and portable catalog updates for direct creators and station recordings."""

import hashlib
import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock
from cueporch import checksum, dump, import_collection, work_id


VERSION_FIELDS = ("duration_seconds", "disposition", "chapters", "editor_job")


def video_version_key(work):
    return hashlib.sha256(json.dumps(work["files"], sort_keys=True).encode()).hexdigest()


def version_snapshot(work):
    return {
        key: work[key]
        for key in ("files", "title", "version_saved_at", *VERSION_FIELDS)
        if key in work
    }


def cut_chapters(work):
    return work.get("chapter_edits", {}).get(video_version_key(work), work.get("chapters", []))


@dataclass
class VideoReplacement:
    original: Path
    screening: Path
    duration: float | None
    captions: tuple = ()
    chapters: list | None = None
    original_language: str | None = None
    editor_job: str | None = None


def update_video_version(root, uid, expected, replacement=None, restore_key=None, guard=None):
    """Commit an exact media version while retaining all previous screening objects."""
    with FileLock(str(root / ".catalog.lock"), timeout=30):
        path = root / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf8"))
        work = next(item for item in manifest["works"] if item["id"] == uid)
        if video_version_key(work) != expected:
            raise ValueError("The video changed. Reload and choose the version again.")
        if guard is not None:
            guard(work)
        if replacement is not None:
            files = []
            for role, source in (("original", replacement.original), ("playback", replacement.screening)):
                digest = checksum(source)
                size = source.stat().st_size
                target = root / "objects" / digest
                if not target.exists() or checksum(target) != digest:
                    source.replace(target)
                files.append({
                    "role": role,
                    "format": source.suffix.lower().lstrip("."),
                    "sha256": digest,
                    "bytes": size,
                })
            files.extend(f for f in work["files"] if f["role"] == "poster")
            files.extend(replacement.captions)
            version = {
                "files": files,
                "duration_seconds": replacement.duration,
                "disposition": "original recovered",
                "version_saved_at": datetime.now(timezone.utc).isoformat(),
            }
            if replacement.chapters is not None:
                version["chapters"] = replacement.chapters
            if replacement.editor_job is not None:
                version["editor_job"] = replacement.editor_job
        else:
            version = next(
                (v for v in reversed(work.get("versions_history", []))
                 if video_version_key(v) == restore_key),
                None,
            )
            if version is None:
                raise ValueError("That retained video version is unavailable.")
        if work["files"] == version["files"]:
            if replacement is not None and replacement.editor_job is not None:
                work["editor_job"] = replacement.editor_job
                dump(path, manifest)
            return work
        history = [
            item for item in work.get("versions_history", [])
            if video_version_key(item) != video_version_key(version)
        ]
        if any(f["role"] in ("original", "playback") for f in work["files"]):
            history.append(version_snapshot(work))
        for key in VERSION_FIELDS:
            work.pop(key, None)
            if key in version:
                work[key] = version[key]
        if "disposition" not in work:
            work["disposition"] = (
                "original recovered" if any(f["role"] == "original" for f in version["files"])
                else "playback copy only"
            )
        work["files"] = version["files"]
        if replacement is not None and replacement.original_language:
            key = video_version_key(work)
            edits = work.setdefault("language_edits", {}).setdefault(key, {})
            edits.setdefault("original_language", replacement.original_language)
        work["versions_history"] = history
        work["version_saved_at"] = version.get("version_saved_at") or datetime.now(timezone.utc).isoformat()
        work["library_version_changed"] = datetime.now(timezone.utc).isoformat()
        dump(path, manifest)
        return work


def merge_collection(source, destination):
    """Commit the source works atomically; readers always see a complete manifest."""
    with FileLock(str(destination / ".catalog.lock"), timeout=30):
        current = json.loads((destination / "manifest.json").read_text(encoding="utf8"))
        incoming = json.loads((source / "manifest.json").read_text(encoding="utf8"))
        if incoming["institution"] != current["institution"]:
            raise ValueError("Source belongs to a different institution")
        works = {w["id"]: w for w in current["works"]}
        added_at = datetime.now(timezone.utc).isoformat()
        for work in incoming["works"]:
            for item in work["files"]:
                digest = item["sha256"]
                target = destination / "objects" / digest
                if target.exists() and checksum(target) == digest:
                    continue
                with tempfile.NamedTemporaryFile(
                    dir=target.parent, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                try:
                    shutil.copyfile(source / "objects" / digest, temporary)
                    if checksum(temporary) != digest:
                        raise ValueError("Source object changed during catalog update")
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            prior = works.get(work["id"])
            if prior and "library_version_changed" in prior:
                # A repeat source import updates source metadata without replacing
                # the operator's selected cut or its timed attachments.
                for key in ("files", "versions_history", "version_saved_at",
                            "library_version_changed", *VERSION_FIELDS):
                    work.pop(key, None)
                    if key in prior:
                        work[key] = prior[key]
            if prior is None:
                work["library_added_at"] = added_at
            elif "library_added_at" in prior:
                work["library_added_at"] = prior["library_added_at"]
            for field in ("language_edits", "chapter_edits"):
                if prior and field in prior:
                    work[field] = prior[field]
            if prior and prior["files"] != work["files"]:
                versions = prior.get("versions_history", [])
                work["versions_history"] = versions + [version_snapshot(prior)]
            elif prior and "versions_history" in prior:
                work["versions_history"] = prior["versions_history"]
            works[work["id"]] = work
        current["works"] = list(works.values())
        dump(destination / "manifest.json", current)
        return works


def copy_work(collection, uid, title):
    """Make a private catalog entry reusing the immutable media objects."""
    with FileLock(str(collection / ".catalog.lock"), timeout=30):
        path = collection / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf8"))
        source = next(item for item in manifest["works"] if item["id"] == uid)
        identifier = uuid.uuid4().hex
        identity = work_id(manifest["institution"], "cueporch-copy", identifier)
        copy = {
            field: source[field]
            for field in (
                "description",
                "duration_seconds",
                "files",
                "disposition",
                "chapters",
                "credits",
            )
            if field in source
        }
        copy.update(
            id=identity,
            provider="cueporch-copy",
            source_id=identifier,
            source_url="",
            copied_from=uid,
            title=title,
            publication="private",
            source_privacy="private",
            source_embed_policy="private",
            library_added_at=datetime.now(timezone.utc).isoformat(),
        )
        key = video_version_key(source)
        for field in ("language_edits", "chapter_edits"):
            if key in source.get(field, {}):
                copy[field] = {key: source[field][key]}
        manifest["works"].append(copy)
        dump(path, manifest)
        return identity


def build_intake(
    folder, institution, identity, title, files, metadata, provider="artist"
):
    inventory = {
        "institution": institution,
        "works": [
            {
                "provider": provider,
                "source_id": identity,
                "source_url": "",
                "title": title,
                "files": files,
                **metadata,
            }
        ],
    }
    dump(folder / "input.json", inventory)
    collection = folder / "collection"
    import_collection(folder / "input.json", collection)
    return collection
