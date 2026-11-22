"""Exercise preserved Vimeo cuts in migration, review, and restore."""

import json
import secrets
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from cueporch import work_id
from demo_source import build_server
from installed_audio_tracks import SAMPLE_AUDIO
from institution import create_app
from test_drive_import import QuietHandler, service_worker


class VimeoVersionMigration(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(TemporaryDirectory()))
        home = self.temp / "collection"
        (home / "objects").mkdir(parents=True)
        (home / "manifest.json").write_text(
            json.dumps(dict(institution="station", schema=1, works=[])), encoding="utf8",
        )
        source = self.enterContext(build_server(self.temp / "vimeo", version_fixture=True, audio_fixture=True))
        self.requests = Counter()
        self.denied = set()
        handler = source.RequestHandlerClass
        original_get = handler.do_GET

        def counters(request):
            path = urlsplit(request.path).path
            self.requests[path] += 1
            if path in self.denied:
                return request.send_error(403)
            return original_get(request)

        self.enterContext(patch.object(handler, "do_GET", counters))
        self.enterContext(service_worker(source))
        self.operator = secrets.token_urlsafe(32)
        self.auth = {"Authorization": "Bearer " + self.operator}
        self.enterContext(patch.dict("os.environ", {"CUEPORCH_PUBLIC_URL": "", "CUEPORCH_PEERTUBE_ORIGIN": ""}))
        with patch("institution.library_worker.start", return_value=lambda: None):
            app = create_app(home, self.temp / "workspace.sqlite", self.operator, "station",
                             simulation=f"http://127.0.0.1:{source.server_port}")
        for name, stop in app.extensions.items():
            if name.startswith("stop_") and callable(stop):
                self.addCleanup(stop)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        self.enterContext(service_worker(server))
        self.client = app.test_client()
        self.base = f"http://127.0.0.1:{server.server_port}"

    def test_private_version_transfer_recovers_earlier_cuts(self):
        uid = work_id("station", "vimeo", "71")
        work = f"/api/library/work/{uid}"
        earlier_mp4, earlier_webm = "/videos/71/versions/202", "/videos/71/versions/200"
        external_media = ("/media/previous.mp4", "/media/previous.webm")

        def versions():
            response = self.client.get(work + "/versions", headers=self.auth)
            self.assertEqual(200, response.status_code, response.get_data(as_text=True))
            return response.get_json()

        def review(allow_versions):
            response = self.client.post(work + "/review-links", headers=self.auth, json={
                "name": "Migrated previous cuts", "allow_versions": allow_versions,
            })
            self.assertEqual(201, response.status_code, response.get_data(as_text=True))
            return self.base + response.get_json()["path"]

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.get_by_role("button", name="Preview transfer").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            previous = page.get_by_text("3 previous cuts", exact=False)
            previous.click()
            expect(page.get_by_text("Vimeo version 2 · Available to transfer")).to_be_visible()
            expect(page.get_by_text("Vimeo version 3 · Owner file needed", exact=False)).to_be_visible()
            self.assertEqual([0, 0], [self.requests[path] for path in external_media])

            transfer = page.get_by_role("button", name="Transfer privately")
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=18000)
            before = versions()
            self.assertEqual(3, len(before))
            current = before[0]
            self.assertEqual((True, "/videos/71/versions/203"), (current["current"], current["source_version"]))
            old = {item["source_version"]: item for item in before[1:]}
            self.assertEqual({earlier_mp4, earlier_webm}, set(old))
            self.assertEqual(["mp4", "webm"], [file["format"] for file in old[earlier_webm]["video_files"]])
            mp4_key = old[earlier_mp4]["key"]

            viewer = browser.new_page()
            self.assertEqual(404, viewer.request.get(self.base + f"/watch/work/{uid}").status)
            self.assertEqual(404, viewer.request.get(review(False) + "?version=" + mp4_key).status)
            allowed = review(True)
            for source_uri, dimensions in ((earlier_mp4, [256, 144]), (earlier_webm, [192, 108])):
                viewer.goto(allowed + "?version=" + old[source_uri]["key"])
                video = viewer.locator("#reviewTourPlayer")
                expect(video).to_be_visible()
                video.evaluate("async video => { video.muted = true; await video.play(); }")
                viewer.wait_for_function("""([width, height]) => {
                    const video = document.querySelector('#reviewTourPlayer');
                    return video.videoWidth === width && video.videoHeight === height && video.currentTime > .2;
                }""", arg=dimensions)

            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=15000)
            self.assertEqual(before, versions())
            self.assertEqual([1, 1], [self.requests[path] for path in external_media])

            restored = self.client.post(work + "/versions/restore", headers=self.auth, json={
                "current": current["key"], "restore": mp4_key,
            })
            self.assertEqual(200, restored.status_code, restored.get_data(as_text=True))
            self.denied.add(earlier_mp4)
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=15000)
            previous.click()
            expect(page.get_by_text("Vimeo version 2 · Already in Library", exact=False)).to_be_visible()
            after = versions()
            self.assertEqual(mp4_key, after[0]["key"])
            self.assertEqual({item["key"] for item in before}, {item["key"] for item in after})
            self.assertEqual([1, 1], [self.requests[path] for path in external_media])
            for manifest in self.temp.rglob("*.json"):
                self.assertNotIn("fixture-signature", manifest.read_text(encoding="utf8"))

    def test_migration_preserves_previous_subtitles(self):
        uid = work_id("station", "vimeo", "71")
        work = f"/api/library/work/{uid}"
        earlier_mp4, earlier_webm = "/videos/71/versions/202", "/videos/71/versions/200"
        texttracks = "/videos/71/versions/202/texttracks"
        subtitle_paths = ("/media/previous-fr.vtt", "/media/previous-en.srt")

        def versions():
            response = self.client.get(work + "/versions", headers=self.auth)
            self.assertEqual(200, response.status_code, response.get_data(as_text=True))
            return response.get_json()

        def review(allow_versions):
            response = self.client.post(work + "/review-links", headers=self.auth, json={
                "name": "Migrated previous cuts", "allow_versions": allow_versions,
            })
            self.assertEqual(201, response.status_code, response.get_data(as_text=True))
            return self.base + response.get_json()["path"]

        self.denied.add(texttracks)

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.get_by_role("button", name="Preview transfer").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            previous = page.get_by_text("3 previous cuts", exact=False)
            previous.click()
            expect(page.get_by_text("Source inventory unavailable · HTTP 403", exact=True)).to_be_visible()

            transfer = page.get_by_role("button", name="Transfer privately")
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=18000)
            before = versions()
            self.assertEqual(3, len(before))
            current = before[0]
            self.assertEqual((True, "/videos/71/versions/203"), (current["current"], current["source_version"]))
            old = {item["source_version"]: item for item in before[1:]}
            self.assertEqual({earlier_mp4, earlier_webm}, set(old))
            earlier = old[earlier_mp4]["key"]

            old_review = review(True) + "?version=" + earlier
            denied_review = review(False) + "?version=" + earlier
            viewer = browser.new_page()
            self.assertEqual(404, viewer.request.get(denied_review).status)
            viewer.goto(old_review)
            expect(viewer.locator('#reviewTourPlayer track[srclang="fr"]')).to_have_count(0)

            self.denied.discard(texttracks)
            page.get_by_role("button", name="Preview transfer").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            previous.click()
            expect(page.get_by_text("fr · subtitles · Available to transfer")).to_be_visible()
            expect(page.get_by_text("de · subtitles · Disabled on Vimeo")).to_be_visible()
            expect(page.get_by_text("es · captions · Owner file needed")).to_be_visible()
            self.assertEqual([0, 0], [self.requests[path] for path in subtitle_paths])

            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=18000)
            after = versions()
            self.assertEqual(current["key"], after[0]["key"])
            after_old = {item["source_version"]: item for item in after[1:]}
            self.assertEqual(earlier, after_old[earlier_mp4]["key"])
            self.assertEqual([1, 1], [self.requests[path] for path in subtitle_paths])
            self.assertEqual(0, self.requests["/media/disabled-de.vtt"])
            self.assertEqual(404, viewer.request.get(denied_review).status)

            viewer.goto(old_review)
            video = viewer.locator("#reviewTourPlayer")
            kind = video.evaluate(
                """async (video) => {
                    const track = video.querySelector('track[srclang="fr"]');
                    track.track.mode = "showing";
                    video.muted = true;
                    await video.play();
                    return track.kind;
                }"""
            )
            self.assertEqual("subtitles", kind)
            viewer.wait_for_function(
                """() => {
                    const track = document.querySelector('#reviewTourPlayer track[srclang="fr"]');
                    const cues = (track && track.track.activeCues) || [];
                    return Array.from(cues).some(cue => cue.text.includes("Earlier cut caption"));
                }"""
            )

            resp = self.client.post(work + "/versions/restore", json={"current": current["key"], "restore": earlier}, headers=self.auth)
            self.assertEqual(resp.status_code, 200)
            resp = self.client.get(work + "/languages/fr", headers=self.auth)
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["cues"][0]["text"], "Earlier cut caption")
            english = self.client.get(work + "/languages/en", headers=self.auth)
            self.assertEqual(200, english.status_code)
            self.assertEqual("Earlier cut English", english.get_json()["cues"][0]["text"])
            data["cues"][0]["text"] = "Local French edit"
            resp = self.client.put(work + "/languages/fr", json={"version": data["version"], "revision": data["revision"], "cues": data["cues"]}, headers=self.auth)
            self.assertEqual(resp.status_code, 200)
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=18000)
            resp = self.client.get(work + "/languages/fr", headers=self.auth)
            self.assertEqual(resp.get_json()["cues"][0]["text"], "Local French edit")
            resp = self.client.get(work + "/languages/fr", headers=self.auth)
            data = resp.get_json()
            resp = self.client.delete(work + "/languages/fr", json={"version": data["version"], "revision": data["revision"]}, headers=self.auth)
            self.assertIn(resp.status_code, (200, 204))
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=18000)
            resp = self.client.get(work + "/languages/fr", headers=self.auth)
            self.assertEqual(resp.status_code, 404)
            for manifest in self.temp.rglob("*.json"):
                self.assertNotIn("fixture-signature", manifest.read_text(encoding="utf8"))

    def test_migration_preserves_version_alternate_audio(self):
        uid = work_id("station", "vimeo", "71")
        work = f"/api/library/work/{uid}"
        audio = work + "/audio-tracks"
        source_paths = ("version-203-es.wav", "version-202-en.mp3")
        password = "fixture-screening-audio"

        def get_tracks():
            response = self.client.get(audio, headers=self.auth)
            self.assertEqual(200, response.status_code)
            return response.get_json()

        def confirm_source(kind, language, filename):
            path = f"{audio}/{kind}/{language}"
            with self.client.get(path, headers=self.auth) as response:
                self.assertEqual(200, response.status_code)
                self.assertEqual((self.temp / "vimeo" / filename).read_bytes(), response.data)
            self.assertEqual(403, self.client.get(path).status_code)

        def play_track(browser, value, tone, width):
            viewer = browser.new_page()
            viewer.goto(self.base + f"/watch/work/{uid}")
            viewer.get_by_label("Video password").fill(password)
            viewer.get_by_role("button", name="Watch video").click()
            player = viewer.locator("video")
            player.click(position={"x": 12, "y": 12})
            selector = viewer.get_by_role("combobox", name="Audio track", exact=True)
            selector.select_option(value)
            expect(player).to_have_attribute("data-audio-track", value)
            self.assertAlmostEqual(tone, player.evaluate(SAMPLE_AUDIO), delta=24)
            self.assertEqual(width, player.evaluate("video => video.videoWidth"))
            viewer.close()

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.get_by_role("button", name="Preview transfer").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            results = page.get_by_text("4 audio results", exact=True)
            results.click()
            expect(page.get_by_text("Vimeo version 203 · es · dubbed · Available to transfer")).to_be_visible()
            expect(page.get_by_text("Vimeo version 203 · en · description · Owner file needed")).to_be_visible()
            expect(page.get_by_text("Vimeo version 201 · Source inventory unavailable · HTTP 403")).to_be_visible()
            self.assertEqual([0, 0], [self.requests[f"/media/{name}"] for name in source_paths])

            transfer = page.get_by_role("button", name="Transfer privately")
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=20000)
            results.click()
            expect(page.get_by_text("Vimeo version 203 · es · dubbed · Recovered")).to_be_visible()
            self.assertEqual(404, self.client.get(f"/watch/work/{uid}").status_code)
            current = get_tracks()
            self.assertEqual([("es", "dubbed")], [(item["language"], item["kind"]) for item in current["tracks"]])
            confirm_source("dubbed", "es", source_paths[0])
            published = self.client.put(work + "/source-privacy", headers=self.auth, json={"password": password})
            self.assertEqual(200, published.status_code, published.get_data(as_text=True))
            play_track(browser, "dubbed:es", 880, 320)

            versions = self.client.get(work + "/versions", headers=self.auth).get_json()
            old = next(version for version in versions if version["source_version"] == "/videos/71/versions/202")
            restored = self.client.post(work + "/versions/restore", headers=self.auth, json={
                "current": current["version"], "restore": old["key"],
            })
            self.assertEqual(200, restored.status_code, restored.get_data(as_text=True))
            summary = get_tracks()
            self.assertEqual([("en", "commentary")], [(item["language"], item["kind"]) for item in summary["tracks"]])
            confirm_source("commentary", "en", source_paths[1])
            play_track(browser, "commentary:en", 660, 256)

            removed = self.client.delete(audio + "/commentary/en", headers=self.auth, json={
                "version": old["key"], "revision": summary["tracks"][0]["revision"],
            })
            self.assertEqual(200, removed.status_code, removed.get_data(as_text=True))
            with patch("library_audio.render_audio", side_effect=AssertionError("Cached rendition was rendered again")):
                transfer.click()
                expect(page.locator("#status")).to_have_text("transferred", timeout=20000)
            self.assertEqual({"version": old["key"], "tracks": [], "available": True}, get_tracks())
            for manifest in self.temp.rglob("*.json"):
                self.assertNotIn("fixture-signature", manifest.read_text(encoding="utf8"))
