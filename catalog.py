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


VERSION_FIELDS = (
    "duration_seconds", "disposition", "chapters", "editor_job", "editor_assets", "stock_asset",
    "source_version", "source_version_number",
)


def video_version_key(work):
    # Text track classification is descriptive metadata. Keep existing cut IDs when it is imported.
    files = [
        {field: value for field, value in item.items() if field != "text_kind"}
        for item in work["files"]
        if item.get("role") != "chapter_picture"
    ]
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


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
    editor_assets: list | None = None


def update_video_version(root, uid, expected, replacement=None, restore_key=None, guard=None, *, new_work=None):
    """Commit an exact media version while retaining all previous screening objects."""
    with FileLock(str(root / ".catalog.lock"), timeout=30):
        path = root / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf8"))
        try:
            work = next(item for item in manifest["works"] if item["id"] == uid)
        except StopIteration:
            if (new_work is not None
                    and new_work.get("id") == uid
                    and replacement is not None):
                work = dict(new_work)
                work["files"] = []
                manifest["works"].append(work)
            else:
                raise
        else:
            if new_work is not None:
                raise ValueError("This project already has a saved video.")
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
            if replacement.editor_assets is not None:
                version["editor_assets"] = replacement.editor_assets
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
                if replacement.editor_assets is not None:
                    work["editor_assets"] = replacement.editor_assets
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


def merge_imported_text_tracks(edited, local):
    incoming = edited.get("tracks", [])
    if not incoming:
        return {}
    tracks = list(local.get("tracks", []))
    seen = set(local.get("vimeo_text_tracks", []))
    for t in tracks:
        if t.get("source_track"):
            seen.add(t["source_track"])
    protected = {(t.get("language") or "und").lower() for t in tracks if not t.get("source_track")}
    for track in incoming:
        st = track["source_track"]
        if st not in seen and track["language"].lower() not in protected:
            tracks.append(track)
        seen.add(st)
    return {"tracks": tracks, "vimeo_text_tracks": sorted(seen)}


def merge_imported_languages(work, prior):
    incoming = work.get("language_edits", {})
    previous = (prior or {}).get("language_edits", {})
    merged = dict(previous)
    cuts = {video_version_key(cut) for cut in [work, *work.get("versions_history", [])]}
    for key, edited in incoming.items():
        if key not in cuts:
            continue
        local = previous.get(key)
        if local is None:
            merged[key] = edited
            continue
        combined = edited | local | merge_imported_text_tracks(edited, local)
        if not edited.get("audio"):
            merged[key] = combined
            continue
        audio = list(local.get("audio", []))
        saved_slots = {tuple(pair) for pair in local.get("vimeo_audio_slots", [])}
        existing = {(track["language"], track["kind"]) for track in audio} | saved_slots
        for track in edited["audio"]:
            slot = (track["language"], track["kind"])
            if slot not in existing:
                audio.append(track)
                existing.add(slot)
        saved_slots.update(tuple(pair) for pair in edited.get("vimeo_audio_slots", []))
        merged[key] = combined | {"audio": audio, "vimeo_audio_slots": [list(pair) for pair in sorted(saved_slots)]}
    if merged:
        work["language_edits"] = merged
    elif incoming and "language_edits" in work:
        del work["language_edits"]


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
            incoming_versions = work.get("versions_history", [])
            history_files = [file for version in incoming_versions for file in version["files"]]
            audio_files = [
                track[field]
                for edit in work.get("language_edits", {}).values()
                for track in edit.get("audio", [])
                for field in ("source", "rendition")
            ]
            caption_files = [
                item
                for edit in work.get("language_edits", {}).values()
                for item in edit.get("tracks", [])
            ]
            for item in work["files"] + history_files + audio_files + caption_files:
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
            if prior and prior.get("provider") == work.get("provider") == "vimeo":
                for field in ("source_embed_appearance", "source_embed_ending", "source_watch_appearance"):
                    if field not in work and field in prior:
                        work[field] = prior[field]
            source_incomplete = work.get("transfer_issues") or work.get("disposition") == "owner file needed"
            retained_source = prior and source_incomplete and any(
                f["role"] in ("original", "playback") for f in prior["files"]
            )
            if prior and ("library_version_changed" in prior or retained_source):
                # Keep the selected local cut, or the last complete media while a new transfer needs repair.
                for key in ("files", "versions_history", "version_saved_at", "caption_status",
                            "library_version_changed", *VERSION_FIELDS):
                    work.pop(key, None)
                    if key in prior:
                        work[key] = prior[key]
            if prior is None:
                work["library_added_at"] = added_at
            elif "library_added_at" in prior:
                work["library_added_at"] = prior["library_added_at"]
            if (
                prior
                and prior.get("provider") == work.get("provider") == "vimeo"
                and any(f.get("role") in ("original", "playback") for f in prior["files"])
                and video_version_key(prior) == video_version_key(work)
                and not (prior.get("source_version") and work.get("source_version")
                         and prior["source_version"] != work["source_version"])
            ):
                unavailable = work.get("unavailable_metadata", {})
                chapters_denied = unavailable.get("/chapters") in (403, 404)
                if chapters_denied and "chapters" in prior:
                    work["chapters"] = prior["chapters"]
                eligible = {
                    ch['uri'] for ch in work.get('chapters') or []
                    if isinstance(ch, dict) and isinstance(ch.get('uri'), str)
                    and (chapters_denied or unavailable.get(
                        ch['uri'].removeprefix('/videos/' + work['source_id']) + '/pictures'
                    ) in (403, 404))
                }
                existing = {
                    f['chapter_uri'] for f in work.get('files') or []
                    if f.get('role') == 'chapter_picture' and 'chapter_uri' in f
                }
                for prior_file in prior.get('files') or []:
                    uri = prior_file.get('chapter_uri')
                    if (prior_file.get('role') == 'chapter_picture'
                            and uri in eligible and uri not in existing):
                        work.setdefault('files', []).append(prior_file)
                        existing.add(uri)
            if prior and "chapter_edits" in prior:
                work["chapter_edits"] = prior["chapter_edits"]
            versions = list(prior.get("versions_history", [])) if prior else []
            active_key = video_version_key(work)
            if prior and video_version_key(prior) != active_key:
                versions.append(version_snapshot(prior))
            if incoming_versions or versions or (prior and "versions_history" in prior):
                seen_keys = {active_key}
                seen_sources = {work.get("source_version")} - {None}
                history = []
                for version in [*versions, *incoming_versions]:
                    key = video_version_key(version)
                    identity = version.get("source_version")
                    if key in seen_keys or identity in seen_sources:
                        continue
                    seen_keys.add(key)
                    if identity is not None:
                        seen_sources.add(identity)
                    history.append(version)
                work["versions_history"] = history
            merge_imported_languages(work, prior)
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
                "stock_asset",
                "editor_assets",
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
