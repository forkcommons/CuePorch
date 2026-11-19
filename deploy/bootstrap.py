"""Provision a private PeerTube instance from the CuePorch pinned compose file."""

import hashlib
import argparse
import os
import secrets
import shutil
import subprocess
from urllib.parse import urlsplit
from pathlib import Path


def provision(directory, port, rtmp_port, video_url=None):
    canonical = urlsplit(video_url) if video_url else None
    if canonical and (
        canonical.scheme != "https"
        or not canonical.hostname
        or canonical.username
        or canonical.password
        or canonical.path not in ("", "/")
        or canonical.query
        or canonical.fragment
    ):
        raise ValueError("Supply a canonical HTTPS origin")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = directory / ".env"
    if not env.exists():
        with os.fdopen(
            os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as stream:
            name = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:12]
            stream.write(f"COMPOSE_PROJECT_NAME=cueporch-{name}\n")
            for key in ["DATABASE_PASSWORD", "INSTANCE_SECRET", "OPERATOR_PASSWORD"]:
                length = 16 if key == "OPERATOR_PASSWORD" else 32
                stream.write(f"{key}={secrets.token_hex(length)}\n")
            stream.write(f"HTTP_PORT={port}\nRTMP_PORT={rtmp_port}\n")
            if canonical:
                stream.write(
                    f"PUBLIC_HOST={canonical.hostname}\nPUBLIC_HTTPS=true\nPUBLIC_PORT={canonical.port or 443}\n"
                )
            else:
                stream.write(f"PUBLIC_PORT={port}\n")
    shutil.copyfile(Path(__file__).with_name("compose.yml"), directory / "compose.yml")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=directory, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--port", type=int, default=19000)
    parser.add_argument("--rtmp-port", type=int, default=11935)
    parser.add_argument(
        "--video-url", help="Canonical HTTPS origin for an institution-owned video host"
    )
    args = parser.parse_args()
    provision(args.directory, args.port, args.rtmp_port, args.video_url)
