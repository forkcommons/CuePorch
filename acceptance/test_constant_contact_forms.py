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
import library_constant_contact_forms as forms
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

        def capture(app, tx, database, connection, validate):
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

    def enable_video(self):
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

    def submit(self, path, email, consent=True):
        visitor = self.client.application.test_client()
        headers = {"Sec-Fetch-Dest": "iframe"}
        get_resp = visitor.get(path, headers=headers)
        assert get_resp.status_code == 200
        page = get_resp.get_data(as_text=True)
        tags = re.findall(r'<input[^>]*>', page)
        enrollment_tag = next(tag for tag in tags if 'name="enrollment"' in tag)
        consent_tag = next(tag for tag in tags if 'name="constant_contact_form_consent"' in tag)
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
            data["constant_contact_form_consent"] = consent_token
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
