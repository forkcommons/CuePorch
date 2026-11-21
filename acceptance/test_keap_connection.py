from contextlib import ExitStack
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import keap_crm as keap
import library_list_registration as forms
import test_constant_contact_forms as cc_fixture
from institution import workspace_oauth, create_app
from test_marketo_connection import _resp

OPERATOR = 't' * 48
REDIRECT_URI = 'http://localhost/integration/oauth/keap/callback'


def keap_request(method, url, **kwargs):
    method = method.upper()

    if method == 'POST' and url == keap.OAUTH.token_url:
        data = kwargs.get('data')
        initial_data = {
            'grant_type': 'authorization_code',
            'code': 'fixture-code',
            'redirect_uri': REDIRECT_URI,
            'client_id': 'cid',
            'client_secret': 'sec',
        }
        refresh_data = {
            'grant_type': 'refresh_token',
            'refresh_token': 'r1',
        }

        if data == initial_data:
            assert 'auth' not in kwargs or kwargs['auth'] is None
            return _resp({
                'access_token': 'a1',
                'refresh_token': 'r1',
                'token_type': 'Bearer',
                'expires_in': 3600,
                'scope': 'full|station.infusionsoft.com',
            })

        if data == refresh_data:
            assert kwargs.get('auth') == ('cid', 'sec')
            return _resp({
                'access_token': 'a2',
                'refresh_token': 'r2',
                'token_type': 'Bearer',
                'expires_in': 3600,
                'scope': 'full|station.infusionsoft.com',
            })

        raise AssertionError

    if method == 'GET' and url == keap.API_BASE + '/tags':
        headers = kwargs.get('headers') or {}
        auth = headers.get('Authorization')
        if auth is None:
            auth = headers.get('authorization')
        assert auth in ('Bearer a1', 'Bearer a2')

        if auth == 'Bearer a1':
            resp = _resp({})
            resp.status_code = 401
            resp._content_consumed = True
            return resp

        params = kwargs.get('params') or {}
        assert params.get('page_size') == 50

        if 'page_token' not in params:
            return _resp({
                'tags': [{'id': '1', 'name': 'Public media'}],
                'next_page_token': 'next+/=',
            })

        assert params['page_token'] == 'next+/='
        return _resp({
            'tags': [{'id': '5', 'name': 'Arts'}],
        })

    raise AssertionError


