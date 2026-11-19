"""Exercise upload, caption correction and embedded playback on a real private pilot host."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from browser import chromium

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cueporch import import_collection
from peertube_destination import PeerTube

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("origin")
parser.add_argument("workspace", type=Path)
args = parser.parse_args()
root = args.workspace
root.mkdir(parents=True, exist_ok=True)
start = time.monotonic()
collection = root / "collection"
vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nRehearsal captions\n"
if not collection.exists():
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=24:d=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=500:duration=5",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            str(root / "meeting.mp4"),
        ],
        check=True,
    )
    (root / "captions.vtt").write_text(vtt, encoding="utf8")
    inventory = dict(
        institution="CuePorch station rehearsal",
        works=[
            dict(
                title="Council meeting rehearsal",
                source_id="rehearsal-1",
                provider="civiccast",
                source_url="",
                source_privacy="public",
                source_embed_policy="anywhere",
                files=[
                    dict(path="meeting.mp4", role="original", format="mp4"),
                    dict(
                        path="captions.vtt", role="caption", format="vtt", language="en"
                    ),
                ],
            )
        ],
    )
    (root / "input.json").write_text(json.dumps(inventory))
    import_collection(root / "input.json", collection)
work = json.loads((collection / "manifest.json").read_text())["works"][0]
peer = PeerTube(
    args.origin, os.environ["PEERTUBE_USERNAME"], os.environ["PEERTUBE_PASSWORD"]
)
receipt = root / "receipt.json"
video = peer.publish(collection, work, receipt, True)
revised = root / "corrected"
if not revised.exists():
    intake = json.loads((root / "input.json").read_text())
    (root / "correction.vtt").write_text(
        vtt.replace("Rehearsal captions", "Corrected caption"), encoding="utf8"
    )
    intake["works"][0]["files"][1]["path"] = "correction.vtt"
    intake["works"][0]["chapters"] = [{"title": "Call to order", "timecode": 0}]
    (root / "correction.json").write_text(json.dumps(intake))
    import_collection(root / "correction.json", revised)
corrected = json.loads((revised / "manifest.json").read_text())["works"][0]
assert video == peer.publish(revised, corrected, receipt, True)
assert (
    peer.call("GET", "/api/v1/videos/" + video + "/chapters").json()["chapters"][0][
        "title"
    ]
    == "Call to order"
)
with sync_playwright() as pw:
    browser = chromium(pw)
    page = browser.new_page()
    page.goto(args.origin + "/videos/embed/" + video, timeout=120000)
    player = page.locator("video").first
    page.get_by_role("button", name="Play Video", exact=True).click(timeout=60000)
    page.wait_for_function(
        "document.querySelector('video')?.currentTime>0", timeout=60000
    )
    outcome = player.evaluate("v=>({width:v.videoWidth,duration:v.duration})")
    assert outcome["width"] > 0
    captions = peer.call("GET", "/api/v1/videos/" + video + "/captions").json()
    assert any(c["language"]["id"] == "en" for c in captions["data"])
    browser.close()
print(
    json.dumps(
        dict(
            video=video,
            playback=outcome,
            elapsed_seconds=round(time.monotonic() - start, 2),
        ),
        indent=2,
    )
)
