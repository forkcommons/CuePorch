"""Generate, edit and record with scripts in the two Studio entry points."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from flask import Flask, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from installed_recording import PROMPTER_SCRIPT

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


def serve(cleanup, app):
    server = cleanup.enter_context(make_server("127.0.0.1", 0, app))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cleanup.callback(thread.join, 4)
    cleanup.callback(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}"


class StudioScripts(unittest.TestCase):
    def test_record_and_event_scripts(self):
        with TemporaryDirectory() as temporary, ExitStack() as cleanup:
            root = Path(temporary)
            library = root / "collection"
            library.mkdir()
            (library / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            model_calls = []
            mock = Flask("studio-script-model")

            @mock.get("/api/tags")
            def models():
                return dict(models=[dict(model="studio-script-fixture:latest")])

            @mock.post("/api/generate")
            def generate_fixture():
                body = request.get_json()
                self.assertEqual(body["model"], "studio-script-fixture")
                self.assertEqual(body["format"]["required"], ["script"])
                options = json.loads(body["prompt"].split("\nScript instructions and source text:\n", 1)[1])
                model_calls.append(options)
                if options["mode"] == "longer":
                    script = options["prior"] + "\nStay for the closing credits."
                elif options["mode"] == "shorter":
                    script = "Welcome to the live screening. Thank you for joining us."
                else:
                    prefix = "Welcome to the live screening." if "screening" in options["brief"] else "Welcome to the library."
                    script = prefix + "\n" + PROMPTER_SCRIPT
                return dict(done=True, done_reason="stop", response=json.dumps(dict(script=script)))

            operator, second = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            cleanup.enter_context(patch.dict(os.environ, {
                "CUEPORCH_OLLAMA_HOST": serve(cleanup, mock),
                "CUEPORCH_OLLAMA_MODEL": "studio-script-fixture",
                "CUEPORCH_OPERATOR_KEYS": json.dumps({"second-operator": second}),
            }))
            for service in ("library_worker", "review_push"):
                cleanup.enter_context(patch(f"institution.{service}.start"))
            app = create_app(library, root / "workspace.sqlite", operator, "station")
            for name in ("stop_event_capture", "stop_editor", "stop_file_links", "stop_caption_jobs", "stop_ai_drafts"):
                cleanup.callback(app.extensions[name])
            origin = serve(cleanup, app)
            headers = {"Authorization": "Bearer " + operator}
            flags = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
            with sync_playwright() as playwright, chromium(playwright, flags) as browser:
                page = browser.new_context(permissions=["camera", "microphone"]).new_page()
                page.add_init_script("sessionStorage.setItem('cueporchOperator', " + json.dumps(operator) + ");")
                script_api = origin + "/api/studio/scripts"
                policy = origin + "/api/policy/ai_script"
                self.assertFalse(page.request.get(origin + "/api/policy", headers=headers).json()["ai_script"])
                self.assertEqual(page.request.get(script_api).status, 403)
                script = page.get_by_label("Script", exact=True)
                panel = page.locator("#scriptGenerator")
                status = panel.get_by_role("status")

                def open_prompter():
                    page.get_by_text("Teleprompter", exact=True).click()
                    panel.get_by_text("Generate a script", exact=True).click()

                page.goto(origin + "/operator/record")
                open_prompter()
                template = panel.get_by_label("Script template")
                self.assertEqual(template.locator("option").all_text_contents(),
                                 ["Custom", "Announcement", "Bug report", "How-to", "Interview",
                                  "Internal communications", "Marketing"])
                self.assertEqual(panel.get_by_label("Duration").locator("option").all_text_contents(),
                                 ["Short", "Medium", "Long"])
                self.assertEqual(panel.get_by_label("Tone").locator("option").all_text_contents(),
                                 ["Confident", "Funny", "Direct", "Inspiring", "Casual", "Formal"])
                template.select_option("Announcement")
                brief = panel.get_by_label("Script brief")
                expect(brief).to_have_value("Announce an upcoming event, explain who can attend and share the date, place and next step.")
                brief.fill("Announce the library's open house for the community.")
                panel.get_by_label("Duration").select_option("Short")
                panel.get_by_label("Tone").select_option("Confident")
                panel.get_by_role("button", name="Generate script", exact=True).click()
                expect(status).to_have_text("Generated script is in the teleprompter.", timeout=15000)
                first = "Welcome to the library.\n" + PROMPTER_SCRIPT
                expect(script).to_have_value(first)
                self.assertEqual((model_calls[0]["template"], model_calls[0]["length"], model_calls[0]["tone"]),
                                 ("Announcement", "Short", "Confident"))
                self.assertIsNone(page.request.get(script_api,
                                  headers={"Authorization": "Bearer " + second}).json()["job"])
                panel.get_by_role("button", name="Make longer").click()
                expect(script).to_have_value(first + "\nStay for the closing credits.", timeout=15000)
                self.assertEqual(model_calls[1]["prior"], first)
                self.record_with_prompter(page, "Welcome to the library.")

                now = datetime.now(timezone.utc)
                schedule = {name: (now + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M")
                            for name, offset in (("start", -1), ("end", 15))}
                live = page.request.post(origin + "/api/live-events", headers=headers, data={
                    "title": "Script screening rehearsal", "visibility": "private", "type": "live", "latency": "standard",
                    "schedule": schedule | {"timezone": "UTC", "repeat": "none"}, "save_replay": False,
                })
                self.assertTrue(live.ok, live.text())
                event = live.json()
                suffix = f"{event['id']}/{event['occurrences'][0]['id']}"
                studio = origin + f"/operator/live-events/{suffix}/studio"
                live_api = origin + f"/api/live-events/{suffix}/studio-scripts"
                self.assertTrue(page.request.put(policy, headers=headers, data={"required": True}).ok)
                page.goto(studio)
                open_prompter()
                page.get_by_label("Production mode").select_option("record")
                expect(script).to_have_value("")
                self.assertIsNone(page.request.get(live_api, headers=headers).json()["job"])
                brief.fill("Introduce the independent arts screening.")
                panel.get_by_role("button", name="Generate script", exact=True).click()
                expect(status).to_have_text("Waiting for script approval.")
                self.assertEqual(len(model_calls), 2)
                script.fill("Use the approved venue notes.")
                self.assertTrue(page.request.put(policy, headers=headers, data={"required": False}).ok)
                expect(status).to_have_text("Script ready. Your teleprompter edits are still in place.", timeout=15000)
                expect(script).to_have_value("Use the approved venue notes.")
                panel.get_by_role("button", name="Use generated script").click()
                live_script = "Welcome to the live screening.\n" + PROMPTER_SCRIPT
                expect(script).to_have_value(live_script)
                page.get_by_text("Scenes and graphics", exact=True).click()
                scenes = page.locator("#studioScenes")
                scenes.get_by_label("Scene template").select_option(label="Opening title")
                scenes.get_by_role("button", name="Add scene", exact=True).click()
                expect(scenes.get_by_role("button", name="Opening title", exact=True)).to_be_visible()
                scenes.get_by_role("button", name="Take scene").click()
                expect(scenes.locator("#studioSceneStatus")).to_have_text("On stage: Opening title")
                self.record_with_prompter(page, "Welcome to the live screening.", camera=False)
                self.assertIn("Welcome to the library", page.request.get(script_api, headers=headers).json()["job"]["result"])
                page.reload()
                open_prompter()
                expect(script).to_have_value(live_script)
                expect(brief).to_have_value("")
                panel.get_by_role("button", name="Make shorter").click()
                expect(script).to_have_value("Welcome to the live screening. Thank you for joining us.", timeout=15000)
                self.assertEqual(model_calls[3]["prior"], live_script)
                self.assertEqual(model_calls[3]["mode"], "shorter")
                self.assertEqual(page.request.get(live_api, headers=headers).json()["job"]["state"], "ready")
                usage = page.request.get(origin + "/api/team/ai/usage", headers=headers).text()
                self.assertTrue(any(",ai_script," in line and line.endswith(event["id"]) for line in usage.splitlines()))
        print("Script templates, tone, duration, optional approval, revisions, event isolation and decoded Record and Live takes passed.")

    def record_with_prompter(self, page, first_line, camera=True):
        view = page.get_by_role("region", name="Teleprompter script")
        expect(view).to_contain_text(first_line)
        if camera:
            page.get_by_role("button", name="Connect camera and microphone").click()
            expect(page.locator("#recordStatus")).to_have_text("Recording devices connected.", timeout=8000)
        page.get_by_role("button", name="Start recording").click()
        page.wait_for_function("() => document.querySelector('#recordPrompter').scrollTop > 12")
        page.get_by_role("button", name="Stop", exact=True).click()
        capture = page.locator("#takePreview")
        expect(capture).to_be_visible()
        self.assertGreater(capture.evaluate("async video => {video.muted=true; await video.play(); return video.videoWidth;}"), 0)


if __name__ == "__main__":
    unittest.main()