class KeapConnectionTest(unittest.TestCase):
    def test_connect_and_read_tags(self):
        with TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            (root / "objects").mkdir(parents=True, exist_ok=True)
            (root / "manifest.json").write_text(
                json.dumps({"institution": "station", "schema": 1, "works": []})
            )

            with patch.dict(
                os.environ,
                {
                    "CUEPORCH_PUBLIC_URL": "",
                    "CUEPORCH_KEAP_CLIENT_ID": "cid",
                    "CUEPORCH_KEAP_CLIENT_SECRET": "sec",
                },
            ), patch(
                "institution.library_worker.start", return_value=lambda: None
            ), patch.object(
                workspace_oauth, "provider_request", side_effect=keap_request
            ) as mock:
                app = create_app(root, root / "station.sqlite3", OPERATOR, "station")

                for name, callback in list(app.extensions.items()):
                    if str(name).startswith("stop_") and callable(callback):
                        stack.callback(callback)

                client = app.test_client()
                headers = {
                    "Authorization": f"Bearer {OPERATOR}",
                    "X-CuePorch-Integration": "1",
                }
                tags_url = "/api/integrations/keap/tags"

                self.assertEqual(client.get(tags_url).status_code, 403)
                self.assertEqual(client.get(tags_url, headers=headers).status_code, 409)

                resp = client.post(
                    "/api/integrations/keap/connect", headers=headers, json={}
                )
                self.assertEqual(resp.status_code, 200)
                authorization_url = resp.get_json()["authorization_url"]

                parsed = urlsplit(authorization_url)
                query = parse_qs(parsed.query)
                self.assertEqual(query["response_type"], ["code"])
                self.assertEqual(query["scope"], ["full"])
                self.assertEqual(query["redirect_uri"], [REDIRECT_URI])

                state = query["state"][0]
                callback_query = {"state": state, "code": "fixture-code"}

                independent_client = app.test_client()
                self.assertEqual(
                    independent_client.get(
                        "/integration/oauth/keap/callback",
                        query_string=callback_query,
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    client.get(
                        "/integration/oauth/keap/callback",
                        query_string=callback_query,
                    ).status_code,
                    303,
                )
                self.assertEqual(
                    client.get(
                        "/integration/oauth/keap/callback",
                        query_string=callback_query,
                    ).status_code,
                    403,
                )

                resp = client.get(tags_url, headers=headers)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(
                    resp.get_json(),
                    {
                        "lists": [{"id": "1", "name": "Public media"}],
                        "next_cursor": "next+/=",
                    },
                )

                resp = client.get(
                    tags_url,
                    headers=headers,
                    query_string={"cursor": "next+/="},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.get_json(), {'lists': [{'id': '5', 'name': 'Arts'}], 'next_cursor': None})
                self.assertEqual(sum(call.kwargs.get('data', {}).get('grant_type') == 'refresh_token' for call in mock.call_args_list), 1)
                resp = client.post('/api/integrations/keap/disconnect', headers=headers, json={})
                self.assertEqual(resp.status_code, 200)
                self.assertFalse(resp.get_json()['connected'])
                resp = client.get(tags_url, headers=headers)
                self.assertEqual(resp.status_code, 409)


class KeapDeliveryProvider:
    def __init__(self):
        self.creates = []
        self.applies = []
        self.new = False
        self.tagged = False

    def _manual_contact(self):
        return {
            "id": "4",
            "email_addresses": [{"email": "manual@example.test", "field": "EMAIL1"}],
            "tag_ids": [],
        }

    def _fresh_contact(self):
        return {
            "id": "7",
            "email_addresses": [{"email": "fresh@example.test", "field": "EMAIL1"}],
            "tag_ids": ["5"] if self.tagged else [],
        }

    def _response(self, payload, status=200):
        r = _resp(payload)
        if status != 200:
            r.status_code = status
            r._content_consumed = True
        return r

    def __call__(self, method, url, **kwargs):
        m = method.upper()
        base = keap.API_BASE.rstrip("/")
        if not url.startswith(base):
            return keap_request(method, url, **kwargs)

        path = url[len(base):]
        handled = (
            m == "GET" and path in (
                "/tags/5",
                "/contacts",
                "/contacts/7",
                "/emailAddresses/manual%40example.test/status",
                "/emailAddresses/fresh%40example.test/status",
            )
        ) or (
            m == "POST" and path in (
                "/contacts",
                "/tags/5/contacts:applyTags",
            )
        )
        if not handled:
            return keap_request(method, url, **kwargs)

        headers = kwargs.get("headers") or {}
        auth = headers.get("Authorization") or headers.get("authorization")
        if auth != "Bearer a2":
            return self._response({"message": "Unauthorized"}, 401)

        if m == "GET" and path == "/tags/5":
            return self._response({"id": "5", "name": "Arts"})

        if m == "GET" and path == "/contacts":
            flt = (kwargs.get("params") or {}).get("filter")
            if flt == "email==manual@example.test":
                return self._response({"contacts": [self._manual_contact()]})
            if flt == "email==fresh@example.test":
                return self._response({"contacts": [self._fresh_contact()] if self.new else []})

        if m == "POST" and path == "/contacts":
            payload = kwargs.get("json")
            email_addresses = payload.get("email_addresses") or []
            if any(e.get("email") == "fresh@example.test" for e in email_addresses):
                assert any(
                    e.get("email") == "fresh@example.test" and "opt_in_reason" in e
                    for e in email_addresses
                )
            assert "duplicate_option" not in payload
            assert payload.get("given_name") == "Test"
            assert payload.get("family_name") == "Viewer"
            self.new = True
            self.creates.append(payload)
            return self._response({"message": "Service Unavailable"}, 503)

        if m == "GET" and path == "/emailAddresses/manual%40example.test/status":
            return self._response({"opted_in": False, "status": "MANUAL", "email":"manual@example.test"})

        if m == "GET" and path == "/emailAddresses/fresh%40example.test/status":
            if not self.new:
                return self._response({"message": "Not Found"}, 404)
            return self._response({"opted_in": True, "status": "SINGLE_OPT_IN", "email":"fresh@example.test"})

        if m == "POST" and path == "/tags/5/contacts:applyTags":
            payload = kwargs.get("json")
            assert payload == {"contact_ids": ["7"]}
            self.applies.append(payload)
            return self._response({"results": {"7": "applied"}})

        if m == "GET" and path == "/contacts/7":
            if not self.new:
                return self._response({"message": "Not Found"}, 404)
            return self._response(self._fresh_contact())

        return keap_request(method, url, **kwargs)


class KeapRegistrationDeliveryTest(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        temp_root = Path(stack.enter_context(TemporaryDirectory()))
        (temp_root / "objects").mkdir()
        (temp_root / "manifest.json").write_text(
            json.dumps({"institution": "station", "schema": 1, "works": []})
        )
        self.database = temp_root / "station.sqlite3"
        env = {
            "CUEPORCH_PUBLIC_URL": "",
            "CUEPORCH_KEAP_CLIENT_ID": "cid",
            "CUEPORCH_KEAP_CLIENT_SECRET": "sec",
        }
        stack.enter_context(patch.dict(os.environ, env))
        stack.enter_context(
            patch("institution.library_worker.start", return_value=lambda: None)
        )
        self.provider = KeapDeliveryProvider()
        stack.enter_context(
            patch.object(
                workspace_oauth,
                "provider_request",
                side_effect=self.provider,
            )
        )

        def capture(app, tx, database, connection, validate, provider=forms.CONSTANT_CONTACT):
            if provider == forms.KEAP:
                self.runner = (tx, connection, validate)
            return lambda: None

        stack.enter_context(patch.object(forms, "start", side_effect=capture))
        self.now = time.time() + 1
        self.stop = Event()
        self.app = create_app(temp_root, self.database, OPERATOR, "station")
        for name in list(self.app.extensions):
            if name.startswith("stop_"):
                stack.callback(self.app.extensions[name])
        self.client = self.app.test_client()
        self.auth = {
            "Authorization": "Bearer " + OPERATOR,
            "X-CuePorch-Integration": "1",
        }

    def test_consent_and_delayed_provider_confirmation(self):
        res = self.client.post(
            "/api/integrations/keap/connect",
            json={},
            headers=self.auth,
        )
        self.assertEqual(res.status_code, 200)
        authorization_url = res.get_json()["authorization_url"]
        state = parse_qs(urlsplit(authorization_url).query)["state"][0]
        callback = "/integration/oauth/keap/callback"
        params = {"state": state, "code": "fixture-code"}
        res = self.client.get(callback, query_string=params)
        self.assertEqual(res.status_code, 303)
        self.assertIn("result=connected", res.headers["Location"])

        embed_path, outbound = cc_fixture.ConstantContactFormsTest.enable_video(
            self, configure_delivery=False
        )
        outbound = outbound.replace('/constant-contact/', '/keap/')
        res = self.client.put(outbound, headers=self.auth, json={'list_id': '5'})
        self.assertEqual(res.status_code, 200)

        def receipt():
            return self.client.get(outbound, headers=self.auth).get_json()['states']

        def tick():
            return forms.run_one(
                *self.runner, self.stop,
                clock=lambda: self.now,
                provider=forms.KEAP,
            )

        def submit(email, consent):
            return cc_fixture.ConstantContactFormsTest.submit(
                self, embed_path, email,
                consent=consent,
                consent_field='keap_form_consent',
            )

        self.now = time.time() + 1
        submit('example@example.test', False)
        self.assertFalse(tick())

        submit('manual@example.test', True)
        self.assertTrue(tick())
        self.assertEqual(receipt(), {'skipped': 1})
        self.assertEqual(self.provider.creates, [])
        self.assertEqual(self.provider.applies, [])

        submit('fresh@example.test', True)
        self.assertTrue(tick())
        self.assertEqual(receipt(), {'skipped': 1, 'submitted': 1})
        self.assertEqual(len(self.provider.creates), 1)

        self.now += 31
        self.assertTrue(tick())
        self.assertEqual(receipt(), {'skipped': 1, 'submitted': 1})

        self.now += 31
        self.assertTrue(tick())
        self.assertEqual(receipt(), {'skipped': 1, 'submitted': 1})

        self.provider.tagged = True
        self.now += 31
        self.assertTrue(tick())
        self.assertEqual(receipt(), {'skipped': 1, 'sent': 1})
        self.assertEqual(len(self.provider.creates), 1)
        self.assertEqual(len(self.provider.applies), 1)
