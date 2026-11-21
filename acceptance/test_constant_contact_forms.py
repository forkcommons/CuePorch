from contextlib import ExitStack, closing
import html
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import constant_contact_crm as contact
import library_list_registration as forms
import workspace_oauth
from institution import create_app
from test_marketo_connection import _resp

API_BASE = contact.API_BASE
LIST_ID = "11111111-1111-1111-1111-111111111111"
ACTIVITY_ID = "22222222-2222-2222-2222-222222222222"
PERSON_ID = "33333333-3333-3333-3333-333333333333"
OPERATOR = "t" * 48
FIXTURE_CODE = "cc-forms-fake"


class FakeProvider:
    def __init__(self):
        self.creates = []
        self.adds = []
        self.polls = 0
        self.complete = False

    def request(self, method, url, **kwargs):
        method = method.upper()
        if method == "POST" and url == contact.OAUTH.token_url:
            assert kwargs["params"]["code"] == FIXTURE_CODE
            return _resp({
                "access_token": "a1",
                "refresh_token": "r1",
                "token_type": "Bearer",
                "scope": contact.OAUTH.scope,
                "expires_in": 3600,
            })

        assert kwargs.get("headers", {}).get("Authorization") == "Bearer a1"

        if method == "GET" and url == f"{API_BASE}/contact_lists":
            if kwargs["params"] == {"cursor": "test-next"}:
                return _resp({
                    "lists": [{
                        "list_id": LIST_ID,
                        "name": "Station audience",
                    }],
                })
            assert set(kwargs["params"]) == {"limit", "status", "channel_type"}
            return _resp({
                "lists": [{
                    "list_id": "44444444-4444-4444-4444-444444444444",
                    "name": "Other list",
                }],
                "_links": {"next": {"href": "/v3/contact_lists?cursor=test-next"}},
            })

        if method == "GET" and url == f"{API_BASE}/contact_lists/{LIST_ID}":
            return _resp({"list_id": LIST_ID, "name": "Station audience"})

        if method == "GET" and url == f"{API_BASE}/contacts":
            email = kwargs["params"]["email"]
            if email == "existing@example.test":
                return _resp({"contacts": [{
                    "contact_id": PERSON_ID,
                    "email_address": {
                        "address": email,
                        "permission_to_send": "explicit",
                        "confirm_status": "confirmed",
                    },
                    "list_memberships": [LIST_ID] if self.complete else [],
                }]})
            return _resp({"contacts": []})

        if method == "POST" and url == f"{API_BASE}/contacts":
            body = kwargs["json"]
            self.creates.append(body)
            address = body["email_address"]["address"]
            return _resp({
                "contact_id": PERSON_ID,
                "email_address": {"address": address, "permission_to_send": "explicit"},
                "list_memberships": body.get("list_memberships", []),
            })

        if method == "POST" and url == f"{API_BASE}/activities/add_list_memberships":
            self.adds.append(kwargs["json"])
            return _resp({"activity_id": ACTIVITY_ID})

        if method == "GET" and url == f"{API_BASE}/activities/{ACTIVITY_ID}":
            self.polls += 1
            return _resp({
                "activity_id": ACTIVITY_ID,
                "state": "completed" if self.complete else "initialized",
            })

        raise AssertionError(f"unknown route: {method} {url}")


