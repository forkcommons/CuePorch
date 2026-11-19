"""Offline host snapshot for an operator-owned PeerTube Compose deployment (Linux)."""

import argparse
import hashlib
import json
import os
import shutil
from contextlib import closing
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cueporch import checksum, dump, verify


def compose(host, *args, **kwargs):
    return subprocess.run(["docker", "compose", *args], cwd=host, check=True, **kwargs)


def backup(host, collection, db, destination):
    if destination.exists():
        raise ValueError("Choose an unused backup path")
    verify(collection)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        output = Path(temp) / "snapshot"
        output.mkdir(mode=0o700)
        active = compose(
            host,
            "ps",
            "--services",
            "--status",
            "running",
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if "postgres" not in active or "peertube" not in active:
            raise ValueError("Start the source instance before taking its snapshot")
        compose(host, "stop", "peertube")
        try:
            with (output / "database.pgdump").open("wb") as stream:
                compose(
                    host,
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "peertube",
                    "-Fc",
                    "peertube",
                    stdout=stream,
                )
            for volume in ("data", "config"):
                with (output / (volume + ".tar")).open("wb") as stream:
                    compose(
                        host,
                        "run",
                        "--rm",
                        "--no-deps",
                        "-T",
                        "--entrypoint",
                        "tar",
                        "peertube",
                        "-C",
                        "/" + volume,
                        "-cf",
                        "-",
                        ".",
                        stdout=stream,
                    )
            shutil.copytree(collection, output / "collection")
            shutil.copyfile(host / ".env", output / ".env")
            os.chmod(output / ".env", 0o600)
            shutil.copyfile(host / "compose.yml", output / "compose.yml")
            if db:
                with (
                    closing(
                        sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
                    ) as source,
                    closing(sqlite3.connect(output / "institution.sqlite")) as target,
                ):
                    source.backup(target)
            if db:
                keyfile = db.parent / "operator.json"
                if keyfile.exists():
                    shutil.copy2(keyfile, output / "operator.json")
                    os.chmod(output / "operator.json", 0o600)
                receipts = db.parent / (db.stem + "-receipts")
                if receipts.exists():
                    shutil.copytree(
                        receipts,
                        output / "institution-receipts",
                        ignore=shutil.ignore_patterns("*.lock"),
                    )
            hashes = {
                f.relative_to(output).as_posix(): checksum(f)
                for f in output.rglob("*")
                if f.is_file()
            }
            dump(output / "snapshot.json", hashes)
            output.rename(destination)
        finally:
            compose(host, "start", "peertube")


def restore(snapshot, host, port, rtmp):
    hashes = json.loads((snapshot / "snapshot.json").read_text())
    for key, digest in hashes.items():
        item = (snapshot / key).resolve()
        if not item.is_relative_to(snapshot.resolve()) or checksum(item) != digest:
            raise ValueError("Backup verification failed")
    if host.exists():
        raise ValueError("Restore into an unused project directory")
    # Explicit unique Compose identity prevents attaching to a different deployment's volumes.
    project = (
        "cueporch-" + hashlib.sha256(str(host.resolve()).encode()).hexdigest()[:16]
    )
    prior = subprocess.check_output(
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + project,
        ],
        text=True,
    )
    if prior.strip():
        raise ValueError("Restore project already has volumes")
    host.mkdir(mode=0o700, parents=True)
    for name in (".env", "compose.yml"):
        shutil.copy2(snapshot / name, host / name)
    entries = dict(
        line.split("=", 1)
        for line in (host / ".env").read_text().splitlines()
        if "=" in line
    )
    entries.update(
        COMPOSE_PROJECT_NAME=project, HTTP_PORT=str(port), RTMP_PORT=str(rtmp)
    )
    (host / ".env").write_text("".join(f"{k}={v}\n" for k, v in entries.items()))
    shutil.copytree(snapshot / "collection", host / "collection")
    verify(host / "collection")
    if (snapshot / "institution.sqlite").exists():
        shutil.copy2(snapshot / "institution.sqlite", host / "institution.sqlite")
    if (snapshot / "operator.json").exists():
        shutil.copy2(snapshot / "operator.json", host / "operator.json")
    if (snapshot / "institution-receipts").exists():
        shutil.copytree(
            snapshot / "institution-receipts", host / "institution-receipts"
        )
    compose(host, "up", "-d", "--wait", "postgres", "redis")
    for _ in range(180):
        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "pg_isready",
                "-q",
                "-h",
                "127.0.0.1",
                "-d",
                "peertube",
                "-U",
                "peertube",
            ],
            cwd=host,
        )
        if result.returncode == 0:
            break
        time.sleep(2)
    else:
        raise RuntimeError("Destination PostgreSQL never accepted TCP connections")
    with (snapshot / "database.pgdump").open("rb") as stream:
        compose(
            host,
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "-U",
            "peertube",
            "--exit-on-error",
            "-d",
            "peertube",
            stdin=stream,
        )
    for volume in ("data", "config"):
        with (snapshot / (volume + ".tar")).open("rb") as stream:
            compose(
                host,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--entrypoint",
                "tar",
                "peertube",
                "-C",
                "/" + volume,
                "-xf",
                "-",
                stdin=stream,
            )
    compose(host, "up", "-d")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    b = sub.add_parser("backup")
    for field in ("host", "collection", "destination"):
        b.add_argument(field, type=Path)
    b.add_argument("--database", type=lambda s: Path(s).resolve())
    r = sub.add_parser("restore")
    r.add_argument("snapshot", type=Path)
    r.add_argument("host", type=Path)
    r.add_argument("--port", type=int, default=29000)
    r.add_argument("--rtmp-port", type=int, default=21935)
    args = parser.parse_args()
    if args.mode == "backup":
        backup(args.host, args.collection, args.database, args.destination)
    else:
        restore(args.snapshot, args.host, args.port, args.rtmp_port)
