"""Drive a real-time software encoder into the isolated PeerTube RTMP listener."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from browser import chromium

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_destination import create_live
from peertube_destination import PeerTube

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("origin")
p.add_argument("output", type=Path)
p.add_argument("--rtmp-port", type=int, default=11935)
args = p.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
peer = PeerTube(
    args.origin, os.environ["PEERTUBE_USERNAME"], os.environ["PEERTUBE_PASSWORD"]
)
live = create_live(peer, "CuePorch station signal rehearsal")
if urlsplit(args.origin).hostname not in ("127.0.0.1", "localhost"):
    sys.exit("Use an isolated local station rehearsal")
base = [
    "ffmpeg",
    "-y",
    "-loglevel",
    "error",
    "-re",
    "-f",
    "lavfi",
    "-i",
    "testsrc2=size=320x180:rate=24",
    "-f",
    "lavfi",
    "-i",
    "sine=frequency=550",
    "-t",
    "60",
    "-map",
    "0:v",
    "-map",
    "1:a",
    "-c:v",
    "libx264",
    "-preset",
    "veryfast",
    "-g",
    "48",
    "-sc_threshold",
    "0",
    "-c:a",
    "aac",
]
recording = args.output / "signal.mp4"
# Use separate encoder processes so a stopped remote listener cannot cancel local recording.
local = subprocess.Popen(base + [str(recording)], stderr=subprocess.DEVNULL)
remote = subprocess.Popen(
    base
    + ["-f", "flv", "rtmp://127.0.0.1:" + str(args.rtmp_port) + "/live/" + live["key"]],
    stderr=subprocess.DEVNULL,
)
try:
    for _ in range(55):
        video = peer.call("GET", "/api/v1/videos/" + live["video"]).json()
        if video["state"]["id"] == 1 and video["streamingPlaylists"]:
            break
        if remote.poll() is not None:
            raise RuntimeError("RTMP encoding ended before live readiness")
        time.sleep(1)
    with sync_playwright() as pw:
        browser = chromium(pw)
        page = browser.new_page()
        page.goto(live["embed"], timeout=30000)
        page.get_by_role("button", name="Play Video", exact=True).click(timeout=20000)
        page.wait_for_function(
            "document.querySelector('video')?.videoWidth>0", timeout=20000
        )
        width = page.locator("video").first.evaluate("v=>v.videoWidth")
        browser.close()
    if remote.poll() is None:
        remote.terminate()
    remote.wait(timeout=10)
    code = local.wait(timeout=75)
    if code:
        raise RuntimeError("Local recording failed")
    seconds = float(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(recording),
            ],
            text=True,
        )
    )
    if seconds < 59:
        raise RuntimeError("Local recording ended prematurely")
    print(
        json.dumps(
            dict(live_video=live["video"], embedded_width=width, local_seconds=seconds),
            indent=2,
        )
    )
finally:
    for process in [remote, local]:
        if process.poll() is None:
            process.terminate()
            process.wait()
