import json
import os
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import institution
import workspace_oauth
from test_marketo_connection import _resp

ORIGIN = "https://station.my.salesforce.com"
CAMPAIGNS = "/api/integrations/salesforce/campaigns"
CALLBACK = "/integration/oauth/salesforce/callback"
FIRST = {"Id": "701000000000001", "Name": "Media", "IsActive": True}
SECOND = {"Id": "701000000000002", "Name": "Arts", "IsActive": True}


def salesforce_request(method, url, **kwargs):
    method = method.upper()

    def _headers():
        headers = kwargs.get("headers") or {}
        items = headers.items() if hasattr(headers, "items") else headers
        return {str(k).lower(): v for k, v in items}

    def _data():
        data = kwargs.get("data")
        if data is None:
            return {}
        if isinstance(data, dict):
            return data
        if isinstance(data, str):
            return {k: v[0] for k, v in parse_qs(data).items()}
        try:
            return dict(data)
        except Exception:
            return {}

    def _auth():
        return _headers().get("authorization")

    if method == "POST" and url == ORIGIN + "/services/oauth2/token":
        data = _data()
        grant_type = data.get("grant_type")

        if grant_type == "authorization_code":
            assert "code_verifier" in data
            return _resp({
                "access_token": "a1",
                "refresh_token": "r1",
                "token_type": "Bearer",
                "scope": "api refresh_token",
                "instance_url": "https://untrusted.invalid",
            })

        if grant_type == "refresh_token":
            assert data.get("refresh_token") == "r1"
            return _resp({
                "access_token": "a2",
                "refresh_token": "r2",
                "token_type": "Bearer",
                "scope": "api refresh_token",
                "instance_url": "https://untrusted.invalid",
            })

        raise AssertionError(f"Unexpected token grant: {grant_type!r}")

    if method == "POST" and url == ORIGIN + "/services/oauth2/revoke":
        assert _data()["token"] == "r2"
        response = _resp({})
        response._content_consumed = True
        return response

    if method == "GET" and url == ORIGIN + "/services/data/v64.0/query":
        auth = _auth()

        if auth == "Bearer a1":
            resp = _resp({})
            resp.status_code = 401
            resp._content_consumed = True
            return resp

        assert auth == "Bearer a2"
        return _resp({
            "records": [FIRST],
            "nextRecordsUrl": "/services/data/v64.0/query/batch-200",
            "done": False,
        })

    if method == "GET" and url == ORIGIN + "/services/data/v64.0/query/batch-200":
        assert _auth() == "Bearer a2"
        return _resp({
            "records": [SECOND],
            "done": True,
        })

    raise AssertionError(f"Unexpected Salesforce request: {method} {url}")


class TestCampaignDiscovery(unittest.TestCase):
    def test_campaign_discovery(self):
        with ExitStack() as stack:
            root = Path(stack.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps({"institution": "station", "schema": 1, "works": []})
            )
            stack.enter_context(patch.dict(os.environ, {
                "CUEPORCH_PUBLIC_URL": "",
                "CUEPORCH_SALESFORCE_ORIGIN": ORIGIN,
                "CUEPORCH_SALESFORCE_CLIENT_ID": "cid",
                "CUEPORCH_SALESFORCE_CLIENT_SECRET": "sec",
            }))
            stack.enter_context(patch.object(institution.library_worker, "start", return_value=lambda: None))
            stack.enter_context(patch.object(workspace_oauth, "provider_request", side_effect=salesforce_request))
            app = institution.create_app(root, root / "station.sqlite3", "t" * 48, "station")
            for name, callback in app.extensions.items():
                if name.startswith("stop_") and callable(callback):
                    stack.callback(callback)

            client = app.test_client()
            auth = {"Authorization": "Bearer " + "t" * 48, "X-CuePorch-Integration": "1"}

            self.assertEqual(client.get(CAMPAIGNS).status_code, 403)
            self.assertEqual(client.get(CAMPAIGNS, headers=auth).status_code, 409)

            r = client.post("/api/integrations/salesforce/connect", json={}, headers=auth)
            self.assertEqual(r.status_code, 200)
            url = r.get_json()["authorization_url"]
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            self.assertTrue(url.startswith(ORIGIN + "/services/oauth2/authorize?"))
            self.assertEqual(parsed.path, "/services/oauth2/authorize")
            self.assertTrue(query.get("code_challenge"))
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertTrue(query.get("state"))

            params = {"state": query["state"][0], "code": "fixture-code"}
            self.assertEqual(app.test_client().get(CALLBACK, query_string=params).status_code, 403)
            self.assertEqual(client.get(CALLBACK, query_string=params).status_code, 303)
            self.assertEqual(client.get(CALLBACK, query_string=params).status_code, 403)

            r = client.get(CAMPAIGNS, headers=auth)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json(), {
                "campaigns": [{"id": FIRST["Id"], "name": "Media"}],
                "next_cursor": "batch-200",
            })

            r = client.get(CAMPAIGNS, query_string={"cursor": "batch-200"}, headers=auth)
            self.assertEqual(r.get_json()["campaigns"], [{"id": SECOND["Id"], "name": "Arts"}])

            self.assertEqual(
                client.get(CAMPAIGNS, query_string={"cursor": "https://evil.example"}, headers=auth).status_code,
                400,
            )

            r = client.post("/api/integrations/salesforce/disconnect", json={}, headers=auth)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["provider_revoked"])
            self.assertEqual(client.get(CAMPAIGNS, headers=auth).status_code, 409)

    def test_validates_my_domain_and_grant(self):
        from salesforce_crm import SalesforceConnection
        from werkzeug.exceptions import HTTPException
        env = {"CUEPORCH_SALESFORCE_CLIENT_ID": "cid", "CUEPORCH_SALESFORCE_CLIENT_SECRET": "sec"}
        with patch.dict(os.environ, {**env, "CUEPORCH_SALESFORCE_ORIGIN": "https://station.my.salesforce.com.example.invalid"}):
            self.assertFalse(SalesforceConnection(None, "t" * 48).configured)
        with patch.dict(os.environ, {**env, "CUEPORCH_SALESFORCE_ORIGIN": "https://Station--Test.sandbox.my.salesforce.com/"}):
            conn = SalesforceConnection(None, "t" * 48)
        self.assertEqual(conn.origin, "https://station--test.sandbox.my.salesforce.com")
        self.assertTrue(conn.configured)
        ciphertext = conn.cipher.encrypt(json.dumps({"origin": ORIGIN}).encode())
        with self.assertRaises(HTTPException) as cm:
            conn.tokens({"token": ciphertext})
        self.assertEqual(cm.exception.code, 409)

    def test_rejects_absolute_pagination_url(self):
        from salesforce_crm import _parse_campaigns
        from werkzeug.exceptions import HTTPException
        body = {"records": [FIRST], "done": False, "nextRecordsUrl": "https://another.my.salesforce.com/services/data/v64.0/query/batch-200"}
        with self.assertRaises(HTTPException) as cm:
            _parse_campaigns(body, None)
        self.assertEqual(cm.exception.code, 502)