class ConstantContactFormsTest(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())

        root = Path(stack.enter_context(TemporaryDirectory()))
        (root / "objects").mkdir()
        (root / "manifest.json").write_text(
            json.dumps({"institution": "station", "schema": 1, "works": []})
        )
        self.database = root / "database.sqlite3"

        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "CUEPORCH_PUBLIC_URL": "",
                    "CUEPORCH_CONSTANT_CONTACT_CLIENT_ID": "cid",
                    "CUEPORCH_CONSTANT_CONTACT_CLIENT_SECRET": "sec",
                },
            )
        )
        stack.enter_context(
            patch("institution.library_worker.start", return_value=lambda: None)
        )

        self.provider = FakeProvider()
        stack.enter_context(
            patch.object(
                workspace_oauth,
                "provider_request",
                side_effect=self.provider.request,
            )
        )

        def capture(app, tx, database, connection, validate, provider=forms.CONSTANT_CONTACT):
            if provider == forms.CONSTANT_CONTACT:
                self.runner = (tx, connection, validate)
            return lambda: None

        stack.enter_context(patch.object(forms, "start", side_effect=capture))

        app = create_app(root, self.database, OPERATOR, "station")

        for name, extension in app.extensions.items():
            if name.startswith("stop_") and callable(extension):
                stack.callback(extension)

        self.client = app.test_client()
        self.auth = {
            "Authorization": f"Bearer {OPERATOR}",
            "X-CuePorch-Integration": "1",
        }

        response = self.client.post(
            "/api/integrations/constant-contact/connect",
            json={},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200)
        authorization_url = response.get_json()["authorization_url"]
        state = parse_qs(urlsplit(authorization_url).query)["state"][0]

        response = self.client.get(
            "/integration/oauth/constant-contact/callback",
            query_string={"state": state, "code": FIXTURE_CODE},
        )
        self.assertEqual(response.status_code, 303)

        self.now = float(int(time.time()) + 1)
        self.stop = Event()

    def tick(self):
        return forms.run_one(
            *self.runner,
            self.stop,
            clock=lambda: self.now,
        )

    def enable_video(self, *, configure_delivery=True):
        fixture_path = self.database.parent / "fixture.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
                "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(fixture_path),
            ],
            check=True,
        )
        file_bytes = fixture_path.read_bytes()
        upload_resp = self.client.post(
            "/api/library/uploads",
            headers=self.auth,
            data={
                "file": (BytesIO(file_bytes), "fixture.mp4"),
                "upload_id": str(uuid4()),
                "privacy": json.dumps({"visibility": "public", "embed": "anywhere"}),
            },
        )
        assert upload_resp.status_code in (200, 201)
        work_id = upload_resp.get_json()["id"]
        reg_path = f"/api/library/work/{work_id}/registration"
        reg_resp = self.client.get(reg_path, headers=self.auth)
        assert reg_resp.status_code == 200
        reg = reg_resp.get_json()
        settings = dict(reg.get("settings") or {})
        settings.update({
            "enabled": True,
            "first_name": "required",
            "last_name": "required",
        })
        reg_put = self.client.put(
            reg_path,
            headers=self.auth,
            json={"revision": reg["revision"], "settings": settings},
        )
        assert reg_put.status_code in (200, 204)
        outbound_path = reg_path + "/constant-contact/outbound"
        if configure_delivery:
            outbound_put = self.client.put(
                outbound_path,
                headers=self.auth,
                json={"list_id": LIST_ID},
            )
            assert outbound_put.status_code in (200, 204)
            outbound_get = self.client.get(outbound_path, headers=self.auth)
            assert outbound_get.status_code == 200
            assert outbound_get.get_json().get("enabled") is True
        return f"/embed/work/{work_id}", outbound_path

    def submit(self, path, email, consent=True, *, consent_field="constant_contact_form_consent"):
        visitor = self.client.application.test_client()
        headers = {"Sec-Fetch-Dest": "iframe"}
        get_resp = visitor.get(path, headers=headers)
        assert get_resp.status_code == 200
        page = get_resp.get_data(as_text=True)
        tags = re.findall(r'<input[^>]*>', page)
        enrollment_tag = next(tag for tag in tags if 'name="enrollment"' in tag)
        consent_tag = next(tag for tag in tags if f'name="{consent_field}"' in tag)
        enrollment = html.unescape(re.search(r'value="([^"]+)"', enrollment_tag).group(1))
        consent_token = html.unescape(re.search(r'value="([^"]+)"', consent_tag).group(1))
        assert not re.search(r"\bchecked\b", consent_tag, re.I)
        assert not re.search(r"\brequired\b", consent_tag, re.I)
        data = {
            "enrollment": enrollment,
            "email": email,
            "first_name": "Test",
            "last_name": "Viewer",
        }
        if consent:
            data[consent_field] = consent_token
        post_resp = visitor.post(path, headers=headers, data=data)
        assert post_resp.status_code == 200
        assert "<video" in post_resp.get_data(as_text=True)


    def test_registration_delivery_lifecycle(self):
        embed_path, outbound_path = self.enable_video()

        def receipt():
            return self.client.get(outbound_path, headers=self.auth).get_json()["states"]

        def policy(required):
            r = self.client.put(
                "/api/policy/event_contact_sync",
                json={"required": required},
                headers=self.auth,
            )
            self.assertEqual(r.status_code, 200)

        unauth = self.client.get(outbound_path)
        self.assertEqual(unauth.status_code, 403)

        self.submit(embed_path, "nooptin@example.test", consent=False)
        self.assertFalse(self.tick())
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.provider.adds, [])
        self.assertEqual(self.provider.polls, 0)

        self.submit(embed_path, "fresh@example.test", consent=True)
        self.assertTrue(self.tick())
        self.assertEqual(receipt(), {"sent": 1})
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(
            self.provider.creates[0],
            {
                "email_address": {
                    "address": "fresh@example.test",
                    "permission_to_send": "explicit",
                },
                "first_name": "Test",
                "last_name": "Viewer",
                "create_source": "Contact",
                "list_memberships": [LIST_ID],
            },
        )

        self.submit(embed_path, "fresh@example.test", consent=True)
        self.assertFalse(self.tick())
        self.assertEqual(receipt(), {"sent": 1})

        policy(True)
        writes_before = (
            len(self.provider.creates),
            len(self.provider.adds),
            self.provider.polls,
        )
        self.submit(embed_path, "existing@example.test", consent=True)
        self.assertEqual(receipt(), {"sent": 1, "waiting": 1})
        self.assertFalse(self.tick())
        self.assertEqual(
            (
                len(self.provider.creates),
                len(self.provider.adds),
                self.provider.polls,
            ),
            writes_before,
        )

        policy(False)
        self.assertTrue(self.tick())
        expected_add = {
            "source": {"contact_ids": [PERSON_ID]},
            "list_ids": [LIST_ID],
        }
        self.assertEqual(self.provider.adds, [expected_add])
        self.assertEqual(receipt(), {"sent": 1, "submitted": 1})

        self.assertFalse(self.tick())
        self.now += 31
        self.assertTrue(self.tick())
        self.assertEqual(self.provider.polls, 1)
        self.assertEqual(receipt(), {"sent": 1, "submitted": 1})
        self.assertEqual(len(self.provider.adds), 1)

        self.provider.complete = True
        self.now += 31
        self.assertTrue(self.tick())
        self.assertEqual(receipt(), {"sent": 2})
        self.assertEqual(len(self.provider.adds), 1)

        policy(True)
        self.submit(embed_path, "canceled@example.test", consent=True)
        self.assertEqual(receipt(), {"sent": 2, "waiting": 1})

        r = self.client.delete(outbound_path, json={}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertFalse((r.get_json() or {}).get("enabled", False))

        policy(False)

        writes_before = (
            len(self.provider.creates),
            len(self.provider.adds),
            self.provider.polls,
        )
        self.assertFalse(self.tick())
        self.assertEqual(
            (
                len(self.provider.creates),
                len(self.provider.adds),
                self.provider.polls,
            ),
            writes_before,
        )

        with closing(sqlite3.connect(self.database)) as conn:
            rows = conn.execute(
                "SELECT email, first_name,last_name,due "
                "FROM library_constant_contact_form_delivery "
                "WHERE state='canceled'"
            ).fetchall()
        self.assertEqual(rows, [(None, None, None, None)])

    def enable_showcase(self):
        fixture = self.database.parent / "fixture.mp4"
        upload = self.client.post(
            "/api/library/uploads",
            data={
                "file": (BytesIO(fixture.read_bytes()), "gallery.mp4"),
                "upload_id": str(uuid4()),
                "privacy": json.dumps({"visibility": "public", "embed": "anywhere"}),
            },
            headers=self.auth,
            content_type="multipart/form-data",
        )
        self.assertIn(upload.status_code, (200, 201))
        member_id = upload.get_json()["id"]

        response = self.client.post("/api/showcases", json={}, headers=self.auth)
        self.assertIn(response.status_code, (200, 201))
        sid = response.get_json()["id"]

        showcase = self.client.get(f"/api/showcases/{sid}", headers=self.auth)
        self.assertEqual(showcase.status_code, 200)
        current = showcase.get_json()
        updated = self.client.put(
            f"/api/showcases/{sid}",
            json={
                "slug": "constant-contact-gallery",
                "revision": current["revision"],
                "settings": current["settings"] | {
                    "title": "Constant Contact gallery",
                    "visibility": "public",
                    "videos": [member_id],
                },
            },
            headers=self.auth,
        )
        self.assertEqual(updated.status_code, 200)

        registration = self.client.get(f"/api/showcases/{sid}/registration", headers=self.auth)
        self.assertEqual(registration.status_code, 200)
        current_registration = registration.get_json()
        registration_updated = self.client.put(
            f"/api/showcases/{sid}/registration",
            json={
                "revision": current_registration["revision"],
                "settings": current_registration["settings"] | {
                    "enabled": True,
                    "first_name": "required",
                    "last_name": "required",
                },
            },
            headers=self.auth,
        )
        self.assertEqual(registration_updated.status_code, 200)
        return sid


    def play_in_browser(self, browser, base, path, email, consent, *, embed=False):
        from playwright.sync_api import expect

        context = browser.new_context()
        try:
            page = context.new_page()
            if embed:
                page.goto(base + "/watch")
                page.set_content(
                    f'<iframe src="{base}{path}" width="640" height="360"></iframe>'
                )
                target = page.frame_locator("iframe")
            else:
                page.goto(base + path)
                target = page

            expect(target.locator("video")).to_have_count(0)

            target.locator("input[name=email]").fill(email)
            target.locator("input[name=first_name]").fill("Test")
            target.locator("input[name=last_name]").fill("Viewer")

            consent_input = target.locator(
                "input[name=constant_contact_form_consent]"
            )
            if consent is None:
                expect(consent_input).to_have_count(0)
            else:
                expect(consent_input).not_to_be_checked()
                if consent:
                    consent_input.check()

            target.get_by_role("button", name="Register and watch").click()

            video = target.locator("video")
            expect(video).to_be_visible()

            width, height = video.evaluate(
                """
                async (v) => {
                    v.muted = true;
                    await v.play();
                    const start = v.currentTime;

                    if (typeof v.requestVideoFrameCallback === 'function') {
                        await new Promise((resolve, reject) => {
                            let settled = false;
                            const timeout = setTimeout(() => {
                                if (settled) return;
                                settled = true;
                                reject(new Error('Timed out waiting for video frame after 5s'));
                            }, 5000);

                            v.requestVideoFrameCallback(() => {
                                if (settled) return;
                                settled = true;
                                clearTimeout(timeout);
                                resolve();
                            });
                        });
                    } else {
                        await new Promise((resolve, reject) => {
                            let settled = false;
                            let interval;
                            const timeout = setTimeout(() => {
                                if (settled) return;
                                settled = true;
                                clearInterval(interval);
                                reject(new Error('Timed out waiting for video currentTime to advance after 5s'));
                            }, 5000);

                            interval = setInterval(() => {
                                if (settled) return;
                                if (v.currentTime > start) {
                                    settled = true;
                                    clearInterval(interval);
                                    clearTimeout(timeout);
                                    resolve();
                                }
                            }, 50);
                        });
                    }

                    return [v.videoWidth, v.videoHeight];
                }
                """
            )

            assert (width, height) == (320, 180), (
                f"Unexpected video dimensions {width}x{height}, expected 320x180"
            )
            return width, height
        finally:
            context.close()


    def test_operator_delivery_and_browser_playback(self):
        from browser import chromium
        from playwright.sync_api import expect, sync_playwright
        from test_drive_import import QuietHandler, service_worker
        from werkzeug.serving import make_server

        embed_path, outbound_path = self.enable_video(configure_delivery=False)
        video_id = embed_path.rsplit("/", 1)[-1]
        sid = self.enable_showcase()
        gallery = "/showcases/constant-contact-gallery"
        video_path = f"/operator/videos/{video_id}/interactivity"
        show_path = f"/operator/showcases?select={sid}"

        server = make_server(
            "127.0.0.1", 0, self.client.application,
            request_handler=QuietHandler, threaded=True,
        )
        base = f"http://127.0.0.1:{server.server_port}"

        with service_worker(server), sync_playwright() as pw, chromium(pw) as browser:
            staff = browser.new_page()
            staff.add_init_script(
                "sessionStorage.setItem('cueporchOperator', %s)" % json.dumps(OPERATOR)
            )

            def open_editor(path, showcase=False):
                staff.goto(f"{base}{path}")
                if showcase:
                    staff.click("#showcaseRegistration")

            def configure():
                staff.click("#registrationConstantContactLoad")
                expect(
                    staff.locator("#registrationConstantContactList option").filter(
                        has_text="Other list"
                    )
                ).to_have_count(1)
                staff.click("#registrationConstantContactMore")
                staff.select_option("#registrationConstantContactList", LIST_ID)
                staff.click("#registrationConstantContactSave")
                expect(
                    staff.locator("#registrationConstantContactStatus")
                ).to_have_text("Constant Contact delivery is enabled.")

            open_editor(video_path)
            configure()
            open_editor(video_path)
            expect(
                staff.locator("#registrationConstantContactList")
            ).to_have_value(LIST_ID)

            open_editor(show_path, True)
            configure()
            open_editor(show_path, True)
            expect(
                staff.locator("#registrationConstantContactList")
            ).to_have_value(LIST_ID)

            self.play_in_browser(
                browser, base, embed_path, "declined@example.test", False, embed=True
            )
            self.now = time.time() + 1
            assert not self.tick()
            assert self.provider.creates == []

            self.play_in_browser(
                browser, base, embed_path, "optin@example.test", True, embed=True
            )
            self.play_in_browser(
                browser, base, gallery, "gallery@example.test", True
            )
            self.now = time.time() + 1
            self.tick()
            self.tick()
            assert sorted(
                c["email_address"]["address"] for c in self.provider.creates
            ) == sorted(["optin@example.test", "gallery@example.test"])
            assert all(
                c["list_memberships"] == [LIST_ID]
                for c in self.provider.creates
            )

            staff.click("#registrationConstantContactStop")
            expect(
                staff.locator("#registrationConstantContactStatus")
            ).to_have_text("Constant Contact delivery is off.")
            before = len(self.provider.creates)
            self.play_in_browser(
                browser, base, gallery, "after-stop@example.test", None
            )
            self.now = time.time() + 1
            assert not self.tick()
            assert len(self.provider.creates) == before

            assert self.client.get(
                outbound_path, headers=self.auth
            ).get_json()["enabled"]
