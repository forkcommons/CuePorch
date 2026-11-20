"""Exercise public showcase discovery and gated TV catalog distribution."""

from contextlib import ExitStack
import json
from pathlib import Path
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.parse import urlsplit
import uuid

from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import make_server

from browser import chromium

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


class ShowcaseDistribution(unittest.TestCase):
    def test_search_and_roku_catalog(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            operator = secrets.token_urlsafe(32)
            app = create_app(root, root / "workspace.sqlite", operator, "station")
            for name, stop in app.extensions.items():
                if name.startswith("stop_"):
                    cleanup.callback(stop)
            service = make_server("127.0.0.1", 0, app)
            runner = Thread(target=service.serve_forever, daemon=True)
            runner.start()
            cleanup.callback(service.server_close)
            cleanup.callback(runner.join)
            cleanup.callback(service.shutdown)
            base = f"http://127.0.0.1:{service.server_port}"
            auth = {"Authorization": "Bearer " + operator}
            api = app.test_client()

            def send(method, path, body=None):
                response = api.open(path, method=method, headers=auth, json=body, base_url=base)
                self.assertLess(response.status_code, 300, response.get_data(as_text=True))
                return response.json

            clip = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264",
                            "-movflags", "+faststart", str(clip)], check=True, capture_output=True)

            public = dict(visibility="public", embed="anywhere", domains=[], download=False)
            private = dict(visibility="private", embed="disabled", domains=[], download=False)

            def upload(title, privacy):
                with clip.open("rb") as video:
                    response = requests.post(base + "/api/library/uploads", headers=auth, timeout=25,
                        files={"file": (title + ".mp4", video, "video/mp4")},
                        data={"upload_id": uuid.uuid4().hex, "privacy": json.dumps(privacy)})
                response.raise_for_status()
                return response.json()["id"]

            first = upload("Stage recording", public)
            second = upload("Artist interview", public)
            restricted = upload("Venue-only cut", public | dict(embed="domains", domains=["venue.example.test"]))
            registered = upload("Registrants-only screening", public)
            hidden = upload("Private assembly", private)
            protected = f"/api/library/work/{registered}/registration"
            form = send("GET", protected)
            send("PUT", protected, form | {"settings": form["settings"] | {"enabled": True}})
            details_path = f"/api/library/work/{first}/details"
            send("PUT", details_path, send("GET", details_path) | dict(description="Stage performance", tags=["arts"]))
            media = send("GET", f"/api/library/work/{first}")["files"][0]["sha256"]
            send("POST", f"/api/library/work/{first}/thumbnail", dict(seconds=0, media=media))
            showcase = send("POST", "/api/showcases")
            endpoint = f"/api/showcases/{showcase['id']}"
            showcase = send("PUT", endpoint, dict(slug="station-arts", revision=showcase["revision"],
                settings=showcase["settings"] | dict(title="Station arts", description="The station program",
                    visibility="public", videos=[second, restricted, hidden, first, registered])))
            watch = base + showcase["watch"]
            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page(viewport=dict(width=390, height=820), is_mobile=True, has_touch=True)
                page.goto(base + "/watch")
                page.evaluate("(token) => sessionStorage.setItem('cueporchOperator', token)", operator)
                page.goto(base + f"/operator/showcases?select={showcase['id']}")
                editor = page.locator("#showcaseEdit")
                expect(editor).to_be_visible()
                expect(page.locator("#showcaseSearchStatus")).to_have_text("Search indexing is off.")
                expect(page.locator("#showcaseSearchTitle")).to_have_text("Station arts · CuePorch")
                expect(page.locator("#showcaseSearchDescription")).to_have_text("The station program")
                self.assertIn("noindex", requests.get(watch, timeout=10).headers.get("X-Robots-Tag", ""))
                editor.get_by_label("Allow search engines").check()
                editor.get_by_label("Search title").fill("Community screenings")
                editor.get_by_label("Search description").fill("Independent films from the station")
                editor.get_by_label("Search keywords").fill("arts, Longmont")
                expect(page.locator("#showcaseSearchTitle")).to_have_text("Community screenings · CuePorch")
                expect(page.locator("#showcaseSearchAddress")).to_have_text(watch)
                editor.get_by_label("Offer a Roku catalog feed").check()
                editor.get_by_label("Channel provider").fill("Community Station")
                editor.get_by_label("Channel genres").fill("Documentary, Educational")
                editor.get_by_role("button", name="Save showcase").click()
                expect(page.locator("#showcaseStatus")).to_have_text("Showcase saved.")
                crawl = requests.get(watch, headers={"User-Agent": "Fixture Crawler"}, timeout=10)
                self.assertNotIn("noindex", crawl.headers.get("X-Robots-Tag", ""))
                self.assertIn('<meta name="description" content="Independent films from the station">', crawl.text)
                self.assertIn('<meta name="keywords" content="arts, Longmont">', crawl.text)

                feed = page.get_by_label("Roku catalog feed", exact=True)
                expect(feed).to_be_visible()
                feed_url = feed.input_value()
                response = requests.get(feed_url, timeout=10)
                response.raise_for_status()
                self.assertIn("no-store", response.headers["Cache-Control"])
                catalog = response.json()
                self.assertEqual("Community Station", catalog["providerName"])
                self.assertEqual("en-US", catalog["language"])
                entries = catalog["shortFormVideos"]
                self.assertEqual([second, first], [item["id"] for item in entries])
                self.assertEqual(["documentary", "educational"], entries[1]["genres"])
                self.assertEqual(["arts"], entries[1]["tags"])
                self.assertEqual("Stage performance", entries[1]["shortDescription"])
                self.assertEqual(1, entries[1]["content"]["duration"])
                movie = entries[1]["content"]["videos"][0]
                self.assertEqual("MP4", movie["videoType"])
                self.assertEqual(urlsplit(base).netloc, urlsplit(movie["url"]).netloc)
                partial = requests.get(movie["url"], headers={"Range": "bytes=0-63"}, timeout=10)
                self.assertEqual(206, partial.status_code)
                self.assertEqual(64, len(partial.content))
                guest = browser.new_page()
                guest.goto(movie["url"])
                self.assertEqual(320, guest.locator("video").evaluate("""async node => {
                    if (node.readyState < 1) await new Promise((resolve, reject) => {
                        node.addEventListener('loadedmetadata', resolve, {once: true});
                        node.addEventListener('error', reject, {once: true});
                        setTimeout(reject, 5000);
                    });
                    return node.videoWidth;
                }"""))
                cover = entries[1]["thumbnail"]
                self.assertEqual("image/jpeg", requests.get(cover, timeout=10).headers["Content-Type"])
                send("PUT", f"/api/library/work/{first}/privacy", private)
                self.assertEqual([second], [item["id"] for item in requests.get(feed_url, timeout=10).json()["shortFormVideos"]])
                for url in (movie["url"], cover):
                    self.assertEqual(404, requests.get(url, timeout=10).status_code)

                page.get_by_role("button", name="Registration", exact=True).click()
                attendee = page.locator("#registrationEditor")
                attendee.get_by_label("Require registration before playback").check()
                attendee.get_by_role("button", name="Save registration").click()
                expect(feed).to_be_hidden()
                self.assertEqual(404, requests.get(feed_url, timeout=10).status_code)
                second_media = entries[0]["content"]["videos"][0]["url"]
                self.assertEqual(404, requests.get(second_media, timeout=10).status_code)
                attendee.get_by_label("Require registration before playback").uncheck()
                attendee.get_by_role("button", name="Save registration").click()
                expect(feed).to_be_visible()
                editor.get_by_label("Visibility").select_option("private")
                expect(page.locator("#showcaseSearchStatus")).to_have_text("Indexing requires a public showcase.")
                editor.get_by_role("button", name="Save showcase").click()
                expect(feed).to_be_hidden()
                for url in (feed_url, second_media, watch):
                    self.assertEqual(404, requests.get(url, timeout=10).status_code)


if __name__ == "__main__":
    unittest.main()
