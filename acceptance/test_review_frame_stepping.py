import json
import secrets
import subprocess
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from institution import create_app
from test_drive_import import QuietHandler, service_worker


class ReviewFrameStepping(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        (self.root / "objects").mkdir()
        (self.root / "manifest.json").write_text(
            json.dumps(dict(institution="station", schema=1, works=[])), encoding="utf-8"
        )
        operator = secrets.token_urlsafe(32)
        self.enterContext(patch.dict("os.environ", {"CUEPORCH_PUBLIC_URL": ""}))
        with patch("institution.library_worker.start", return_value=lambda: None):
            app = create_app(self.root, self.root / "workspace.sqlite", operator, "station")
        for name, stop in app.extensions.items():
            if name.startswith("stop_") and callable(stop):
                self.addCleanup(stop)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.enterContext(service_worker(server))
        self.client = app.test_client()
        self.auth = {"Authorization": "Bearer " + operator}

    def test_vfr_review_steps_displayed_frames(self):
        clip = self.root / "Variable rate.mp4"
        choose = r"select=eq(n\,0)+eq(n\,1)+eq(n\,6)+eq(n\,7)+eq(n\,13)+eq(n\,17)"
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=2",
            "-vf", choose, "-fps_mode", "vfr", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(clip),
        ], check=True, capture_output=True, timeout=20)
        uploaded = self.client.post("/api/library/uploads", headers=self.auth, data={
            "upload_id": str(uuid4()), "file": (BytesIO(clip.read_bytes()), clip.name),
        }, content_type="multipart/form-data")
        self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
        work_id = uploaded.get_json()["id"]
        created = self.client.post(f"/api/library/work/{work_id}/review-links", headers=self.auth, json={
            "name": "VFR review", "password": "fixture-frame-password",
        })
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        path = created.get_json()["path"]
        version = self.client.get(f"/api/library/work/{work_id}/versions", headers=self.auth)
        self.assertEqual(version.status_code, 200)
        version_key = version.get_json()[0]["key"]
        index = f"{path}/frames/{version_key}"
        self.assertEqual(self.client.get(index + "?at=0").status_code, 403)

        with sync_playwright() as pw, chromium(pw) as browser:
            viewer = browser.new_page()
            self.exercise_vfr(viewer, self.base + path)
            selected = viewer.locator("#reviewTourPlayer").evaluate(
                "video => new URL(video.src).searchParams.get('rendition')"
            )
            address = self.base + index
            request = viewer.request
            frames = request.get(address + "?at=0", params={"rendition": selected} if selected else {})
            self.assertEqual(frames.status, 200, frames.text())
            self.assertIn("no-store", frames.headers["cache-control"])
            self.assertEqual(frames.json()["frames"], [0, .1, .6, .7, 1.3, 1.7])
            self.assertEqual(request.get(address + "?at=1").status, 400)
            self.assertEqual(request.get(address + "?at=0&rendition=bad").status, 404)
            self.assertEqual(request.get(f"{self.base}{path}/frames/fabricated?at=0").status, 404)
            disabled = self.client.put(
                f"/api/library/work/{work_id}/review-links/{created.get_json()['id']}",
                headers=self.auth, json={"enabled": False},
            )
            self.assertEqual(disabled.status_code, 200, disabled.get_data(as_text=True))
            self.assertEqual(request.get(address + "?at=0").status, 404)

    def exercise_vfr(self, viewer, url):
        viewer.goto(url)
        expect(viewer.locator("video")).to_have_count(0)
        viewer.get_by_label("Review password").fill("fixture-frame-password")
        viewer.get_by_role("button", name="Open review").click()
        video = viewer.locator("#reviewTourPlayer")
        expect(video).to_be_visible()
        quality = viewer.locator("#reviewQuality")
        if quality.count():
            original = quality.locator("option").filter(has_text="Original")
            quality.select_option(original.get_attribute("value"))
        viewer.wait_for_function("() => document.querySelector('#reviewTourPlayer').readyState >= 2")
        video.evaluate("""video => {
            video.pause();
            video.decodedTimes = [];
            function observe(_, metadata) {
                video.decodedTimes.push(metadata.mediaTime);
                video.requestVideoFrameCallback(observe);
            }
            video.requestVideoFrameCallback(observe);
            video.currentTime = .101;
        }""")
        self.wait_for_decoded_frame(viewer, .1, 0)
        for button, expected in (
            ("Next frame", .6), ("Previous frame", .1), ("Next frame", .6),
            ("Next frame", .7), ("Next frame", 1.3), ("Next frame", 1.7),
            ("Previous frame", 1.3),
        ):
            first = video.evaluate("video => video.decodedTimes.length")
            viewer.get_by_role("button", name=button, exact=True).click()
            self.wait_for_decoded_frame(viewer, expected, first)

    def wait_for_decoded_frame(self, viewer, pts, first):
        viewer.wait_for_function("""([pts, first]) => {
            const video = document.querySelector('#reviewTourPlayer');
            return !video.seeking && video.decodedTimes.slice(first).some(time => Math.abs(time - pts) < .002);
        }""", arg=[pts, first], timeout=3000)
