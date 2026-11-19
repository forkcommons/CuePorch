"""Verify each hosted LPM work against its private source manifest without exposing playback credentials."""

import argparse, json, os, sys
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright
from browser import chromium

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peertube_destination import PeerTube

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("origin")
p.add_argument("collection", type=Path)
p.add_argument("delivery", type=Path)
a = p.parse_args()
peer = PeerTube(
    a.origin, os.environ["PEERTUBE_USERNAME"], os.environ["PEERTUBE_PASSWORD"]
)
works = {
    w["id"]: w
    for w in json.loads((a.collection / "manifest.json").read_text(encoding="utf8"))[
        "works"
    ]
}
receipt = json.loads(a.delivery.read_text(encoding="utf8"))
assert len(works) == len(receipt["works"])
results = []
with sync_playwright() as pw:
    browser = chromium(pw)
    for r in receipt["works"]:
        w = works[r["work_id"]]
        path = "/api/v1/videos/" + r["peer_id"]
        anon = requests.get(a.origin + path).status_code
        assert anon in (401, 403, 404)
        video = peer.call("GET", path).json()
        assert video["privacy"]["id"] == 3 and video["name"] == w["title"]
        page = browser.new_page()

        def authorize(route):
            if route.request.url.startswith(a.origin + "/"):
                route.continue_(
                    headers={
                        **route.request.headers,
                        "Authorization": peer.session.headers["Authorization"],
                    }
                )
            else:
                route.abort()

        page.route("**/*", authorize)
        page.goto(a.origin + "/videos/embed/" + r["peer_id"], timeout=90000)
        page.get_by_role("button", name="Play Video", exact=True).click(timeout=30000)
        page.wait_for_function(
            "document.querySelector('video')?.videoWidth>0 && document.querySelector('video').currentTime>0",
            timeout=60000,
        )
        observed = page.locator("video").first.evaluate(
            "v=>({width:v.videoWidth,duration:v.duration})"
        )
        assert (
            observed["width"] > 0
            and abs(observed["duration"] - w["duration_seconds"]) < 2
        ), (
            w["title"],
            observed,
            w["duration_seconds"],
        )
        languages = [
            c["language"]["id"]
            for c in peer.call("GET", path + "/captions").json()["data"]
        ]
        assert sorted(languages) == sorted(
            f["language"] for f in w["files"] if f["role"] == "caption"
        )
        results.append(
            dict(
                work=w["id"],
                title=w["title"],
                hosted_id=r["peer_id"],
                anonymous_status=anon,
                browser=observed,
                captions=languages,
                quality=w["disposition"],
            )
        )
        page.close()
    browser.close()
print(
    json.dumps(
        {
            "works": results,
            "bytes_in_portable_snapshot": sum(
                f["bytes"] for w in works.values() for f in w["files"]
            ),
        },
        indent=2,
    )
)
