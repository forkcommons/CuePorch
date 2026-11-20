"""Restore, translate and play a saved subtitle track using the installed TextFlowKit adapter."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from flask import Flask, abort, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cueporch import import_collection
from institution import create_app


class SubtitleTranslation(unittest.TestCase):
    def serve(self, cleanup, app):
        server = cleanup.enter_context(make_server("127.0.0.1", 0, app))
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        cleanup.callback(worker.join, 4)
        cleanup.callback(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_translated_track_and_approval(self):
        with TemporaryDirectory() as temporary, ExitStack() as cleanup:
            root = Path(temporary)
            subtitles_path = root / "English.vtt"
            subtitles_path.write_text("WEBVTT\n\n00:00:00.100 --> 00:00:00.900 line:90%\n"
                                      "Welcome to the Council.\n\n"
                                      "00:00:01.000 --> 00:00:01.900\nThe public session starts now.\n", encoding="utf8")
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=256x144:r=12:d=2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / "video.mp4")], check=True)
            manifest, collection = root / "intake.json", root / "library"
            manifest.write_text(json.dumps(dict(institution="station", works=[dict(
                title="Station captions", provider="cueporch-upload", source_id="local-video", source_url="",
                files=[dict(role="playback", format="mp4", path="video.mp4")],
            )])), encoding="utf8")
            import_collection(manifest, collection)
            work = json.loads((collection / "manifest.json").read_text(encoding="utf8"))["works"][0]["id"]

            mock = Flask("local-translation-model")
            calls = []
            control = {"available": True}
            responses = {"es": "1. Bienvenidos al Consejo.\n2. La sesión pública comienza ahora.",
                         "fr": "1. Bienvenue au Conseil.\n2. La séance publique commence maintenant."}

            @mock.post("/api/generate")
            def complete():
                payload = request.get_json()
                self.assertEqual((payload["model"], payload["stream"]), ("local-translation-fixture", False))
                self.assertIn("Welcome to the Council.", payload["prompt"])
                language = payload["prompt"].split("numbered line into ", 1)[1].split(".", 1)[0]
                calls.append(language)
                if not control["available"]:
                    abort(503)
                return dict(done=True, done_reason="stop", response=responses[language])

            mock_origin = self.serve(cleanup, mock)
            cleanup.enter_context(patch.dict(os.environ, {
                "TEXTFLOWKIT_OLLAMA_HOST": mock_origin,
                "TEXTFLOWKIT_TRANSLATE_MODEL": "local-translation-fixture",
            }))
            for service in ("library_worker", "review_push"):
                cleanup.enter_context(patch(f"institution.{service}.start"))
            operator = secrets.token_urlsafe(32)
            headers = {"Authorization": "Bearer " + operator}
            app = create_app(collection, root / "institution.sqlite", operator, "station")
            for name in ("stop_event_capture", "stop_editor", "stop_file_links", "stop_caption_jobs"):
                cleanup.callback(app.extensions[name])
            origin = self.serve(cleanup, app)
            video = origin + "/api/library/work/" + work
            track = video + "/languages/"
            gate = origin + "/api/policy/translation"

            with sync_playwright() as playwright:
                browser = chromium(playwright)
                page = browser.new_page()
                api = page.request
                self.assertEqual(api.get(video + "/caption-jobs").status, 403)
                self.assertFalse(api.get(origin + "/api/policy", headers=headers).json()["translation"])
                self.assertTrue(api.put(video + "/privacy", headers=headers,
                                        data=dict(visibility="public", embed="anywhere", download=False)).ok)
                page.goto(origin + f"/operator/videos/{work}/languages")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open video languages").click()
                page.get_by_label("Subtitle language").fill("en")
                page.get_by_label("Subtitle file").set_input_files(subtitles_path)
                page.get_by_role("button", name="Upload subtitles").click()
                expect(page.get_by_role("button", name="Edit en")).to_be_visible()
                original = api.get(track + "en", headers=headers).json()
                status = page.locator("#subtitleTranslationStatus")

                def translate(target):
                    page.get_by_label("Source subtitle track").select_option("en")
                    page.get_by_label("Target language").fill(target)
                    page.get_by_role("button", name="Translate subtitles").click()

                def waiting(target):
                    translate(target)
                    expect(status).to_have_text("Waiting for translation approval.")
                    response = api.get(track + target, headers=headers)
                    self.assertEqual(response.status, 404)

                translate("es")
                expect(status).to_have_text("Translated subtitles are ready.", timeout=20000)
                expect(page.get_by_role("button", name="Edit es")).to_be_visible()
                output = api.get(track + "es", headers=headers).json()
                self.assertEqual([cue["text"] for cue in output["cues"]],
                                 ["Bienvenidos al Consejo.", "La sesión pública comienza ahora."])
                self.assertEqual([{key: cue[key] for key in ("start", "end", "settings")} for cue in output["cues"]],
                                 [{key: cue[key] for key in ("start", "end", "settings")} for cue in original["cues"]])
                self.assertEqual(api.get(track + "en", headers=headers).json(), original)
                self.assertEqual(calls, ["es"])

                viewer = browser.new_page()
                viewer.goto(origin + "/watch/work/" + work)
                player = viewer.locator("video")
                self.assertEqual(player.evaluate("async video => {video.muted=true; await video.play(); return video.videoWidth;}"), 256)
                viewer.wait_for_function("""() => {
                    const track = [...document.querySelector('video').textTracks].find(track => track.language === 'es');
                    if (track) track.mode = 'hidden';
                    return track?.cues?.[1]?.text === 'La sesión pública comienza ahora.';
                }""")

                self.assertTrue(api.put(gate, headers=headers, data={"required": True}).ok)
                waiting("de")
                self.assertEqual(calls, ["es"])
                edited = [dict(original["cues"][0], text="Welcome to the Council. Today is our regular session."),
                          original["cues"][1]]
                self.assertTrue(api.put(track + "en", headers=headers, data=dict(original, cues=edited)).ok)
                self.assertTrue(api.put(gate, headers=headers, data={"required": False}).ok)
                expect(status).to_have_text("The source subtitle track changed. Start a new translation.")
                self.assertEqual(calls, ["es"])
                self.assertEqual(api.get(track + "de", headers=headers).status, 404)

                control["available"] = False
                translate("fr")
                expect(status).to_have_text("TextFlowKit could not translate this track using the configured local model.")
                self.assertEqual(api.get(track + "fr", headers=headers).status, 404)
                control["available"] = True
                self.assertTrue(api.put(gate, headers=headers, data={"required": True}).ok)
                waiting("fr")
                self.assertEqual(calls, ["es", "fr"])
                self.assertTrue(api.put(gate, headers=headers, data={"required": False}).ok)
                expect(status).to_have_text("Translated subtitles are ready.", timeout=20000)
                self.assertEqual(api.get(track + "fr", headers=headers).json()["cues"][0]["text"], "Bienvenue au Conseil.")
                self.assertEqual(calls, ["es", "fr", "fr"])
                browser.close()


if __name__ == "__main__":
    unittest.main()
