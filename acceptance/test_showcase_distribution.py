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
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

from PIL import Image
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
            with patch("institution.library_worker.start", return_value=lambda: None):
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

            first = upload("Stage recording", public | dict(download=True))
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
                guest.goto(watch)
                guest.get_by_role("button", name="Stage recording").click()
                expect(guest.locator("#nowPlaying")).to_have_text("Stage recording")
                self.assertEqual(320, guest.locator(".showcase-screen video").evaluate("""
                    async node => {node.muted = true; await node.play(); node.pause(); return node.videoWidth}
                """))

                editor.get_by_label("List in Watch channels").check()
                editor.get_by_label("Channel category").fill("Arts")
                with page.expect_response(lambda response: response.url == base + endpoint
                                          and response.request.method == "PUT") as saved_channel:
                    editor.get_by_role("button", name="Save showcase").click()
                self.assertTrue(saved_channel.value.ok)
                current = send("GET", endpoint)
                channel = current["channel"]
                self.assertEqual("/channels/station-arts", channel)
                expect(page.get_by_label("Channel link")).to_have_value(base + channel)
                directory = base + "/watch/channels?category=Arts"
                guest.goto(base + "/watch/channels")
                guest.get_by_role("navigation", name="Channel categories").get_by_role("link", name="Arts").click()
                guest.get_by_role("link", name="Station arts", exact=True).click()
                expect(guest).to_have_url(base + channel)
                expect(guest.locator("#nowPlaying")).to_have_text("Artist interview")
                guest.get_by_role("button", name="Stage recording").click()
                self.assertEqual(320, guest.locator(".showcase-screen video").evaluate(
                    "async video => {video.muted = true; await video.play(); video.pause(); return video.videoWidth}"
                ))
                expect(guest.get_by_text("Private assembly", exact=True)).to_have_count(0)
                marker = {"X-CuePorch-Viewer": "1"}
                self.assertTrue(guest.context.request.post(base + "/api/viewer/signup", headers=marker, data={
                    "name": "Channel viewer", "email": "channel-viewer@example.invalid",
                    "password": "Channel fixture password 2026!",
                }).ok)
                self.assertTrue(guest.context.request.put(base + "/api/viewer/preferences", headers=marker,
                                                         data={"mode": "hide_mature"}).ok)
                second_details = f"/api/library/work/{second}/details"
                original_details = send("GET", second_details)
                send("PUT", second_details, original_details | {"rating": "mature", "content": ["language"]})
                guest.reload()
                expect(guest.get_by_role("button", name="Artist interview")).to_have_count(0)
                expect(guest.get_by_role("button", name="Stage recording")).to_be_visible()
                send("PUT", second_details, original_details)
                send("PUT", endpoint, current | {"settings": current["settings"] | {"visibility": "private"}})
                self.assertEqual(404, requests.get(base + channel, timeout=10).status_code)
                guest.goto(directory)
                expect(guest.get_by_role("link", name="Station arts", exact=True)).to_have_count(0)
                current = send("GET", endpoint)
                send("PUT", endpoint, current | {"settings": current["settings"] | {"visibility": "public"}})
                page.reload()
                editor = page.locator("#showcaseEdit")
                expect(editor).to_be_visible()

                backdrop = root / "exhibition.png"
                Image.new("RGB", (89, 54), "#03655d").save(backdrop)
                editor.get_by_label("Featured background image").set_input_files(backdrop)
                editor.get_by_label("Featured title").fill("Opening night")
                editor.get_by_label("Featured text alignment").select_option("center")
                editor.get_by_label("Show CuePorch brand").check()
                editor.get_by_label("Show Watch navigation").check()
                editor.get_by_label("Gallery grid contents").select_option("players")
                editor.get_by_label("Playlist card size").select_option("extra-large")
                editor.get_by_label("Play the next video").check()
                editor.get_by_label("Loop the playlist").check()
                editor.get_by_label("Offer downloads where the video allows them").check()
                editor.get_by_role("button", name="Save showcase").click()
                expect(editor.get_by_label("Featured background image")).to_have_value("")
                page.get_by_label("Preview width").select_option("mobile")
                page.get_by_role("button", name="Preview saved showcase").click()
                preview = page.frame_locator("#showcaseFrame")
                featured = preview.get_by_role("region", name="Featured information")
                expect(featured.get_by_role("heading", name="Opening night")).to_be_visible()
                expect(featured).to_have_css("text-align", "center")
                image = featured.locator("img")
                self.assertEqual(89, image.evaluate("async image => {await image.decode(); return image.naturalWidth}"))
                expect(preview.locator("body")).to_have_attribute("data-size", "extra-large")
                expect(preview.get_by_text("Hosted on CuePorch")).to_be_visible()
                expect(preview.get_by_role("link", name="Browse Watch")).to_have_attribute("href", "/watch")
                guest.goto(watch)
                guest.locator(".showcase-playlist video").first.wait_for()
                expect(guest.locator(".showcase-playlist video[src]")).to_have_count(3)
                registration_card = guest.locator(".showcase-playlist li", has_text="Registrants-only screening")
                expect(registration_card.locator("video")).to_have_count(1)
                self.assertIsNone(registration_card.locator("video").get_attribute("src"))
                expect(registration_card.locator("video")).to_be_hidden()
                expect(registration_card.get_by_role("button", name="Register to watch")).to_be_visible()
                expect(guest.locator(".showcase-playlist")).not_to_contain_text("Private assembly")
                expect(guest.locator(".showcase-screen video")).to_have_count(0)
                guest.get_by_label("Search videos").fill("stage")
                expect(guest.locator(".showcase-playlist li:visible")).to_have_count(1)
                guest.get_by_label("Search videos").fill("")
                stage = guest.locator(".showcase-playlist video[aria-labelledby=showcaseCardTitle2]")
                stage.evaluate("""async node => {node.muted = true; await node.play(); node.pause()}""")
                expect(guest.locator("#nowPlaying")).to_have_text("Stage recording")
                expect(guest.locator("#showcasePlayingDescription")).to_have_text("Stage performance")
                self.assertEqual(320, stage.evaluate("node => node.videoWidth"))
                attachment = guest.get_by_role("link", name="Download video")
                expect(attachment).to_be_visible()
                download_url = base + attachment.get_attribute("href")
                downloaded = requests.get(download_url, timeout=10)
                self.assertIn("attachment;", downloaded.headers["Content-Disposition"])
                self.assertEqual(downloaded.content, requests.get(movie["url"], timeout=10).content)
                guest.context.grant_permissions(["clipboard-read", "clipboard-write"])
                guest.get_by_role("button", name="Copy link to this video").click()
                expect(guest.locator("#showcaseCopyVideoStatus")).to_have_text("Link to this video copied.")
                shared_url = guest.evaluate("navigator.clipboard.readText()")
                self.assertEqual(watch + "?video=" + first, shared_url)
                chosen = browser.new_page()
                chosen.goto(shared_url)
                expect(chosen.locator("#nowPlaying")).to_have_text("Stage recording")
                chosen.close()
                stage.evaluate("async node => {node.currentTime = node.duration - .1; await node.play()}")
                expect(guest.locator("#nowPlaying")).to_have_text("Registrants-only screening")
                expect(guest.get_by_role("region", name="Video registration")).to_be_visible()
                expect(attachment).to_be_hidden()
                expect(registration_card.locator("video[src]")).to_have_count(0)
                guest.get_by_role("button", name="Dismiss registration").click()
                second_player = guest.locator(".showcase-playlist video[src]").first
                second_player.evaluate("async node => {node.muted = true; await node.play()}")
                expect(guest.locator("#nowPlaying")).to_have_text("Artist interview")
                disallowed = requests.get(base + second_player.get_attribute("src") + "?download=1", timeout=10)
                self.assertEqual(403, disallowed.status_code)

                send("PUT", f"/api/library/work/{first}/privacy", private)
                self.assertEqual([second], [item["id"] for item in requests.get(feed_url, timeout=10).json()["shortFormVideos"]])
                for url in (movie["url"], cover):
                    self.assertEqual(404, requests.get(url, timeout=10).status_code)
                self.assertEqual(404, requests.get(download_url, timeout=10).status_code)
                guest.goto(shared_url)
                expect(guest.locator("#nowPlaying")).to_have_text("Artist interview")

                page.get_by_role("button", name="Registration", exact=True).click()
                attendee = page.locator("#registrationEditor")
                attendee.get_by_label("Require registration before playback").check()
                attendee.get_by_role("button", name="Save registration").click()
                expect(feed).to_be_hidden()
                self.assertEqual(404, requests.get(feed_url, timeout=10).status_code)
                guest.goto(watch)
                expect(guest.locator(".showcase-feature-image")).to_have_count(0)
                expect(guest.locator(".showcase-playlist video")).to_have_count(0)
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

                reporting = send("POST", "/api/showcases")
                destination = f"/api/showcases/{reporting['id']}"
                reporting = send("PUT", destination, dict(slug="station-reporting", revision=reporting["revision"],
                    settings=reporting["settings"] | dict(visibility="public", videos=[second, restricted])))
                watch_report = base + reporting["watch"]
                guest.goto(watch_report)
                observations = [base + item["analytics"] for item in json.loads(
                    guest.locator("#showcaseContents").text_content())]

                def played_to_end(response):
                    return ("/showcase-observation/" in response.url and response.status == 204
                            and response.request.post_data_json.get("completed") is True)

                video = guest.locator(".showcase-screen video")
                with guest.expect_response(lambda response: response.url == observations[0] and played_to_end(response)):
                    self.assertEqual(320, video.evaluate("async node => {node.muted = true; await node.play();return node.videoWidth}"))
                with guest.expect_response(lambda response: response.url == observations[1] and played_to_end(response)):
                    guest.get_by_role("button", name="Venue-only cut").click()
                with guest.expect_response(lambda response: response.url == observations[0] and played_to_end(response)):
                    guest.get_by_role("button", name="Artist interview").click()
                expect(guest.locator("#nowPlaying")).to_have_text("Artist interview")

                with browser.new_context(extra_http_headers={"Sec-GPC": "1"}) as opted_out:
                    visitor = opted_out.new_page()
                    visitor.goto(watch_report)
                    self.assertTrue(all(row["analytics"] is None for row in json.loads(
                        visitor.locator("#showcaseContents").text_content())))
                    self.assertEqual(320, visitor.locator(".showcase-screen video").evaluate(
                        "async node => {node.muted=true;await node.play();node.pause();return node.videoWidth}"))
                totals = send("GET", destination + "/analytics")["totals"]
                self.assertEqual((2, 2, 2), (totals["impressions"], totals["views"], totals["finishes"]))
                self.assertGreaterEqual(totals["watched_seconds"], 1.9)
                self.assertEqual(403, requests.get(base + destination + "/analytics", timeout=10).status_code)
                page.goto(base + "/operator/showcases?select=" + reporting["id"])
                page.get_by_role("button", name="Analytics", exact=True).click()
                expect(page.locator("#showcaseReportTotals")).to_contain_text("2 impressions · 2 plays · 2 finishes")
                with page.expect_download() as csv_download:
                    page.get_by_role("button", name="Download analytics CSV").click()
                self.assertIn(",2,2,2,", Path(csv_download.value.path()).read_text(encoding="utf-8"))
                send("PUT", destination, reporting | {"settings": reporting["settings"] | {"visibility": "private"}})
                self.assertEqual(404, requests.post(observations[0], timeout=10, json={}).status_code)


if __name__ == "__main__":
    unittest.main()
