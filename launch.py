"""Start CuePorch in a private installation directory. Requires Python 3.11+, FFmpeg and Docker Compose."""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument("directory", type=Path)
p.add_argument("--institution", required=True)
p.add_argument("--port", type=int, default=19001)
p.add_argument("--peer-port", type=int, default=19000)
p.add_argument("--rtmp-port", type=int, default=11935)
p.add_argument(
    "--existing-peer",
    help="Use an existing host and PEERTUBE_USERNAME/PEERTUBE_PASSWORD",
)
p.add_argument("--simulate", help="Loopback source for an account-free rehearsal")
p.add_argument("--public-url", help="Institution-controlled canonical CuePorch origin")
p.add_argument(
    "--video-url", help="Canonical HTTPS origin when provisioning a PeerTube host"
)
args = p.parse_args()
home = args.directory.resolve()
home.mkdir(mode=0o700, parents=True, exist_ok=True)
if not shutil.which("ffmpeg"):
    p.error("Install FFmpeg and run this command again")
if not 0 < args.port < 65536 or args.port in (args.peer_port, args.rtmp_port):
    p.error("Choose distinct valid service ports")
config = home / "operator.json"
if config.exists():
    keys = json.loads(config.read_text())
    if keys["institution"] != args.institution:
        p.error("Choose a new directory for a different institution")
else:
    keys = dict(
        institution=args.institution,
        operator=secrets.token_urlsafe(32),
        creator=secrets.token_urlsafe(32),
        publisher=secrets.token_hex(16),
        station=secrets.token_urlsafe(32),
    )
    with os.fdopen(
        os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
    ) as f:
        json.dump(keys, f)
if "station" not in keys:
    keys["station"] = secrets.token_urlsafe(32)
    config.write_text(json.dumps(keys), encoding="utf8")
private_env = dict(
    os.environ,
    CUEPORCH_STATION_KEY=keys["station"],
    CUEPORCH_OPERATOR_KEY=keys["operator"],
    CUEPORCH_CREATOR_KEY=keys["creator"],
)
if args.public_url:
    from urllib.parse import urlsplit

    configured = urlsplit(args.public_url)
    if (
        configured.scheme != "https"
        or not configured.hostname
        or configured.path not in ("", "/")
        or configured.username
        or configured.password
        or configured.fragment
        or configured.query
    ):
        p.error("Use a canonical HTTPS institution origin")
    private_env["CUEPORCH_PUBLIC_URL"] = args.public_url.rstrip("/")
if args.existing_peer:
    if not all(os.environ.get(k) for k in ("PEERTUBE_PASSWORD", "PEERTUBE_USERNAME")):
        p.error("Set the existing host's service credentials in the environment")
    origin = args.existing_peer
else:
    host = home / "host"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "deploy" / "bootstrap.py"),
            str(host),
            "--port",
            str(args.peer_port),
            "--rtmp-port",
            str(args.rtmp_port),
        ]
        + (["--video-url", args.video_url] if args.video_url else []),
        check=True,
    )
    values = dict(
        line.split("=", 1)
        for line in (host / ".env").read_text().splitlines()
        if "=" in line
    )
    private_env.update(
        PEERTUBE_PASSWORD=values["OPERATOR_PASSWORD"], PEERTUBE_USERNAME="root"
    )
    origin = args.video_url or f"http://localhost:{values['HTTP_PORT']}"
private_env["CUEPORCH_PEERTUBE_ORIGIN"] = origin
runtime = home / "runtime"
python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
if not python.exists():
    venv.EnvBuilder(with_pip=True).create(runtime)
requirements = (ROOT / "requirements.txt").read_bytes()
marker = runtime / "cueporch-requirements"
if not marker.exists() or marker.read_bytes() != requirements:
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(ROOT / "requirements.txt"),
        ],
        check=True,
    )
    marker.write_bytes(requirements)
if not args.existing_peer:
    if "publisher" not in keys:
        keys["publisher"] = secrets.token_hex(16)
        config.write_text(json.dumps(keys), encoding="utf8")
    subprocess.run(
        [str(python), str(ROOT / "provision_publisher.py"), origin, str(config)],
        env=private_env,
        check=True,
    )
    private_env.update(
        PEERTUBE_USERNAME="cueporch_publisher", PEERTUBE_PASSWORD=keys["publisher"]
    )
collection = home / "collection"
if not (collection / "manifest.json").exists():
    (collection / "objects").mkdir(parents=True, exist_ok=True)
    with (collection / "manifest.json").open("x", encoding="utf8") as f:
        json.dump(dict(schema=1, institution=args.institution, works=[]), f)
print(f"Operator console: http://127.0.0.1:{args.port}/operator", flush=True)
print(f"Creator intake: http://127.0.0.1:{args.port}/submit", flush=True)
print(f"Credentials: {config}", flush=True)
raise SystemExit(
    subprocess.call(
        [
            str(python),
            str(ROOT / "institution.py"),
            str(collection),
            str(home / "institution.sqlite"),
            args.institution,
            "--port",
            str(args.port),
        ]
        + (["--simulate", args.simulate] if args.simulate else []),
        env=private_env,
    )
)
