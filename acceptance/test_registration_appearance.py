"""Exercise one reusable registration design on the three real visitor surfaces."""

from contextlib import ExitStack
import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import re
import secrets
from socketserver import TCPServer
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch
import uuid

from PIL import Image
from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import make_server

from browser import chromium
from installed_simulcast import tls_certificate
from test_team_invitation_delivery import Relay

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


class RegistrationAppearance(unittest.TestCase):
    def test_preset_preview_and_viewer(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            cert, key = tls_certificate(root)
            relay = cleanup.enter_context(TCPServer(("127.0.0.1", 0), Relay))
            relay.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            relay.tls.load_cert_chain(cert, key)
            relay.deliveries = []
            relay.delivered = Event()
            listener = Thread(target=relay.serve_forever, daemon=True)
            listener.start()
            cleanup.callback(listener.join)
            cleanup.callback(relay.shutdown)
            cleanup.enter_context(patch.dict(os.environ, {
                "CUEPORCH_PUBLIC_URL": "https://cueporch-registration.example.test",
                "CUEPORCH_SMTP_HOST": "127.0.0.1", "CUEPORCH_SMTP_PORT": str(relay.server_address[1]),
                "CUEPORCH_SMTP_FROM": "crew@example.test", "CUEPORCH_SMTP_USER": "fixture",
                "CUEPORCH_SMTP_PASSWORD": "mail-credential", "SSL_CERT_FILE": str(cert),
            }))
            cleanup.enter_context(patch("institution.library_worker.start"))
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

            def check_design(view, heading="Register for screenings"):
                expect(view.get_by_role("heading", name=heading)).to_be_visible()
                gate = view.locator(".registration-gate")
                expect(gate).to_have_attribute("data-alignment", "end")
                appearance = gate.evaluate("""
                    node => {
                        const styles = getComputedStyle(node);
                        const overlay = getComputedStyle(node, "::before");
                        const raw = styles.justifyContent;
                        const justify = raw === "flow-end" ? "flex-end" : raw;
                        const card = node.querySelector(".registration-card").getBoundingClientRect();
                        const bounds = node.getBoundingClientRect();
                        const padding = Number.parseFloat(styles.paddingRight);
                        const border = Number.parseFloat(styles.borderRightWidth);
                        const freeSpace = node.clientWidth - card.width - padding - Number.parseFloat(styles.paddingLeft);
                        return {
                            style: [justify, styles.backgroundColor, overlay.backgroundColor, overlay.opacity],
                            trailingGap: bounds.right - card.right - border,
                            padding, freeSpace,
                        };
                    }
                """)
                self.assertEqual(["flex-end", "rgb(15, 49, 50)", "rgb(97, 37, 92)", "0.62"], appearance["style"])
                if appearance["freeSpace"] > 1:
                    self.assertAlmostEqual(appearance["padding"], appearance["trailingGap"], delta=1)
                self.assertEqual(41, view.get_by_role("img", name="Organizer logo").evaluate(
                    "async node => {await node.decode();return node.naturalWidth}"))
                self.assertEqual(80, gate.locator(".registration-background").evaluate(
                    "async node => {await node.decode();return node.naturalWidth}"))

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
                editor.get_by_role("button", name="Add field").click()
                editor.get_by_label("Field type").select_option("hidden")
                editor.get_by_label("Field label").fill("Campaign reference")
                editor.get_by_label("URL parameter after reg_").fill("campaign")
                editor.get_by_label("Default answer").fill("direct audience")
                editor.get_by_role("button", name="Add field").click()
                choice = editor.locator("#registrationFields > li").nth(1)
                choice.get_by_label("Field type").select_option("select")
                choice.get_by_label("Field label").fill("Attendance plan")
                choice.get_by_label("Choices, one per line").fill("Gallery\nOnline")
                editor.get_by_role("button", name="Add field").click()
                terms = editor.locator("#registrationFields > li").nth(2)
                terms.get_by_label("Field type").select_option("checkbox")
                terms.get_by_label("Field label").fill("I have read the screening terms")
                terms.get_by_label("Terms link").fill("https://gallery.example.test/screening-terms")
                editor.get_by_label("Language to edit").select_option("es")
                editor.get_by_label("Show this language to viewers").check()
                editor.get_by_label("Translated heading").fill("Inscripción para proyecciones")
                editor.get_by_label("Translation for Attendance plan").fill("Modalidad")
                editor.get_by_label("Translation for choice Gallery").fill("En sala")
                editor.get_by_label("Translation for choice Online").fill("En línea")
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
                editor.get_by_label("Preview language").select_option("es")
                editor.get_by_role("button", name="Preview registration").click()
                expect(preview.get_by_role("heading", name="Inscripción para proyecciones")).to_be_visible()
                expect(preview.get_by_label("Modalidad").get_by_role("option", name="En sala")).to_have_attribute("value", "Gallery")
                expect(preview.get_by_role("link", name="Leer las condiciones")).to_have_attribute(
                    "href", "https://gallery.example.test/screening-terms")
                editor.get_by_label("Preview language").select_option("en")
                editor.get_by_label("Preview screen").select_option("sign-on")
                editor.get_by_role("button", name="Preview registration").click()
                check_design(preview, "Already registered")
                expect(preview.get_by_role("button", name="Send code")).to_be_visible()
                editor.get_by_label("Form preview device").select_option("desktop")
                self.assertEqual(800, frame.evaluate("node => node.getBoundingClientRect().width"))
                check_design(preview, "Already registered")
                self.assertEqual([], relay.deliveries)
                self.assertFalse(send("GET", endpoint)["settings"]["enabled"])
                self.assertEqual(403, api.post("/api/registration/preview", json={}).status_code)
                save_button = editor.get_by_role("button", name="Save registration")
                save_button.scroll_into_view_if_needed()
                save_button.click()
                expect(editor.locator("#registrationStatus")).to_have_text("Registration saved.")

                settings = send("GET", endpoint)
                hidden_field = settings["settings"]["fields"][0]["id"]
                invalid = settings["settings"] | {"appearance": settings["settings"]["appearance"] | {"opacity": 101}}
                self.assertEqual(422, api.put(endpoint, headers=auth,
                    json={"settings": invalid, "revision": settings["revision"]}).status_code)
                expect(editor.get_by_label("Overlay opacity")).to_have_value("62")

                page.goto(base + "/operator/showcases?select=" + gallery["id"])
                page.get_by_role("button", name="Registration", exact=True).click()
                editor.get_by_label("Saved registration preset").select_option("Arts screenings")
                editor.get_by_label("Form layout").select_option("steps")
                editor.get_by_role("button", name="Save registration").click()
                expect(editor.locator("#registrationStatus")).to_have_text("Registration saved.")

                with browser.new_context(locale="es-MX") as local:
                    translated = local.new_page()
                    translated.goto(base + gallery["watch"])
                    expect(translated.locator(".registration-gate")).to_have_attribute("lang", "es")
                    expect(translated.get_by_role("heading", name="Inscripción para proyecciones")).to_be_visible()
                    expect(translated.get_by_text("Paso 1 de 2")).to_be_visible()
                    translated.get_by_label("Correo electrónico").fill("appearance@example.test")
                    translated.get_by_role("button", name="Continuar").click()
                    expect(translated.get_by_text("Paso 2 de 2")).to_be_visible()
                    translated.get_by_label("Modalidad").select_option(label="En sala")
                    translated.get_by_label("Idioma", exact=True).select_option("en")
                    expect(translated.get_by_label("Attendance plan")).to_have_value("Gallery")
                    expect(translated.get_by_text("Step 2 of 2")).to_be_visible()
                    translated.get_by_label("Language", exact=True).select_option("es")
                    translated.get_by_role("button", name="¿Ya te registraste?").click()
                    expect(translated.get_by_role("heading", name="Ya registrado")).to_be_visible()
                    expect(translated.get_by_role("button", name="Enviar código")).to_be_visible()

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
                        for index, (path, playable) in enumerate(targets):
                            campaign = "?reg_campaign=%3Darts-invite" if viewport["width"] == 1280 else ""
                            visitor.goto(base + path + campaign)
                            if path == targets[0][0]:
                                expect(visitor.locator(".registration-gate")).to_have_count(0)
                                visitor.get_by_label("Video password").fill(pw)
                                visitor.get_by_role("button", name="Watch video").click()
                            check_design(visitor)
                            expect(visitor.get_by_text("Campaign reference", exact=True)).to_have_count(0)
                            expect(visitor.locator("video")).to_have_count(0)
                            email = f"visitor-{viewport['width']}-{index}@example.test"
                            visitor.get_by_label("Email", exact=True).fill(email)
                            if index == 1:
                                visitor.get_by_role("button", name="Continue").click()
                            visitor.get_by_role("button", name="Register and watch").click()
                            expect(visitor.locator(".registration-gate")).to_have_count(0)
                            if playable:
                                self.assertEqual(320, visitor.locator("video").evaluate(
                                    "async v => {v.muted=true; await v.play(); return v.videoWidth;}"))
                            else:
                                expect(visitor.locator("#eventStatus")).to_have_text("Scheduled")
                                self.assertTrue(visitor.locator("#eventLobby").get_attribute("data-viewer-grant"))

                            visitor.goto(base + path)
                            if index == 0:
                                visitor.get_by_label("Video password").fill(pw)
                                visitor.get_by_role("button", name="Watch video").click()
                            visitor.get_by_role("button", name="Already registered?").click()
                            check_design(visitor, "Already registered")
                            visitor.get_by_label("Email", exact=True).fill(email.upper())
                            relay.delivered.clear()
                            visitor.get_by_role("button", name="Send code").click()
                            expect(visitor.get_by_label("Registration code")).to_be_visible()
                            self.assertTrue(relay.delivered.wait(5), "No code reached the TLS fixture")
                            message = relay.deliveries[-1]
                            self.assertEqual(email, message["To"])
                            code = re.search(r"\b[0-9]{8}\b", message.get_content())[0]
                            details = {"registration_action": "verify", "return_code": code,
                                       "return_challenge": visitor.locator("[name=return_challenge]").input_value(),
                                       "enrollment": visitor.locator("[name=enrollment]").input_value()}
                            if index == 0:
                                other = guests.new_page()
                                other.goto(base + gallery["watch"])
                                wrong_scope = details | {"enrollment": other.locator("[name=enrollment]").input_value()}
                                self.assertEqual(422, other.request.post(other.url, form=wrong_scope).status)
                                other.close()
                                visitor.get_by_label("Registration code").fill("00000000" if code != "00000000" else "99999999")
                                visitor.get_by_role("button", name="Open registration").click()
                                expect(visitor.get_by_role("alert")).to_have_text("The code was invalid or expired.")
                            visitor.get_by_label("Registration code").fill(code)
                            visitor.get_by_role("button", name="Open registration").click()
                            expect(visitor.locator(".registration-gate")).to_have_count(0)
                            if playable:
                                self.assertEqual(320, visitor.locator("video").evaluate(
                                    "async v => {v.muted=true; await v.play(); return v.videoWidth;}"))
                            else:
                                self.assertTrue(visitor.locator("#eventLobby").get_attribute("data-viewer-grant"))
                            self.assertEqual(422, visitor.request.post(base + path, form=details).status)

                self.assertEqual(6, len(relay.deliveries))
                for index, form_path in enumerate((endpoint, f"/api/showcases/{gallery['id']}/registration",
                                                    f"/api/live-events/{event['id']}/registration")):
                    answers = {row["email"]: row["answers"][hidden_field] for row in send("GET", form_path)["registrants"]}
                    self.assertEqual({f"visitor-1280-{index}@example.test": "=arts-invite",
                                      f"visitor-390-{index}@example.test": "direct audience"}, answers)
                    export = api.get(form_path + "/export", headers=auth)
                    self.assertEqual(200, export.status_code)
                    rows = {row["email"]: row[hidden_field] for row in csv.DictReader(StringIO(export.get_data(as_text=True)))}
                    self.assertEqual("'=arts-invite", rows[f"visitor-1280-{index}@example.test"])
                print("Hidden campaign values, defaults and escaped CSV verified on all three registration forms.")
                with browser.new_context() as visitor_context:
                    visitor = visitor_context.new_page()
                    video_path = base + f"/watch/work/{work}"
                    visitor.goto(video_path)
                    visitor.get_by_label("Video password").fill(pw)
                    visitor.get_by_role("button", name="Watch video").click()
                    visitor.get_by_label("Email", exact=True).fill("old-consent@example.test")
                    visitor.get_by_role("button", name="Register and watch").click()
                    expect(visitor.locator("video")).to_be_visible()
                    current = send("GET", endpoint)
                    requirement = dict(id="field_new_consent", type="checkbox", label="I agree to the new terms", required=True,
                                       link="https://gallery.example.test/new-terms")
                    send("PUT", endpoint, {"revision": current["revision"],
                        "settings": current["settings"] | {"fields": [requirement], "translations": {}}})
                    visitor.goto(video_path)
                    visitor.get_by_label("Video password").fill(pw)
                    visitor.get_by_role("button", name="Watch video").click()
                    visitor.get_by_role("button", name="Already registered?").click()
                    visitor.get_by_label("Email", exact=True).fill("old-consent@example.test")
                    relay.delivered.clear()
                    visitor.get_by_role("button", name="Send code").click()
                    expect(visitor.get_by_text("If that address has a registration", exact=False)).to_be_visible()
                    self.assertFalse(relay.delivered.wait(1))
                    visitor.get_by_role("button", name="Back to registration").click()
                    expect(visitor.get_by_label("I agree to the new terms")).to_be_visible()
                    expect(visitor.get_by_role("link", name="Read the terms")).to_have_attribute(
                        "href", "https://gallery.example.test/new-terms")
                    expect(visitor.locator("video")).to_have_count(0)
                    visitor.get_by_label("Email", exact=True).fill("old-consent@example.test")
                    visitor.get_by_label("I agree to the new terms").check()
                    visitor.get_by_role("button", name="Register and watch").click()
                    expect(visitor.locator("video")).to_be_visible()
                roster = f"/api/live-events/{event['id']}/{event['occurrences'][0]['id']}/registration/export"
                exported = api.get(roster, headers=auth)
                self.assertEqual(200, exported.status_code)
                self.assertEqual(3, len(exported.get_data(as_text=True).splitlines()))


if __name__ == "__main__":
    unittest.main()
