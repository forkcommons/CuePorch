"""Exercise one reusable registration design on the three real visitor surfaces."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
import uuid

from PIL import Image
from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import make_server

from browser import chromium

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


class RegistrationAppearance(unittest.TestCase):
    def test_preset_preview_and_viewer(self):
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
                response = api.open(path, method=method, headers=auth, json=body)
                self.assertLess(response.status_code, 300, response.get_data(as_text=True))
                return response.json

            clip = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264", str(clip)],
                           check=True, capture_output=True)
            logo = root / "logo.png"
            background = root / "backdrop.png"
            Image.new("RGB", (41, 21), "#268378").save(logo)
            Image.new("RGB", (80, 45), "#6b226a").save(background)

            def upload(title, privacy):
                with clip.open("rb") as video:
                    response = requests.post(base + "/api/library/uploads", headers=auth, timeout=25,
                        files={"file": (title + ".mp4", video, "video/mp4")},
                        data={"upload_id": uuid.uuid4().hex, "privacy": json.dumps(privacy)})
                response.raise_for_status()
                return response.json()["id"]

            pw = "Appearance test audience password"
            work = upload("Protected arts film", dict(visibility="password", password=pw, embed="anywhere"))
            member = upload("Showcase film", dict(visibility="public", embed="anywhere"))
            gallery = send("POST", "/api/showcases")
            gallery = send("PUT", f"/api/showcases/{gallery['id']}", dict(
                slug="registration-showcase", revision=gallery["revision"],
                settings=gallery["settings"] | dict(title="Screening gallery", videos=[member], visibility="public"),
            ))
            now = datetime.now(timezone.utc)
            event = send("POST", "/api/live-events", dict(
                title="Station appearance test", slug="registration-event", type="live", latency="standard",
                visibility="public", dvr_seconds=120, save_replay=False, schedule=dict(timezone="UTC", repeat="none",
                    start=(now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M"),
                    end=(now + timedelta(minutes=55)).strftime("%Y-%m-%dT%H:%M"))))

            def check_design(view):
                gate = view.locator(".registration-gate")
                expect(gate).to_have_attribute("data-alignment", "end")
                self.assertEqual(["flex-end", "rgb(15, 49, 50)", "rgb(97, 37, 92)", "0.62"], gate.evaluate("""
                    node => {
                        const styles = getComputedStyle(node);
                        const overlay = getComputedStyle(node, "::before");
                        return [styles.justifyContent, styles.backgroundColor, overlay.backgroundColor, overlay.opacity];
                    }
                """))
                self.assertEqual(41, view.get_by_role("img", name="Organizer logo").evaluate(
                    "async node => {await node.decode();return node.naturalWidth}"))
                self.assertEqual(80, gate.locator(".registration-background").evaluate(
                    "async node => {await node.decode();return node.naturalWidth}"))
                expect(view.get_by_role("heading", name="Register for screenings")).to_be_visible()

            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                page.goto(base + "/watch")
                page.evaluate("(token) => sessionStorage.setItem('cueporchOperator', token)", operator)
                endpoint = f"/api/library/work/{work}/registration"
                page.goto(base + f"/operator/videos/{work}/interactivity")
                editor = page.locator("#registrationEditor")
                expect(editor).to_be_visible()
                editor.get_by_label("Require registration before playback").check()
                editor.get_by_label("Form heading").fill("Register for screenings")
                editor.get_by_label("Form alignment").select_option("end")
                editor.get_by_label("Registration logo").set_input_files(logo)
                editor.get_by_label("Registration background image").set_input_files(background)
                editor.get_by_label("Background color", exact=True).fill("#0f3132")
                editor.get_by_label("Overlay color").fill("#61255c")
                editor.get_by_label("Overlay opacity").fill("62")
                editor.get_by_label("Save as registration preset").fill("Arts screenings")
                editor.get_by_role("button", name="Preview registration").click()
                frame = page.locator("#registrationPreview")
                expect(frame).to_have_attribute("data-size", "desktop")
                preview = page.frame_locator("#registrationPreview")
                check_design(preview)
                self.assertEqual(800, frame.evaluate("node => node.getBoundingClientRect().width"))
                editor.get_by_label("Form preview device").select_option("mobile")
                self.assertEqual(390, frame.evaluate("node => node.getBoundingClientRect().width"))
                check_design(preview)
                self.assertFalse(send("GET", endpoint)["settings"]["enabled"])
                self.assertEqual(403, api.post("/api/registration/preview", json={}).status_code)
                editor.get_by_role("button", name="Save registration").click()
                expect(editor.locator("#registrationStatus")).to_have_text("Registration saved.")

                settings = send("GET", endpoint)
                invalid = settings["settings"] | {"appearance": settings["settings"]["appearance"] | {"opacity": 101}}
                self.assertEqual(422, api.put(endpoint, headers=auth,
                    json={"settings": invalid, "revision": settings["revision"]}).status_code)
                expect(editor.get_by_label("Overlay opacity")).to_have_value("62")

                page.goto(base + "/operator/showcases?select=" + gallery["id"])
                page.get_by_role("button", name="Registration", exact=True).click()
                editor.get_by_label("Saved registration preset").select_option("Arts screenings")
                editor.get_by_role("button", name="Save registration").click()
                expect(editor.locator("#registrationStatus")).to_have_text("Registration saved.")

                page.goto(base + "/operator/live-events")
                page.get_by_role("button", name="Open production for Station appearance test", exact=False).click()
                page.get_by_role("button", name="Registration", exact=True).click()
                editor.get_by_label("Saved registration preset").select_option("Arts screenings")
                editor.get_by_role("button", name="Save registration").click()
                expect(editor.locator("#registrationStatus")).to_have_text("Registration saved.")

                for viewport in ({"width": 1280, "height": 880}, {"width": 390, "height": 780}):
                    with browser.new_context(viewport=viewport) as guests:
                        visitor = guests.new_page()
                        targets = [(f"/watch/work/{work}", True), (gallery["watch"], True), (event["watch"], False)]
                        for path, playable in targets:
                            visitor.goto(base + path)
                            if path == targets[0][0]:
                                expect(visitor.locator(".registration-gate")).to_have_count(0)
                                visitor.get_by_label("Video password").fill(pw)
                                visitor.get_by_role("button", name="Watch video").click()
                            check_design(visitor)
                            expect(visitor.locator("video")).to_have_count(0)
                            visitor.get_by_label("Email", exact=True).fill("visitor@example.test")
                            visitor.get_by_role("button", name="Register and watch").click()
                            expect(visitor.locator(".registration-gate")).to_have_count(0)
                            if playable:
                                self.assertEqual(320, visitor.locator("video").evaluate(
                                    "async v => {v.muted=true; await v.play(); return v.videoWidth;}"))
                            else:
                                expect(visitor.locator("#eventStatus")).to_have_text("Scheduled")
                                self.assertTrue(visitor.locator("#eventLobby").get_attribute("data-viewer-grant"))


if __name__ == "__main__":
    unittest.main()
