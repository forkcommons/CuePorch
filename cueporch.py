"""CuePorch private pilot inventory and portable media collection. Python 3.11+."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
        tmp = Path(target.name)
    try:
        tmp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def work_id(institution, provider, identity):
    if not all(
        isinstance(v, str) and v.strip() for v in (institution, provider, identity)
    ):
        raise ValueError("Institution, provider and a stable source ID are required")
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([institution, provider, identity]))
    )


def import_collection(source, destination):
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    if destination.exists():
        raise ValueError("Choose a new collection directory for each snapshot")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        root = Path(staging) / "collection"
        (root / "objects").mkdir(parents=True)
        works = []
        seen = set()
        for row in data["works"]:
            key = work_id(data["institution"], row["provider"], row["source_id"])
            if key in seen:
                raise ValueError(f"Repeated source identifier for {row['title']}")
            seen.add(key)
            record = {
                field: row[field]
                for field in ("title", "source_id", "provider", "source_url")
            }
            record.update(id=key, publication="private", files=[])
            for field in (
                "duration_seconds",
                "source_privacy",
                "credits",
                "description",
                "source_embed_policy",
                "chapters",
                "known_embed_pages",
                "related_work",
                "asset_type",
                "source_creator",
                "source_upload_sha256",
                "stock_asset",
                "source_upload_stock",
            ):
                if field in row:
                    record[field] = row[field]
            for attachment in row.get("files", []):
                item = {field: attachment[field] for field in ("role", "format")}
                if item["role"] not in (
                    "original",
                    "playback",
                    "caption",
                    "poster",
                    "caption-source",
                ):
                    raise ValueError("Unrecognized file role")
                for field in ("language", "provenance", "name", "text_kind"):
                    if field in attachment:
                        item[field] = attachment[field]
                path = source.parent / attachment["path"]
                digest = checksum(path)
                blob = root / "objects" / digest
                if not blob.exists():
                    shutil.copyfile(path, blob)
                if checksum(blob) != digest:
                    raise ValueError(f"File changed during import: {path.name}")
                item.update(sha256=digest, bytes=blob.stat().st_size)
                record["files"].append(item)
            roles = {a["role"] for a in record["files"]}
            record["disposition"] = (
                "original recovered"
                if "original" in roles
                else "playback copy only"
                if "playback" in roles
                else "owner file needed"
            )
            works.append(record)
        dump(
            root / "manifest.json",
            {"schema": 1, "institution": data["institution"], "works": works},
        )
        verify(root)
        root.rename(destination)
    return len(works)


def verify(root):
    data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if data["schema"] != 1:
        raise ValueError("Unsupported collection schema")
    ids = set()
    for work in data["works"]:
        key = work_id(data["institution"], work["provider"], work["source_id"])
        if key != work["id"] or key in ids:
            raise ValueError("Invalid or repeated work identity")
        ids.add(key)
        files = list(work["files"])
        for prior in work.get("versions_history", []):
            files.extend(prior["files"])
        for version, edit in work.get("language_edits", {}).items():
            if not re.fullmatch("[0-9a-f]{64}", version):
                raise ValueError("Invalid subtitle version")
            for entry in edit.get("tracks", []):
                if entry["role"] != "caption":
                    raise ValueError("Invalid subtitle attachment")
                files.append(entry)
            for alternate in edit.get("audio", []):
                files.extend((alternate["source"], alternate["rendition"]))
        for item in files:
            digest = item["sha256"]
            if not re.fullmatch("[0-9a-f]{64}", digest):
                raise ValueError("Invalid object identifier")
            path = root / "objects" / digest
            if path.stat().st_size != item["bytes"] or checksum(path) != digest:
                raise ValueError(f"Object verification failed for {key}")
    return len(ids)


def discover_vimeo(institution, output):
    token = os.environ.get("VIMEO_ACCESS_TOKEN")
    if not token:
        raise ValueError("Set VIMEO_ACCESS_TOKEN for the authorized Vimeo account")
    fields = "uri,name,description,duration,privacy.view,privacy.embed,link"
    url = "https://api.vimeo.com/me/videos?" + urlencode(
        {"per_page": 100, "fields": fields}
    )
    rows = {}
    visited = set()
    expected = None
    opener = build_opener(NoRedirect())
    while url:
        parts = urlsplit(url)
        if (parts.scheme, parts.netloc, parts.path) != (
            "https",
            "api.vimeo.com",
            "/me/videos",
        ) or url in visited:
            raise ValueError("Unexpected pagination URL")
        visited.add(url)
        request = Request(url, headers={"Authorization": "Bearer " + token})
        with opener.open(request, timeout=30) as response:
            page = json.load(response)
        if expected is None:
            expected = page["total"]
        if expected != page["total"]:
            raise ValueError(
                "Source library changed during discovery. Retry the inventory."
            )
        for video in page["data"]:
            match = re.fullmatch(r"/videos/([0-9]+)", video["uri"])
            if not match or match[1] in rows:
                raise ValueError("Invalid or repeated Vimeo identifier")
            vid = match[1]
            rows[vid] = dict(
                provider="vimeo",
                source_id=vid,
                title=video["name"],
                source_url="https://vimeo.com/" + vid,
                description=video.get("description"),
                duration_seconds=video["duration"],
                source_privacy=video["privacy"]["view"],
                source_embed_policy=video["privacy"]["embed"],
                files=[],
            )
        following = page["paging"]["next"]
        url = urljoin(url, following) if following else None
    if len(rows) != expected:
        raise ValueError("Inventory did not reconcile with Vimeo's total")
    dump(output, {"institution": institution, "works": list(rows.values())})
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser(
        "import-local", help="Build a new private portable snapshot"
    )
    importer.add_argument("inventory", type=Path)
    importer.add_argument("destination", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("collection", type=Path)
    discover = commands.add_parser("vimeo-inventory", help="Read-only source discovery")
    discover.add_argument("institution")
    discover.add_argument("output", type=Path)
    transfer = commands.add_parser(
        "vimeo-transfer", help="Inventory or migrate a Vimeo library privately"
    )
    transfer.add_argument("institution")
    transfer.add_argument("destination", type=Path)
    transfer.add_argument("--simulate", metavar="LOOPBACK_ORIGIN")
    transfer.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "vimeo-transfer":
        from vimeo_transfer import Source, migrate

        count = migrate(
            args.institution, args.destination, Source(args.simulate), args.dry_run
        )
    elif args.command == "verify":
        count = verify(args.collection)
    elif args.command == "import-local":
        count = import_collection(args.inventory, args.destination)
    else:
        count = discover_vimeo(args.institution, args.output)
    print(f"{args.command}: {count} works")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(str(exc))
