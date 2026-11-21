import html
import json
import re
import time
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from test_mailchimp_connection import MailchimpProvider
from test_marketo_connection import _resp

from institution import create_app


class RegistrationProvider(MailchimpProvider):
    def __init__(self, challenge=None, fault=None):
        super().__init__(challenge=challenge, fault=fault)
        self.writes = []

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        member_prefix = "/3.0/lists/listsafe1/members/"
        if (
            parsed.scheme == "https"
            and parsed.netloc == "api.mailchimp.com"
            and parsed.path.startswith(member_prefix)
            and len(parsed.path) > len(member_prefix)
        ):
            headers = kwargs.get("headers") or {}
            assert headers.get("Authorization") == "Bearer fixture-mailchimp-access"

            if method == "GET":
                response = _resp(
                    {
                        "type": "https://mailchimp.com/developer/marketing/docs/errors/",
                        "title": "Resource Not Found",
                        "status": 404,
                        "detail": "The requested resource could not be found.",
                        "instance": "fixture",
                    }
                )
                response.status_code = 404
                response._content_consumed = True
                return response

            if method == "PUT":
                payload = kwargs.get("json")
                assert payload is not None
                self.writes.append(payload)
                member_hash = parsed.path.rsplit("/", 1)[-1]
                return _resp(
                    {
                        "id": member_hash,
                        "list_id": "listsafe1",
                        "email_address": payload.get("email_address"),
                        "status": "pending",
                    }
                )

            raise AssertionError("unexpected member method: " + method)

        return super().request(method, url, **kwargs)


class BrowserRegistrationProvider(RegistrationProvider):
    def request(self, method, url, **kwargs):
        parts = urlsplit(url)
        base = "/3.0/lists/listsafe2"
        if parts.netloc != "api.mailchimp.com" or not (
            parts.path == base or parts.path.startswith(base + "/")
        ):
            return super().request(method, url, **kwargs)
        assert (kwargs.get("headers") or {}).get(
            "Authorization"
        ) == "Bearer fixture-mailchimp-access"
        path = parts.path[len("/3.0") :]
        if method == "GET" and path == "/lists/listsafe2":
            return _resp(
                {
                    "id": "listsafe2",
                    "name": "Station audience",
                    "double_optin": True,
                    "marketing_permissions": False,
                }
            )
        if method == "GET" and path == "/lists/listsafe2/merge-fields":
            params = kwargs.setdefault("params", {})
            offset = int(params.get("offset", 0))
            assert offset in (0, 1)
            if offset == 0:
                mf = {
                    "merge_id": 1,
                    "tag": "TOPIC",
                    "name": "Topic",
                    "type": "text",
                    "required": False,
                }
            else:
                mf = {
                    "merge_id": 2,
                    "tag": "GENRE",
                    "name": "Genre",
                    "type": "dropdown",
                    "required": True,
                    "options": {"choices": ["Documentary", "Experimental"]},
                }
            return _resp(
                {"list_id": "listsafe2", "total_items": 2, "merge_fields": [mf]}
            )
        if path.startswith("/lists/listsafe2/members"):
            resp = super().request(
                method,
                url.replace("/lists/listsafe2/members/", "/lists/listsafe1/members/"),
                **kwargs,
            )
            if method == "PUT" and 200 <= resp.status_code < 300:
                data = resp.json()
                data["list_id"] = "listsafe2"
                return _resp(data)
            return resp
        raise AssertionError(f"Unknown listsafe2 path: {parts.path}")


