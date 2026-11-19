"""Measure the normal clean-install, inventory, migration and public embed path in a browser."""

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import requests
from operator_walkthrough import walkthrough
from demo_source import build_server

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument("workspace", type=Path)
p.add_argument("--peer", required=True)
args = p.parse_args()
ws = args.workspace.resolve()
ws.mkdir(parents=True, exist_ok=True)
home = ws / "installation"
if home.exists():
    p.error("Use a fresh installation directory for timing")
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
site = f"http://127.0.0.1:{port}"
with build_server(ws / "fixture") as source:
    thread = threading.Thread(target=source.serve_forever, daemon=True)
    thread.start()
    t = time.monotonic()
    with (ws / "install.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "launch.py"),
                str(home),
                "--institution",
                "Station rehearsal",
                "--existing-peer",
                args.peer,
                "--port",
                str(port),
                "--simulate",
                f"http://127.0.0.1:{source.server_port}",
            ],
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(300):
                if process.poll() is not None:
                    raise RuntimeError("Installation exited; see install.log")
                try:
                    if requests.get(site + "/operator", timeout=1).ok:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError("Operator console did not start")
            installation = time.monotonic() - t
            keys = json.loads((home / "operator.json").read_text())
            transfer, first = walkthrough(site, keys, t)
            print(
                json.dumps(
                    dict(
                        clean_install_seconds=round(installation, 2),
                        transfer_seconds=round(transfer, 2),
                        first_embed_seconds=round(first, 2),
                        inventory_items=3,
                        hosted_works=2,
                        missing_original=1,
                        input_media_bytes=(ws / "fixture" / "meeting.mp4")
                        .stat()
                        .st_size
                        * 2,
                        prior_dependency="running PeerTube 8.3.2 on same workstation",
                        browser="headless Edge",
                    ),
                    indent=2,
                )
            )
        finally:
            if process.poll() is None:
                children = psutil.Process(process.pid).children(recursive=True)
                for child in reversed(children):
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                process.terminate()
                process.wait()
            source.shutdown()
            thread.join()
