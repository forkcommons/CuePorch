import json
import secrets
import subprocess
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from institution import create_app
from test_drive_import import QuietHandler, service_worker

PASSWORD = "fixture-folder-password"


class FolderReview(unittest.TestCase):
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

    def create_folder(self, name, parent=None):
        payload = {"name": name, "color": "#75198a"}
        if parent is not None:
            payload["parent"] = parent
        r = self.client.post("/api/library/folders", headers=self.auth, json=payload)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()["id"]

    def upload(self, title, color, folder):
        clip = self.root / f"{title}.mp4"
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            f"color=c={color}:s=160x90:r=24:d=2", "-an", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(clip),
        ], check=True, capture_output=True, timeout=20,
           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        uploaded = self.client.post("/api/library/uploads", headers=self.auth, data={
            "upload_id": str(uuid4()), "file": (BytesIO(clip.read_bytes()), clip.name),
        }, content_type="multipart/form-data")
        self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
        uid = uploaded.get_json()["id"]
        self.move(uid, folder)
        return uid

    def move(self, uid, folder):
        placed = self.client.put("/api/library/placement", headers=self.auth,
                                 json={"works": [uid], "folder": folder})
        self.assertEqual(placed.status_code, 204, placed.get_data(as_text=True))

    def create_link(self, browser):
        page = browser.new_page()
        page.goto(self.base + "/operator/library")
        page.locator("#key").fill(self.auth["Authorization"][7:])
        page.get_by_role("button", name="Load library").click()
        folders = page.get_by_role("navigation", name="Folders")
        folders.get_by_role("button", name="Festival Review", exact=True).click()
        page.get_by_role("button", name="Folder review links").click()
        page.get_by_label("Folder review name").fill("Curator round")
        page.get_by_label("Review password").fill(PASSWORD)
        page.get_by_role("button", name="Create folder review link").click()

        review_link = page.get_by_role("link", name="Curator round")
        review_link.wait_for(state="visible")
        return page, review_link.get_attribute("href")

    def open_link(self, page, link, check_bad_password=False):
        response = page.goto(link)
        self.assertEqual(response.status, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        expect(page.get_by_role("list", name="Videos for review")).to_have_count(0)
        if check_bad_password:
            page.get_by_label("Review password").fill("wrong")
            page.get_by_role("button", name="Open review").click()
            expect(page.get_by_role("alert")).to_contain_text("That password did not open")
        page.get_by_label("Review password").fill(PASSWORD)
        page.get_by_role("button", name="Open review").click()
        expect(page.get_by_role("list", name="Videos for review")).to_be_visible()

    def test_folder_review_shares_only_current_members(self):
        folder = self.create_folder("Festival Review")
        subfolder = self.create_folder("Rough cuts", folder)
        outside = self.create_folder("Other work")
        self.upload("Opening Reel", "blue", folder)
        second = self.upload("Nested Reel", "green", subfolder)
        outsider = self.upload("Unshared Reel", "red", outside)
        endpoint = f"/api/library/folders/{folder}/review-links"
        self.assertEqual(self.client.get(endpoint).status_code, 403)
        with sync_playwright() as pw, chromium(pw) as browser:
            operator, link = self.create_link(browser)
            first_viewer, second_viewer = browser.new_page(), browser.new_page()
            self.open_link(first_viewer, link, check_bad_password=True)
            self.open_link(second_viewer, link)
            expect(first_viewer.get_by_role("link", name="Opening Reel", exact=True)).to_be_visible()
            expect(first_viewer.get_by_role("link", name="Unshared Reel", exact=True)).to_have_count(0)
            self.assertEqual(first_viewer.request.get(f"{link}/item/{outsider}").status, 404)

            first_viewer.get_by_role("link", name="Nested Reel", exact=True).click()
            player = first_viewer.locator("#reviewTourPlayer")
            decoded = player.evaluate("""async video => {
                video.muted = true;
                await video.play();
                const width = await new Promise(resolve =>
                    video.requestVideoFrameCallback(() => resolve(video.videoWidth)));
                video.pause();
                return width;
            }""")
            self.assertEqual(decoded, 160)
            child = first_viewer.url
            source = player.evaluate("video => video.currentSrc")
            frames = self.base + player.get_attribute("data-frames") + "?at=0"
            feedback = self.base + first_viewer.locator("#reviewFeedback").get_attribute("data-feedback")
            self.assertEqual(first_viewer.request.get(frames).status, 200)
            self.assertEqual(first_viewer.request.get(source, params={"download": "1"}).status, 403)
            with browser.new_context() as anonymous:
                for address in (child, source, frames, feedback):
                    self.assertEqual(anonymous.request.get(address).status, 403, address)
                self.assertEqual(anonymous.request.post(child + "/unlock", form={"password": PASSWORD}).status, 403)
            self.assertEqual(self.client.get(f"/watch/work/{second}").status_code, 404)
            standalone = self.client.get(f"/api/library/work/{second}/review-links", headers=self.auth)
            self.assertEqual(standalone.status_code, 200)
            self.assertNotIn(urlsplit(child).path, {entry["path"] for entry in standalone.get_json()})

            first_viewer.get_by_role("button", name="Use current time", exact=True).click()
            first_viewer.get_by_label("Name", exact=True).fill("Reviewer A")
            first_viewer.get_by_label("Comment", exact=True).fill("Check the opening frame")
            first_viewer.get_by_role("button", name="Post comment", exact=True).click()
            expect(first_viewer.get_by_text("Check the opening frame", exact=True)).to_be_visible()
            thread = first_viewer.request.get(feedback).json()["comments"][0]
            self.assertIsNotNone(thread["seconds"])

            second_viewer.get_by_role("link", name="Nested Reel", exact=True).click()
            self.assertEqual(second_viewer.url, child)
            second_viewer.get_by_role("button", name="Reply", exact=True).click()
            second_viewer.get_by_label("Name", exact=True).fill("Reviewer B")
            comment = second_viewer.get_by_label("Comment", exact=True)
            comment.fill("@Reviewer")
            second_viewer.get_by_role("button", name="Mention Reviewer A", exact=True).click()
            comment.press("End")
            comment.press_sequentially("I will take a look.")
            second_viewer.get_by_role("button", name="Post reply", exact=True).click()
            expect(second_viewer.get_by_text("@Reviewer A I will take a look.", exact=True)).to_be_visible()
            bell = first_viewer.get_by_text("Notifications (1)", exact=True)
            expect(bell).to_be_visible(timeout=15000)
            bell.click()
            expect(first_viewer.get_by_role("link", name="Reviewer B mentioned you", exact=True)).to_be_visible()

            first_viewer.get_by_role("link", name="Back to folder review").click()
            first_viewer.get_by_role("link", name="Opening Reel", exact=True).click()
            sibling = first_viewer.url
            self.assertEqual(first_viewer.request.get(sibling).status, 200)

            self.move(second, outside)
            for viewer in (first_viewer, second_viewer):
                for address in (child, source, frames, feedback):
                    self.assertEqual(viewer.request.get(address).status, 404, address)
            first_viewer.goto(link)
            expect(first_viewer.get_by_role("link", name="Nested Reel", exact=True)).to_have_count(0)
            self.assertEqual(first_viewer.request.get(sibling).status, 200)
            self.move(outsider, folder)
            first_viewer.reload()
            expect(first_viewer.get_by_role("link", name="Unshared Reel", exact=True)).to_be_visible()

            with operator.expect_response(lambda response: response.request.method == "PUT"
                                           and urlsplit(response.url).path.startswith(endpoint + "/")) as disabled:
                operator.get_by_role("button", name="Disable Curator round").click()
            self.assertEqual(disabled.value.status, 200)
            self.assertEqual(first_viewer.request.get(link).status, 404)
            self.assertEqual(second_viewer.request.get(sibling).status, 404)
            operator.get_by_role("button", name="Enable Curator round").click()
            expect(operator.get_by_role("button", name="Disable Curator round")).to_be_visible()
            self.assertEqual(first_viewer.request.get(sibling).status, 403)
            self.open_link(first_viewer, link)
            self.assertEqual(first_viewer.request.get(sibling).status, 200)
