import base64
import hashlib
import json
import sqlite3
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from institution import create_app
from test_marketo_connection import _resp


class MailchimpProvider:
    def __init__(self, challenge=None, fault=None):
        self.challenge = challenge
        self.fault = fault
        self.exchanges = 0
        self.reads = 0

    def request(self, method, url, **kwargs):
        if method == "POST" and url == "https://login.mailchimp.com/oauth2/token":
            data = kwargs.get("data") or {}
            assert data.get("grant_type") == "authorization_code"
            assert data.get("code") == "demo-code"
            assert (
                data.get("redirect_uri")
                == "http://localhost/integration/oauth/mailchimp/callback"
            )
            assert data.get("client_id") == "cid"
            assert data.get("client_secret") == "sec"
            verifier = data.get("code_verifier")
            assert verifier
            computed = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            assert computed == self.challenge
            self.exchanges += 1
            return _resp({"access_token": "fixture-mailchimp-access"})

        assert method == "GET"
        parsed = urlsplit(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "api.mailchimp.com"
        assert parsed.path.startswith("/3.0/")
        headers = kwargs.get("headers") or {}
        assert headers.get("Authorization") == "Bearer fixture-mailchimp-access"
        self.reads += 1

        if self.fault in (401, 429):
            response = _resp({}, {"Retry-After": "37"})
            response.status_code = self.fault
            response._content_consumed = True
            return response

        params = kwargs.get("params") or {}
        path = parsed.path

        if path == "/3.0/lists":
            assert params.get("count") == 50
            offset = params.get("offset")
            assert offset in (0, 1)
            row = (
                {"id": "listsafe1", "name": "Arts subscribers"}
                if offset == 0
                else {"id": "listsafe2", "name": "Station audience"}
            )
            return _resp({"total_items": 2, "lists": [row]})

        if path == "/3.0/lists/listsafe1":
            return _resp(
                {
                    "id": "listsafe1",
                    "name": "Arts subscribers",
                    "double_optin": True,
                    "marketing_permissions": False,
                }
            )

        if path == "/3.0/lists/listsafe1/merge-fields":
            return _resp(
                {
                    "list_id": "listsafe1",
                    "total_items": 1,
                    "merge_fields": [
                        {
                            "tag": "GENRE",
                            "name": "Genre",
                            "type": "dropdown",
                            "required": True,
                            "options": {"choices": ["Documentary", "Experimental"]},
                        }
                    ],
                }
            )

        raise AssertionError("unknown network: " + url)


class MailchimpConnectionTest(unittest.TestCase):
    def test_mailchimp_workspace_connection(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps({"institution": "station", "schema": 1, "works": []}),
                encoding="utf-8",
            )
            database = root / "workspace.sqlite"
            provider = MailchimpProvider()
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
            operator = "t" * 48
            app = create_app(root, database, operator, "station")
            for name, value in app.extensions.items():
                if name.startswith("stop_") and callable(value):
                    cleanup.callback(value)
            self.exercise_connection(app, database, operator, provider)

    def exercise_connection(self, app, database, operator, provider):
        base = "/api/integrations/mailchimp"
        headers = {"Authorization": "Bearer " + operator, "X-CuePorch-Integration": "1"}
        anon = app.test_client()
        assert anon.get(base + "/audiences").status_code == 403
        assert (
            anon.post(
                base + "/connect",
                headers={**headers, "Origin": "https://other.example"},
                json={},
            ).status_code
            == 403
        )
        client = app.test_client()
        rv = client.post(base + "/connect", headers=headers, json={})
        assert rv.status_code == 200
        authorization_url = rv.get_json()["authorization_url"]
        parts = urlsplit(authorization_url)
        query = parse_qs(parts.query)
        assert parts.scheme == "https"
        assert parts.netloc == "login.mailchimp.com"
        assert parts.path == "/oauth2/authorize"
        assert query["code_challenge_method"] == ["S256"]
        provider.challenge = query["code_challenge"][0]
        state = query["state"][0]
        callback_path = "/integration/oauth/mailchimp/callback"
        callback_query = {"state": state, "code": "demo-code"}
        assert anon.get(callback_path, query_string=callback_query).status_code == 403
        rv = client.get(callback_path, query_string=callback_query)
        assert rv.status_code == 303
        assert (
            rv.headers["Location"]
            == "/operator/integrations?connection=mailchimp&result=connected"
        )
        assert client.get(callback_path, query_string=callback_query).status_code == 403
        assert provider.exchanges == 1
        with closing(sqlite3.connect(database)) as conn:
            row = conn.execute(
                "SELECT token FROM workspace_integration_connection WHERE provider=?",
                ("mailchimp",),
            ).fetchone()
        assert row is not None
        token = row[0]
        assert isinstance(token, (bytes, bytearray))
        assert b"fixture-mailchimp-access" not in token
        self.verify_audiences(client, base, headers, provider)
        rv = client.post(
            base + "/disconnect", headers=headers, json={"marker": "disconnect"}
        )
        assert rv.status_code == 200
        assert rv.get_json()["connected"] is False
        assert client.get(base + "/audiences", headers=headers).status_code == 409
        with closing(sqlite3.connect(database)) as conn:
            row = conn.execute(
                "SELECT token FROM workspace_integration_connection WHERE provider=?",
                ("mailchimp",),
            ).fetchone()
        assert row is None
        assert provider.exchanges == 1

    def verify_audiences(self, client, base, headers, provider):
        integrations = client.get("/api/integrations", headers=headers).get_json()[
            "integrations"
        ]
        mailchimp = next(
            integration
            for integration in integrations
            if integration["id"] == "mailchimp"
        )
        self.assertEqual(mailchimp["category"], "Marketing")
        self.assertTrue(mailchimp["connected"])

        response = client.get(base + "/audiences", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(
            response.get_json(),
            dict(
                audiences=[dict(id="listsafe1", name="Arts subscribers")], next_offset=1
            ),
        )

        response = client.get(
            base + "/audiences", headers=headers, query_string={"offset": 1}
        )
        self.assertEqual(
            response.get_json(),
            dict(
                audiences=[dict(id="listsafe2", name="Station audience")],
                next_offset=None,
            ),
        )

        response = client.get(base + "/audiences/listsafe1/fields", headers=headers)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        audience = body["audience"]
        self.assertEqual(audience["id"], "listsafe1")
        self.assertTrue(audience["double_optin"])
        genre = next(field for field in body["fields"] if field["tag"] == "GENRE")
        self.assertEqual(genre["type"], "dropdown")
        self.assertEqual(genre["choices"], ["Documentary", "Experimental"])

        self.verify_audience_errors(client, base, headers, provider)

    def verify_audience_errors(self, client, base, headers, provider):
        before = provider.reads
        r = client.get(base + "/audiences?offset=bad", headers=headers)
        assert r.status_code == 400
        assert provider.reads == before

        r = client.get(base + "/audiences/$bad/fields", headers=headers)
        assert r.status_code == 400
        assert provider.reads == before

        provider.fault = 429
        r = client.get(base + "/audiences", headers=headers)
        assert r.status_code == 429
        assert r.headers.get("Retry-After") == "37"

        provider.fault = 401
        before = provider.reads
        r = client.get(base + "/audiences", headers=headers)
        assert r.status_code == 409
        assert provider.reads == before + 1
        assert provider.exchanges == 1
        provider.fault = None