class MailchimpRegistrationTest(unittest.TestCase):
    def test_mailchimp_registration_delivery(self):
        self.run_rehearsal(RegistrationProvider(), self.exercise)

    def test_mailchimp_delivery_controls(self):
        self.run_rehearsal(
            BrowserRegistrationProvider(), self.exercise_browser, public_url=""
        )

    def test_mailchimp_delivery_controls_engagement(self):
        self.run_rehearsal(
            BrowserRegistrationProvider(),
            lambda app, operator, provider: self.exercise_browser(
                app, operator, provider, engagement=True
            ),
            public_url="",
        )

    def run_rehearsal(self, provider, exercise, *, public_url="http://localhost"):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps({"institution": "station", "schema": 1, "works": []}),
                encoding="utf-8",
            )
            database = root / "workspace.sqlite"

            cleanup.enter_context(
                patch.dict(
                    "os.environ",
                    {
                        "CUEPORCH_PUBLIC_URL": public_url,
                        "CUEPORCH_MAILCHIMP_CLIENT_ID": "cid",
                        "CUEPORCH_MAILCHIMP_CLIENT_SECRET": "sec",
                    },
                )
            )
            cleanup.enter_context(
                patch("institution.library_worker.start", return_value=lambda: None)
            )
            cleanup.enter_context(
                patch("workspace_oauth.provider_request", side_effect=provider.request)
            )
            cleanup.enter_context(patch("event_peer.host_available", return_value=True))

            operator = "t" * 48
            app = create_app(root, database, operator, "station")
            for name, value in app.extensions.items():
                if name.startswith("stop_") and callable(value):
                    cleanup.callback(value)

            headers = {
                "Authorization": "Bearer " + operator,
                "X-CuePorch-Integration": "1",
            }
            self.connect_mailchimp(app.test_client(), provider, headers)
            exercise(app, operator, provider)

    def connect_mailchimp(self, client, provider, headers):
        response = client.post(
            "/api/integrations/mailchimp/connect",
            headers=headers,
            json={},
        )
        self.assertEqual(response.status_code, 200, response.data)
        authorization_url = response.get_json()["authorization_url"]
        parts = urlsplit(authorization_url)
        query = parse_qs(parts.query)
        provider.challenge = query["code_challenge"][0]
        state = query["state"][0]
        response = client.get(
            "/integration/oauth/mailchimp/callback",
            query_string={"state": state, "code": "demo-code"},
        )
        self.assertEqual(response.status_code, 303, response.data)
        self.assertEqual(provider.exchanges, 1)

    def create_webinar(self, client, hdr, *, engagement=False):
        start = datetime.now(UTC) + timedelta(minutes=6)
        end = start + timedelta(hours=1)
        schedule = {
            "timezone": "UTC",
            "repeat": "none",
            "start": start.strftime("%Y-%m-%dT%H:%M"),
            "end": end.strftime("%Y-%m-%dT%H:%M"),
        }
        r = client.post(
            "/api/live-events",
            json={
                "title": "Mailchimp Webinar",
                "slug": "mailchimp-webinar",
                "visibility": "public",
                "type": "webinar",
                "latency": "standard",
                "save_replay": False,
                "schedule": schedule,
            },
            headers=hdr,
        )
        assert r.status_code in (200, 201), r.data
        ev = r.get_json()
        uid = ev["id"]
        opens = ev["occurrences"][0]["id"]

        r = client.get(f"/api/live-events/{uid}/registration", headers=hdr)
        body = r.get_json()
        body["settings"]["enabled"] = True
        if engagement:
            body["settings"]["attendance_analytics"] = True
        body["settings"]["fields"] = [
            {
                "id": "field_genre",
                "type": "select",
                "label": "Genre",
                "required": True,
                "options": ["Documentary", "Experimental"],
            }
        ]
        r = client.put(f"/api/live-events/{uid}/registration", json=body, headers=hdr)
        assert r.status_code in (200, 204), r.data

        return uid, opens

    def wait_for_sent(self, client, headers, uid, opens):
        deadline = time.monotonic() + 5
        states = None
        while time.monotonic() < deadline:
            r = client.get(
                f"/api/live-events/{uid}/{opens}/registration/mailchimp/outbound",
                headers=headers,
            )
            self.assertEqual(r.status_code, 200)
            states = r.get_json()["states"]
            if states == {"sent": 1}:
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for sent state; final states: {states!r}")

    def exercise(self, app, operator, provider):
        client = app.test_client()
        hdr = {"Authorization": "Bearer " + operator, "X-CuePorch-Integration": "1"}
        uid, opens = self.create_webinar(client, hdr)
        url = f"/api/live-events/{uid}/{opens}/registration/mailchimp/outbound"

        r = client.put(url, json={"audience": "listsafe1"}, headers=hdr)
        assert r.status_code == 422, r.data
        r = client.put(
            url,
            json={"audience": "listsafe1", "mapping": {"field_genre": "GENRE"}},
            headers=hdr,
        )
        assert r.status_code == 200, r.data

        event_path = f"/events/mailchimp-webinar?occurrence={opens}"

        def submit(email, opt_in, genre="Documentary"):
            visitor = app.test_client()
            page = visitor.get(event_path).get_data(as_text=True)
            enroll = html.unescape(
                re.search(r'name="enrollment" value="([^"]+)"', page).group(1)
            )
            consent_m = re.search(r'name="mailchimp_consent" value="([^"]+)"', page)
            form = {
                "email": email,
                "first_name": "Tester",
                "last_name": "User",
                "field_genre": genre,
                "enrollment": enroll,
            }
            if opt_in:
                assert consent_m is not None, "missing mailchimp_consent input"
                form["mailchimp_consent"] = html.unescape(consent_m.group(1))
            r = visitor.post(event_path, data=form)
            assert r.status_code in (200, 302), r.data

        def receipt():
            r = client.get(url, headers=hdr)
            assert r.status_code == 200, r.data
            return r.get_json()

        def wait_states(want, timeout=5.0):
            deadline, last = time.time() + timeout, None
            while time.time() < deadline:
                last = receipt()
                if last.get("states") == want:
                    return last
                time.sleep(0.05)
            self.fail(f"states never became {want}: {last}")

        submit("decline@example.com", False)
        rc = receipt()
        assert rc["states"] == {}, rc
        assert provider.writes == [], provider.writes

        submit("opt1@example.com", True, "Documentary")
        wait_states({"sent": 1})
        assert provider.writes == [
            {
                "email_address": "opt1@example.com",
                "status_if_new": "pending",
                "merge_fields": {"GENRE": "Documentary"},
            }
        ], provider.writes

        r = client.put(
            "/api/policy/event_contact_sync", json={"required": True}, headers=hdr
        )
        assert r.status_code in (200, 204), r.data
        submit("opt2@example.com", True, "Documentary")
        wait_states({"waiting": 1, "sent": 1})
        assert len(provider.writes) == 1, provider.writes

        r = client.put(
            "/api/policy/event_contact_sync", json={"required": False}, headers=hdr
        )
        assert r.status_code in (200, 204), r.data
        wait_states({"sent": 2})
        assert provider.writes == [
            {
                "email_address": "opt1@example.com",
                "status_if_new": "pending",
                "merge_fields": {"GENRE": "Documentary"},
            },
            {
                "email_address": "opt2@example.com",
                "status_if_new": "pending",
                "merge_fields": {"GENRE": "Documentary"},
            },
        ], provider.writes

        r = client.post("/api/integrations/mailchimp/disconnect", json={}, headers=hdr)
        assert r.status_code in (200, 204), r.data
        rc = receipt()
        assert rc["state"] == "off", rc

    def exercise_browser(self, app, operator, provider, *, engagement=False):
        from browser import chromium
        from playwright.sync_api import expect, sync_playwright
        from test_drive_import import QuietHandler, service_worker
        from werkzeug.serving import make_server

        client = app.test_client()
        headers = {"Authorization": "Bearer " + operator, "X-CuePorch-Integration": "1"}
        uid, opens = self.create_webinar(client, headers, engagement=engagement)
        server = make_server(
            "127.0.0.1", 0, app, request_handler=QuietHandler, threaded=True
        )
        base = f"http://127.0.0.1:{server.server_port}"
        with service_worker(server), sync_playwright() as pw, chromium(pw) as browser:
            page = browser.new_page()
            page.add_init_script(
                "sessionStorage.setItem('cueporchOperator', "
                + json.dumps(operator)
                + ")"
            )
            page.goto(base + "/operator/live-events")
            page.get_by_role(
                "button", name="Open production for Mailchimp Webinar"
            ).click(timeout=8000)
            page.get_by_text("Attendees for this occurrence").click()
            with page.expect_response(
                lambda r: "/api/integrations/mailchimp/audiences?offset=0" in r.url,
                timeout=8000,
            ) as response_info:
                page.get_by_text("Load Mailchimp audiences", exact=True).click()
            resp = response_info.value
            assert resp.ok, (
                f"Mailchimp load audiences failed: {resp.status} {resp.text()}"
            )
            assert resp.json()["next_offset"] == 1
            expect(page.locator("#eventMailchimpMore")).to_be_visible(timeout=5000)
            page.get_by_text("More Mailchimp audiences", exact=True).click(timeout=8000)
            page.select_option("#eventMailchimpAudience", "listsafe2")
            expect(page.locator("#eventMailchimpMappingStatus")).to_contain_text(
                "Map required field: Genre."
            )
            expect(page.locator("#eventMailchimpEnable")).to_be_disabled()
            page.select_option(
                '#eventMailchimpMapping select[data-form-field="field_genre"]', "GENRE"
            )
            if engagement:
                eng = page.locator("#eventMailchimpEngagement")
                expect(eng).to_be_enabled()
                eng.check()
            page.locator("#eventMailchimpEnable").click()
            expect(page.locator("#eventMailchimpStatus")).to_contain_text(
                "Mailchimp delivery enabled."
            )
            if engagement:
                expect(page.locator("#eventMailchimpEngagement")).to_be_checked()
            attendee = browser.new_page()
            attendee.goto(base + "/events/mailchimp-webinar?occurrence=" + str(opens))
            attendee.fill("input[name=first_name]", "Test")
            attendee.fill("input[name=last_name]", "User")
            attendee.fill("input[name=email]", "screening@example.test")
            attendee.select_option('select[name="field_genre"]', "Documentary")
            cb = attendee.locator('input[name="mailchimp_consent"]')
            expect(cb).not_to_be_checked()
            cb.check()
            if engagement:
                eng_cb = attendee.locator(
                    'input[name="mailchimp_engagement_consent"]'
                )
                expect(eng_cb).not_to_be_checked()
                eng_cb.check()
                r = client.put(
                    "/api/policy/event_engagement_sync",
                    headers=headers,
                    json={"required": True},
                )
                assert r.status_code in (200, 204), r.data
            else:
                expect(
                    attendee.locator('input[name="mailchimp_engagement_consent"]')
                ).to_have_count(0)
            attendee.locator("form button[type=submit]").first.click()
            self.wait_for_sent(client, headers, uid, opens)
            assert provider.writes == [
                {
                    "email_address": "screening@example.test",
                    "status_if_new": "pending",
                    "merge_fields": {"GENRE": "Documentary"},
                }
            ]
            if engagement:
                status = client.get(
                    f"/api/live-events/{uid}/{opens}/registration/mailchimp/outbound",
                    headers=headers,
                )
                assert status.status_code == 200, status.data
                receipt = status.get_json()
                assert receipt["engagement"] is True, receipt
                assert receipt["engagement_states"] == {"waiting": 1}, receipt
            page.get_by_role("button", name="Refresh Mailchimp delivery status").click()
            expect(page.locator("#eventMailchimpStatus")).to_contain_text("sent: 1")
            if engagement:
                expect(page.locator("#eventMailchimpStatus")).to_contain_text(
                    "Engagement waiting: 1"
                )
            page.get_by_role("button", name="Stop Mailchimp delivery").click()
            expect(page.locator("#eventMailchimpStatus")).to_contain_text(
                "Mailchimp delivery is off."
            )
            status = client.get(
                f"/api/live-events/{uid}/{opens}/registration/mailchimp/outbound",
                headers=headers,
            )
            assert status.status_code == 200, status.data
            assert status.get_json()["enabled"] is False
