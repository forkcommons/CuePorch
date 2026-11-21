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


class MailchimpRegistrationTest(unittest.TestCase):
    def test_mailchimp_registration_delivery(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps({"institution": "station", "schema": 1, "works": []}),
                encoding="utf-8",
            )
            database = root / "workspace.sqlite"
            provider = RegistrationProvider()

            cleanup.enter_context(
                patch.dict(
                    "os.environ",
                    {
                        "CUEPORCH_PUBLIC_URL": "http://localhost",
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
            self.exercise(app, operator, provider)

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

    def exercise(self, app, operator, provider):
        client = app.test_client()
        hdr = {"Authorization": "Bearer " + operator, "X-CuePorch-Integration": "1"}
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
