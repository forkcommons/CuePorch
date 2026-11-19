"""Intake and portable catalog updates for direct creators and station recordings."""

import json
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock
from cueporch import checksum, dump, import_collection, work_id


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
            if prior is None:
                work["library_added_at"] = added_at
            elif "library_added_at" in prior:
                work["library_added_at"] = prior["library_added_at"]
            if prior and prior["files"] != work["files"]:
                versions = prior.get("versions_history", [])
                work["versions_history"] = versions + [
                    {"files": prior["files"], "title": prior["title"]}
                ]
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
