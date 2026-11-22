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
        source = self.enterContext(build_server(self.temp / "vimeo", version_fixture=True))
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
