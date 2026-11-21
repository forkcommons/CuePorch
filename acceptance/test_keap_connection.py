from contextlib import ExitStack
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import keap_crm as keap
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
