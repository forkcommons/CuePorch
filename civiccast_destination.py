"""CuePorch destination for CivicCast's hash-verified cable package output."""

import argparse
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import requests
from catalog import build_intake
from cueporch import checksum


def unpack(package, folder, institution, public):
    with zipfile.ZipFile(package) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("Package has duplicate entries")
        if archive.getinfo("manifest.json").file_size > 1024 * 1024:
            raise ValueError("Station manifest exceeds the transfer limit")
        if sum(i.file_size for i in archive.infolist()) > int(
            os.environ.get("CUEPORCH_MAX_UPLOAD_BYTES", str(8 * 1024**3))
        ):
            raise ValueError("Station package exceeds the unpacked transfer limit")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("profile") != "civiccast-cable-file-package-v1":
            raise ValueError("Unexpected CivicCast package profile")
        files = []
        for key, role, formats in [
            ("media", "original", {"mp4", "mov", "webm"}),
            ("captions", "caption", {"vtt", "srt"}),
        ]:
            obj = manifest[key]
            fmt = Path(obj["path"]).suffix.lower().lstrip(".")
            if fmt not in formats:
                raise ValueError("Unsupported station format")
            local = folder / (role + "." + fmt)
            with archive.open(obj["path"]) as src, local.open("wb") as dest:
                shutil.copyfileobj(src, dest, 1024 * 1024)
            if checksum(local) != obj["sha256"]:
                raise ValueError("CivicCast package checksum mismatch")
            item = dict(
                role=role, format=fmt, path=local.name, provenance="CivicCast " + key
            )
            if role == "caption":
                item.update(language="en")
                if fmt == "srt":
                    item["role"] = "caption-source"
                    files.append(item)
                    converted = folder / "broadcast.vtt"
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-loglevel",
                            "error",
                            "-i",
                            str(local),
                            str(converted),
                        ],
                        check=True,
                    )
                    item = dict(
                        role="caption",
                        format="vtt",
                        path=converted.name,
                        language="en",
                        provenance="Converted from CivicCast SRT",
                    )
            files.append(item)
    staged = build_intake(
        folder,
        institution,
        manifest["asset_id"],
        manifest["title"],
        files,
        dict(
            source_privacy="public" if public else "private",
            source_embed_policy="anywhere" if public else "private",
        ),
        "civiccast",
    )
    return staged, json.loads((staged / "manifest.json").read_text(encoding="utf8"))[
        "works"
    ][0]


def submit_package(origin, credential, path, public=False, kind="work"):
    parsed = urlsplit(origin)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
    ):
        raise ValueError("Station handoff requires HTTPS or a local pilot host")
    with path.open("rb") as stream:
        result = requests.post(
            origin.rstrip("/") + "/api/station-packages",
            data=stream,
            params={"public": "true" if public else "false", "kind": kind},
            headers={
                "Authorization": "Bearer " + credential,
                "Content-Type": "application/zip",
            },
            timeout=(15, 900),
            allow_redirects=False,
        )
    result.raise_for_status()
    return result.json()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "source", type=Path, help="CivicCast ZIP or its configured output directory"
    )
    p.add_argument("--institution-url", required=True)
    p.add_argument(
        "--public",
        action="store_true",
        help="Use only for a station output cleared for unrestricted viewing",
    )
    p.add_argument("--kind", choices=("work", "meeting"), default="work")
    p.add_argument("--watch", action="store_true")
    args = p.parse_args()
    processed = {}
    while True:
        paths = (
            sorted(args.source.glob("*-cable-package.zip"))
            if args.source.is_dir()
            else [args.source]
        )
        failed = False
        for path in paths:
            try:
                marker = (path.stat().st_mtime_ns, path.stat().st_size)
                if processed.get(path) == marker:
                    continue
                result = submit_package(
                    args.institution_url,
                    os.environ["CUEPORCH_STATION_KEY"],
                    path,
                    args.public,
                    args.kind,
                )
                processed[path] = marker
                print(json.dumps(result), flush=True)
            except (OSError, ValueError, requests.RequestException) as exc:
                failed = True
                print(
                    json.dumps(
                        {
                            "package": path.name,
                            "status": "failed",
                            "error": type(exc).__name__,
                        }
                    ),
                    flush=True,
                )
        if not args.watch:
            raise SystemExit(int(failed))
        time.sleep(5)
