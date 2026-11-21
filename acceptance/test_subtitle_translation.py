"""Restore, translate and play a saved subtitle track using the installed TextFlowKit adapter."""

from contextlib import ExitStack
import csv
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from flask import Flask, abort, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import caption_adapter
from cueporch import import_collection
from institution import create_app
import local_model


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
                title=title, provider="cueporch-upload", source_id=title, source_url="",
                files=[dict(role="playback", format="mp4", path="video.mp4")],
            ) for title in ("Station captions", "Nested interview", "Outside the folder")])), encoding="utf8")
            import_collection(manifest, collection)
            imported = json.loads((collection / "manifest.json").read_text(encoding="utf8"))["works"]
            work, nested, outside = [item["id"] for item in imported]

            mock = Flask("local-translation-model")
            calls = []
            draft_calls = []
            generation_requests = []
            control = {"available": True}
            model_started, release_model = Event(), Event()
            cleanup.callback(release_model.set)
            responses = {"es": "1. Bienvenidos al Consejo.\n2. La sesión pública comienza ahora.",
                         "fr": "1. Bienvenue au Conseil.\n2. La séance publique commence maintenant."}
            responses.update({"es-mx": responses["es"], "fr-ca": responses["fr"],
                              "es-ar": responses["es"], "es-cl": responses["es"]})

            @mock.get("/api/tags")
            def models():
                return dict(models=[
                    dict(name="local-editorial-fixture:latest"),
                    dict(model="local-translation-fixture:latest"),
                    dict(model="regional-model:latest", remote_model="example:cloud", remote_host="https://example.invalid"),
                ])

            @mock.post("/api/generate")
            def complete():
                payload = request.get_json()
                generation_requests.append(payload["model"])
                if "format" in payload:
                    self.assertEqual((payload["model"], payload["stream"]), ("local-editorial-fixture", False))
                    transcript = json.loads(payload["prompt"].split("\nTranscript:\n", 1)[1])
                    self.assertIn("Welcome to the Council.", transcript[0]["text"])
                    fields = payload["format"]["properties"]
                    if "answer" in fields:
                        question = json.loads(payload["prompt"].split("\nQuestion:\n", 1)[1].split("\nTranscript:\n", 1)[0])
                        if question == "What equipment was used?":
                            result = dict(answer=None, cues=[])
                        elif "using the language code es" in payload["prompt"]:
                            result = dict(answer="La sesión pública está comenzando.", cues=[2])
                        else:
                            result = dict(answer="The Council is opening the public session.", cues=[2])
                    elif "clips" in fields:
                        highlight = json.loads(payload["prompt"].split("\nHighlight request:\n", 1)[1].split("\nTranscript:\n", 1)[0])
                        draft_calls.append(highlight)
                        self.assertEqual(transcript[1]["start"], 1)
                        result = {"clips": [{"first": 2, "last": 2}, {"first": 1, "last": 1}]}
                        if highlight == "Return invalid cues.":
                            result = {"clips": [{"first": 2, "last": 20}]}
                    elif "chapters" in fields:
                        draft_calls.append("chapters")
                        result = {"chapters": [{"cue": 1, "title": "Welcome"}, {"cue": 2, "title": "Public session"}]}
                    else:
                        draft_calls.append("details")
                        result = dict(title="Council session", description="The Council opens a public session.",
                                      tags=["council", "public session"])
                    return dict(done=True, done_reason="stop", response=json.dumps(result))
                self.assertEqual((payload["model"], payload["stream"]), ("local-translation-fixture", False))
                self.assertIn("Welcome to the Council.", payload["prompt"])
                language = payload["prompt"].split("numbered line into ", 1)[1].split(".", 1)[0]
                calls.append(language)
                if not control["available"]:
                    abort(503)
                if language == "es-cl":
                    model_started.set()
                    if not release_model.wait(15):
                        abort(503)
                return dict(done=True, done_reason="stop", response=responses[language])

            mock_origin = self.serve(cleanup, mock)
            cleanup.enter_context(patch.dict(os.environ, {
                "TEXTFLOWKIT_OLLAMA_HOST": mock_origin,
                "TEXTFLOWKIT_TRANSLATE_MODEL": "local-translation-fixture",
                "CUEPORCH_OLLAMA_HOST": mock_origin,
                "CUEPORCH_OLLAMA_MODEL": "local-editorial-fixture",
            }))
            with patch.dict(os.environ, {"CUEPORCH_OLLAMA_MODEL": "regional-model",
                                         "TEXTFLOWKIT_TRANSLATE_MODEL": "regional-model"}):
                with self.assertRaisesRegex(local_model.ModelError, "cloud alias"):
                    local_model.generate("Private fixture transcript", {})
                with self.assertRaisesRegex(caption_adapter.CaptionError, "cloud alias"):
                    caption_adapter.translate_cues([{"text": "Private fixture transcript"}], "es")
            with patch.dict(os.environ, {"CUEPORCH_OLLAMA_MODEL": "regional:1017-cloud"}):
                self.assertIsNone(local_model.runtime())
            self.assertEqual(generation_requests, [])
            print("Ollama inventory: renamed cloud alias rejected before generation and translation.")
            for service in ("library_worker", "review_push"):
                cleanup.enter_context(patch(f"institution.{service}.start"))
            operator = secrets.token_urlsafe(32)
            headers = {"Authorization": "Bearer " + operator}
            app = create_app(collection, root / "institution.sqlite", operator, "station")
            for name in ("stop_event_capture", "stop_editor", "stop_file_links", "stop_caption_jobs", "stop_ai_drafts"):
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
                self.verify_batches(page, origin, headers, work, nested, outside, calls, subtitles_path)
                self.verify_ai_policy(browser, page, origin, headers, work, calls, model_started, release_model)
                self.verify_video_drafts(page, viewer, origin, headers, work, draft_calls)
                self.verify_highlights(browser, page, origin, headers, work, draft_calls)
                self.verify_video_questions(cleanup, page, viewer, origin, headers, work)
                browser.close()

    def verify_batches(self, page, origin, headers, work, nested, outside, calls, vtt):
        api = page.request
        path = origin + "/api/library/subtitle-translations"
        folder_api = origin + "/api/library/folders"
        gate = origin + "/api/policy/translation"
        dialog = page.get_by_role("dialog")
        status = page.locator("#workDialogStatus")
        self.assertEqual(api.get(path).status, 403)

        def submit(target):
            dialog.get_by_label("Source language").fill("en")
            dialog.get_by_label("Target language").fill(target)
            dialog.get_by_role("button", name="Queue translations").click()

        page.goto(origin + "/operator/library")
        page.get_by_role("button", name="Load library").click()
        page.get_by_role("checkbox", name="Select Station captions").check()
        page.get_by_role("button", name="Translate selected subtitles").click()
        submit("es-mx")
        expect(status).to_have_text("1 of 1 complete.", timeout=15000)
        self.assertEqual(calls[-1], "es-mx")
        dialog.get_by_role("button", name="Close").click()

        parent = api.post(folder_api, headers=headers, data=dict(name="Council archive", color="#345678")).json()["id"]
        child = api.post(folder_api, headers=headers,
                         data=dict(name="Interviews", color="#345678", parent=parent)).json()["id"]
        for folder, member in ((parent, work), (child, nested)):
            self.assertTrue(api.put(origin + "/api/library/placement", headers=headers,
                                    data=dict(works=[member], folder=folder)).ok)
        self.assertTrue(api.put(gate, headers=headers, data={"required": True}).ok)
        page.reload()
        page.get_by_role("button", name="Load library").click()
        page.locator("#folderTree").get_by_role("button", name="Council archive").click()
        page.get_by_role("button", name="Translate this folder").click()
        submit("fr-ca")
        expect(dialog.get_by_text("Station captions · Waiting for approval")).to_be_visible()
        expect(dialog.get_by_text("Nested interview · Skipped · This cut has no subtitle track in the source language.")).to_be_visible()
        self.assertNotIn("fr-ca", calls)
        self.assertTrue(api.put(gate, headers=headers, data={"required": False}).ok)
        expect(status).to_have_text("1 of 2 complete.", timeout=15000)

        batch = next(item for item in api.get(path, headers=headers).json()["batches"] if item["target"] == "fr-ca")
        batch_path = path + "/" + batch["id"]
        records = {item["work"]: item for item in api.get(batch_path, headers=headers).json()["items"]}
        self.assertEqual(set(records), {work, nested})
        self.assertNotIn(outside, records)
        replay = api.post(path, headers=headers, data=dict(id=batch["id"], folder=parent, source_language="en",
                                                            target_language="fr-ca"))
        self.assertEqual(replay.status, 200)
        self.assertEqual({item["work"]: item for item in replay.json()["items"]}, records)

        languages = origin + f"/api/library/work/{nested}/languages"
        version = api.get(languages, headers=headers).json()["version"]
        upload = api.post(languages, headers=headers, multipart={
            "version": version, "language": "en", "kind": "subtitles",
            "track": {"name": "English.vtt", "mimeType": "text/vtt", "buffer": vtt.read_bytes()},
        })
        self.assertTrue(upload.ok)
        dialog.get_by_role("button", name="Retry failed or skipped").click()
        expect(status).to_have_text("2 of 2 complete.", timeout=15000)
        self.assertEqual(calls.count("fr-ca"), 2)
        finished = {item["work"]: item for item in api.get(batch_path, headers=headers).json()["items"]}
        self.assertEqual(finished[work]["job"], records[work]["job"])
        self.assertTrue(api.get(languages + "/fr-ca", headers=headers).ok)

        page.reload()
        page.get_by_role("button", name="Load library").click()
        page.get_by_role("button", name="Subtitle translation batches").click()
        dialog.get_by_role("button", name="en → fr-ca · 2 items", exact=False).click()
        expect(status).to_have_text("2 of 2 complete.")

    def verify_ai_policy(self, browser, page, origin, headers, work, calls, model_started, release_model):
        api = page.request
        settings_api = origin + "/api/team/ai"
        jobs = origin + f"/api/library/work/{work}/caption-jobs"
        languages = origin + f"/api/library/work/{work}/languages"
        batches = origin + "/api/library/subtitle-translations"
        gate = origin + "/api/policy/translation"
        team_headers = {"X-CuePorch-Team": "1"}
        member_headers = {"Authorization": "Bearer team-session"}
        self.assertEqual(api.get(settings_api + "/usage").status, 403)
        admin = browser.new_page()
        invite = api.post(origin + "/api/team/invitations", headers=headers | team_headers,
                          data=dict(email="caption-admin@station.example", role="admin"))
        self.assertTrue(invite.ok, invite.text())
        joined = admin.request.post(origin + "/api/team/join", headers=team_headers, data=dict(
            invitation=invite.json()["path"].split("#", 1)[1],
            name="Caption administrator", password="Strong local fixture phrase",
        ))
        self.assertEqual(joined.status, 201, joined.text())
        self.assertEqual(admin.request.get(settings_api, headers=member_headers).status, 403)
        self.assertTrue(admin.request.get(jobs, headers=member_headers).json()["translation_available"])

        page.goto(origin + "/team/settings")
        page.get_by_role("link", name="AI settings").click()
        form = page.locator("#aiSettings")
        translation = form.get_by_role("group", name="Translate subtitles")
        captions = form.get_by_role("group", name="Generate captions")
        translation_enabled = translation.get_by_label("Enable for this workspace")
        translation_admins = translation.get_by_label("Allow Administrators to run this workflow")
        original = api.get(languages + "/en", headers=headers).json()
        version = original["version"]
        before = api.get(settings_api, headers=headers).json()
        expect(translation_enabled).to_be_checked()
        expect(translation_admins).to_be_checked()

        def save():
            with page.expect_response(lambda response: response.url == settings_api and response.request.method == "PUT") as result:
                form.get_by_role("button", name="Save AI settings").click()
            self.assertTrue(result.value.ok, result.value.text())
            expect(page.get_by_role("status")).to_have_text("AI settings saved.")

        form.get_by_label("Subtitle dictionary").fill("*\tConsejo\tConcejo local\nes-ar\tConsejo\tConcejo de Longmont")
        captions.get_by_label("Enable for this workspace").uncheck()
        translation_admins.uncheck()
        save()
        self.assertEqual(api.put(settings_api, headers=headers | team_headers, data=before).status, 409)
        self.assertEqual(api.post(jobs, headers=headers, data=dict(version=version)).status, 403)
        self.assertTrue(api.put(languages, headers=headers, data=dict(version=version, language="en", generate=True)).ok)
        self.assertFalse(any(not job["source_language"] for job in api.get(jobs, headers=headers).json()["jobs"]))

        payload = dict(version=version, source_language="en", target_language="es-ar")
        self.assertEqual(admin.request.post(jobs, headers=member_headers, data=payload).status, 403)
        self.assertEqual(admin.request.post(batches, headers=member_headers,
                         data=dict(works=[work], source_language="en", target_language="es-ar")).status, 403)
        amount = len(calls)
        self.assertTrue(api.put(gate, headers=headers, data={"required": True}).ok)
        self.assertTrue(api.post(jobs, headers=headers, data=payload).ok)
        translation_enabled.uncheck()
        save()
        self.assertTrue(api.put(gate, headers=headers, data={"required": False}).ok)

        def job_state(language, state):
            deadline = monotonic() + 18
            latest = {}
            while monotonic() < deadline:
                report = api.get(jobs, headers=headers)
                self.assertTrue(report.ok, report.text())
                latest = next((job for job in report.json()["jobs"] if job["language"] == language), {})
                if latest.get("state") == state:
                    return
                if latest.get("state") in {"complete", "failed", "blocked"}:
                    break
                sleep(0.25)
            self.fail(f"Expected {language} job to be {state}. Last observed: {latest}")

        job_state("es-ar", "blocked")
        self.assertEqual(len(calls), amount)
        self.assertEqual(api.get(languages + "/es-ar", headers=headers).status, 404)

        translation_enabled.check()
        translation_admins.check()
        save()
        self.assertTrue(admin.request.post(jobs, headers=member_headers, data=payload).ok)
        job_state("es-ar", "complete")
        revised = api.get(languages + "/es-ar", headers=headers).json()
        self.assertEqual(revised["cues"][0]["text"], "Bienvenidos al Concejo de Longmont.")
        self.assertEqual(revised["cues"][0]["settings"], original["cues"][0]["settings"])
        self.assertEqual(api.get(languages + "/en", headers=headers).json(), original)

        payload["target_language"] = "es-cl"
        self.assertTrue(api.post(jobs, headers=headers, data=payload).ok)
        try:
            self.assertTrue(model_started.wait(10))
            translation_enabled.uncheck()
            save()
        finally:
            release_model.set()
        job_state("es-cl", "blocked")
        self.assertEqual(api.get(languages + "/es-cl", headers=headers).status, 404)

        with page.expect_download() as download:
            form.get_by_role("button", name="Download 90-day usage CSV").click()
        self.assertEqual(download.value.suggested_filename, "cueporch-ai-usage.csv")
        with download.value.path().open(encoding="utf8", newline="") as report:
            rows = list(csv.DictReader(report))
        recent = [row for row in rows if row["language"] == "es-ar"]
        self.assertEqual([(row["status"], row["published_cues"]) for row in recent],
                         [("complete", "2"), ("blocked", "0")])
        self.assertEqual(recent[0]["requested_by"], "team:" + joined.json()["id"])
        self.assertEqual(recent[0]["workflow"], "translation")
        self.assertEqual(recent[0]["work_id"], work)
        self.assertEqual(admin.request.get(settings_api + "/usage", headers=member_headers).status, 403)
        print("AI settings: Owner access, disabled/role restrictions, queued and running blocks, dictionary and usage verified.")

    def verify_video_drafts(self, page, viewer, origin, headers, work, calls):
        api = page.request
        video = origin + f"/api/library/work/{work}"
        details_gate = origin + "/api/policy/ai_details"
        chapters_gate = origin + "/api/policy/ai_chapters"
        self.assertEqual(api.get(video + "/ai-drafts").status, 403)
        self.assertFalse(api.get(details_gate.rpartition("/")[0], headers=headers).json()["ai_details"])
        page.goto(origin + f"/operator/videos/{work}")
        tools = page.locator("#aiVideoTools")
        details = tools.locator('[data-ai-step="ai_details"]')
        chapters = tools.locator('[data-ai-step="ai_chapters"]')
        details_button = details.get_by_role("button", name="Generate video details")
        chapters_button = chapters.get_by_role("button", name="Generate chapters")
        automatic = tools.get_by_label("Save generated results automatically")
        expect(automatic).to_be_checked()
        tools.get_by_label("Source subtitle track").select_option("en")
        details_button.click()
        expect(details.get_by_role("status")).to_have_text("Generated result saved.", timeout=18000)
        expect(page.get_by_role("heading", name="Council session")).to_be_visible()
        metadata = api.get(video + "/details", headers=headers).json()
        self.assertEqual((metadata["description"], metadata["tags"]),
                         ("The Council opens a public session.", ["council", "public session"]))
        self.assertEqual(calls, ["details"])

        self.assertTrue(api.put(details_gate, headers=headers, data={"required": True}).ok)
        details_button.click()
        expect(details.get_by_role("status")).to_have_text("Waiting for operator approval.")
        self.assertEqual(calls, ["details"])
        manual = page.locator("#videoMeta")
        manual.get_by_label("Title", exact=True).fill("Approved Council record")
        manual.get_by_role("button", name="Save video details").click()
        expect(page.get_by_role("heading", name="Approved Council record")).to_be_visible()
        self.assertTrue(api.put(details_gate, headers=headers, data={"required": False}).ok)
        expect(details.get_by_role("status")).to_have_text(
            "Existing details or chapters changed after this job was queued. Generate a new draft.", timeout=18000)
        self.assertEqual(api.get(video + "/details", headers=headers).json()["title"], "Approved Council record")

        automatic.uncheck()
        self.assertTrue(api.put(chapters_gate, headers=headers, data={"required": True}).ok)
        chapters_button.click()
        expect(chapters.get_by_role("status")).to_have_text("Waiting for operator approval.")
        self.assertEqual(calls, ["details", "details"])
        self.assertTrue(api.put(chapters_gate, headers=headers, data={"required": False}).ok)
        expect(chapters.get_by_role("status")).to_have_text("Draft ready. Edit it here before saving.", timeout=18000)
        self.assertEqual(api.get(video + "/chapters", headers=headers).json()["chapters"], [])
        page.reload()
        second = chapters.get_by_role("group", name="Chapter 2")
        seconds = second.get_by_label("Start (seconds)")
        expect(seconds).to_have_value("1")
        second.get_by_label("Chapter title").fill("Open Council session")
        seconds.fill("8")
        chapters.get_by_role("button", name="Save suggested chapters").click()
        expect(chapters.get_by_role("status")).to_have_text("Enter chapter times in whole seconds within the video.")
        self.assertEqual(api.get(video + "/chapters", headers=headers).json()["chapters"], [])
        seconds.fill("1")
        chapters.get_by_role("button", name="Save suggested chapters").click()
        expect(chapters.get_by_role("status")).to_have_text("Generated result saved.")
        result = api.get(video + "/chapters", headers=headers).json()["chapters"]
        self.assertEqual(result, [dict(title="Welcome", timecode=0), dict(title="Open Council session", timecode=1)])
        self.assertEqual(calls, ["details", "details", "chapters"])

        viewer.goto(origin + "/watch/work/" + work)
        player = viewer.locator("video")
        self.assertEqual(player.evaluate("async video => {video.muted=true; await video.play(); video.pause(); return video.videoWidth;}"), 256)
        viewer.get_by_label("Chapters", exact=True).select_option(label="Open Council session")
        viewer.wait_for_function("() => document.querySelector('video').currentTime >= 0.99")
        usage = api.get(origin + "/api/team/ai/usage", headers=headers)
        self.assertTrue(usage.ok)
        rows = list(csv.DictReader(usage.text().splitlines()))
        self.assertEqual([row["status"] for row in rows if row["workflow"] == "ai_details"], ["superseded", "saved"])
        self.assertEqual([row["status"] for row in rows if row["workflow"] == "ai_chapters"], ["saved"])
        print("Video draft verification: automatic details, stale-edit protection, persisted chapter draft and decoded chapter seek.")

    def verify_highlights(self, browser, page, origin, headers, work, calls):
        source = origin + f"/api/library/work/{work}"
        approvals = origin + "/api/policy/ai_highlights"
        api = page.request
        self.assertFalse(api.get(origin + "/api/policy", headers=headers).json()["ai_highlights"])
        expect(page.get_by_label("Source subtitle track")).to_have_value("en")
        self.assertTrue(api.put(approvals, headers=headers, data={"required": True}).ok)
        reel = page.locator('[data-ai-step="ai_highlights"]')
        description = reel.get_by_label("Describe the highlight reel")
        create = reel.get_by_role("button", name="Create private highlight reel")
        description.fill("Open with the public session, then replay the welcome.")
        take_count = len(calls)
        create.click()
        expect(reel.get_by_role("status")).to_have_text("Waiting for operator approval.")
        self.assertEqual(len(calls), take_count)
        self.assertTrue(api.put(approvals, headers=headers, data={"required": False}).ok)
        expect(reel.get_by_role("status")).to_have_text("Private highlight reel saved.", timeout=25000)
        self.assertEqual(calls[-1], "Open with the public session, then replay the welcome.")
        report = api.get(source + "/ai-drafts", headers=headers).json()["jobs"]["ai_highlights"]
        self.assertEqual(report["state"], "saved")
        target = report["result"]["work"]
        self.assertNotEqual(target, work)
        self.assertAlmostEqual(report["result"]["duration"], 1.7, delta=0.12)
        self.assertEqual(api.get(source + "/details", headers=headers).json()["title"], "Approved Council record")
        self.assertEqual(api.get(origin + f"/watch/work/{target}").status, 404)
        result = api.get(origin + f"/api/library/work/{target}/languages/en", headers=headers).json()
        self.assertEqual([cue["text"] for cue in result["cues"]],
                         ["The public session starts now.", "Welcome to the Council. Today is our regular session."])
        self.assertEqual([cue["start"] for cue in result["cues"]], [0, 900])

        preview = browser.new_page()
        preview.add_init_script("sessionStorage.setItem('cueporchOperator', " +
                                json.dumps(headers["Authorization"].removeprefix("Bearer ")) + ");")
        preview.goto(origin + reel.get_by_role("link", name="Preview private reel").get_attribute("href"))
        player = preview.locator("#workspacePreview")
        expect(player).to_be_visible()
        expect(player).to_have_attribute("src", re.compile(r"^/library-media/"))
        self.assertEqual(player.evaluate("async video => {video.muted=true; await video.play(); return video.videoWidth;}"), 1280)
        expect(reel.get_by_role("link", name="Open in Timeline editor")).to_have_attribute("href", f"/operator/videos/{target}/timeline")
        preview.close()
        description.fill("Return invalid cues.")
        create.click()
        expect(reel.get_by_role("status")).to_have_text(
            "The local model selected cues outside the source transcript.", timeout=15000)
        failed = api.get(source + "/ai-drafts", headers=headers).json()["jobs"]["ai_highlights"]
        self.assertEqual((failed["state"], failed["result"]), ("failed", None))
        print("Highlights: withheld model, private separate reel, decoded video, retimed cues and malformed selection passed.")

    def verify_video_questions(self, cleanup, operator, viewer, origin, headers, work):
        api = operator.request
        video = origin + f"/api/library/work/{work}"
        settings_path = origin + "/api/team/ai"
        questions_path = origin + "/api/viewer/video-questions"
        watch = origin + f"/watch/work/{work}"
        panel = viewer.locator("#videoQuestions")
        marker = {"X-CuePorch-Questions": "1"}
        expect(panel).to_have_count(0)
        self.assertEqual(viewer.request.post(questions_path, headers=marker,
                         data=dict(session="invalid", question="What is this about?")).status, 404)

        operator.goto(origin + "/team/ai")
        operator.get_by_label("Allow authorized viewers to ask questions about videos").check()
        operator.get_by_role("button", name="Save AI settings").click()
        expect(operator.locator("#aiStatus")).to_have_text("AI settings saved.")
        viewer.reload()
        panel.get_by_text("Ask about this video", exact=True).click()
        panel.get_by_role("button", name="What is this video about?").click()
        expect(panel.locator("[data-answer-text]")).to_have_text("The Council is opening the public session.", timeout=15000)
        expect(panel.get_by_role("list", name="Answer sources")).to_contain_text("The public session starts now.")
        panel.get_by_role("button", name="Play from 0:01").click()
        viewer.wait_for_function("() => document.querySelector('video').currentTime >= 0.99")
        self.assertEqual(viewer.locator("video").evaluate("video => video.videoWidth"), 256)
        key = panel.get_attribute("data-session")
        query = dict(session=key, question="What equipment was used?")
        panel.get_by_label("Your question").fill(query["question"])
        panel.get_by_role("button", name="Ask", exact=True).click()
        expect(panel.get_by_role("status")).to_have_text("The subtitles do not contain an answer to this question.")
        expect(panel.locator("[data-answer-text]")).to_be_hidden()

        self.assertTrue(api.put(video + "/appearance/watch", headers=headers, data=dict(captions=False)).ok)
        self.assertEqual(viewer.request.post(questions_path, headers=marker, data=query).status, 404)
        viewer.reload()
        expect(panel).to_have_count(0)
        self.assertTrue(api.delete(video + "/appearance/watch", headers=headers).ok)

        frame_site = Flask("cueporch-question-embed")

        @frame_site.get("/")
        def embed():
            return f'<iframe title="Council video" src="{origin}/embed/work/{work}"></iframe>'

        embed_site = self.serve(cleanup, frame_site)
        viewer.goto(embed_site)
        frame = viewer.frame_locator("iframe")
        embedded = frame.locator("#videoQuestions")
        embedded.get_by_text("Ask about this video", exact=True).click()
        embedded.get_by_label("Answer language").select_option("es")
        embedded.get_by_label("Your question").fill("¿Qué sucede en el video?")
        embedded.get_by_role("button", name="Ask", exact=True).click()
        expect(embedded.locator("[data-answer-text]")).to_have_text("La sesión pública está comenzando.", timeout=15000)
        embed_key = embedded.get_attribute("data-session")
        self.assertTrue(api.put(video + "/appearance/embed", headers=headers, data=dict(allow_seek=False)).ok)
        embedded.get_by_role("button", name="Ask", exact=True).click()
        expect(embedded.locator("[data-answer-text]")).to_have_text("La sesión pública está comenzando.")
        expect(embedded.get_by_role("button", name="Play from 0:01")).to_have_count(0)

        settings = api.get(settings_path, headers=headers).json()
        settings["settings"]["viewer_questions"] = False
        self.assertTrue(api.put(settings_path, headers=headers | {"X-CuePorch-Team": "1"}, data=settings).ok)
        self.assertEqual(viewer.request.post(questions_path, headers=marker,
                         data=dict(query, session=embed_key)).status, 404)
        settings = api.get(settings_path, headers=headers).json()
        settings["settings"]["viewer_questions"] = True
        self.assertTrue(api.put(settings_path, headers=headers | {"X-CuePorch-Team": "1"}, data=settings).ok)

        self.assertTrue(api.put(video + "/privacy", headers=headers,
                                data=dict(visibility="password", embed="anywhere", password="screening fixture")).ok)
        self.assertEqual(viewer.request.post(questions_path, headers=marker, data=query).status, 404)
        viewer.goto(watch)
        expect(panel).to_have_count(0)
        viewer.get_by_label("Video password").fill("screening fixture")
        viewer.get_by_role("button", name="Watch video").click()
        panel.get_by_text("Ask about this video", exact=True).click()
        panel.get_by_role("button", name="What is this video about?").click()
        expect(panel.locator("[data-answer-text]")).to_have_text("The Council is opening the public session.", timeout=15000)
        locked_key = panel.get_attribute("data-session")
        with patch("library_video_questions.QUESTIONS_PER_MINUTE", 1):
            self.assertEqual(viewer.request.post(questions_path, headers=marker,
                             data=dict(query, session=locked_key)).status, 429)
        print("Video questions: exact transcript sources, decoded seek, embed language, authorization, withdrawal and budget verified.")

        gallery = api.post(origin + "/api/showcases", headers=headers).json()
        path = origin + "/api/showcases/" + gallery["id"]
        gallery = api.put(path, headers=headers, data=dict(slug="protected-caption-gallery", revision=gallery["revision"],
            password="gallery fixture", settings=gallery["settings"] | dict(
                visibility="password", videos=[work], grid_media="players", questions=True))).json()
        self.assertTrue(gallery["settings"]["questions"])
        viewer.goto(origin + gallery["watch"])
        questions = viewer.locator("#showcaseQuestions")
        expect(questions).to_have_count(0)
        viewer.get_by_label("Showcase password").fill("gallery fixture")
        viewer.get_by_role("button", name="Unlock showcase").click()
        questions.get_by_text("Ask AI about this video").click()
        questions.get_by_role("button", name="What is this video about?").click()
        expect(questions.locator("[data-answer-text]")).to_have_text("The Council is opening the public session.")
        expect(questions.get_by_role("list", name="Answer sources")).to_contain_text("The public session starts now.")
        questions.get_by_role("button", name="Play from 0:01").click()
        viewer.wait_for_function("() => document.querySelector('video').currentTime >= 0.99")
        self.assertEqual(256, viewer.locator(".showcase-playlist video").evaluate("video => video.videoWidth"))
        key = questions.get_attribute("data-session")
        self.assertEqual(404, viewer.request.post(questions_path, headers=marker,
                         data=dict(query, session=key)).status)
        showcase_questions = origin + "/api/viewer/showcase-questions"
        self.assertEqual(404, viewer.request.post(showcase_questions, headers=marker,
                         data=dict(query, session=locked_key)).status)
        self.assertTrue(api.put(path, headers=headers, data=gallery | {
            "settings": gallery["settings"] | {"questions": False},
        }).ok)
        self.assertEqual(404, viewer.request.post(showcase_questions, headers=marker,
                         data=dict(query, session=key)).status)
        print("Showcase questions: password gate, decoded citation, session scope and withdrawal verified.")


if __name__ == "__main__":
    unittest.main()
