from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from werkzeug.exceptions import BadGateway, TooManyRequests

import campaign_monitor_crm as cm
import workspace_oauth
from institution import create_app
from test_marketo_connection import _resp

CLIENT_ID = "a" * 32
LIST_ID = "b" * 32
OPERATOR = "t" * 48

REDIRECT_URI = "http://localhost/integration/oauth/campaign-monitor/callback"


class CampaignMonitorProvider:
    def __init__(self):
        self.exchanges = 0
        self.refreshes = 0
        self.list_reads = 0

    def request(self, method, url, **kwargs):
        method = (method or "").upper()
        headers = kwargs.get("headers") or {}
        auth = headers.get("Authorization") or headers.get("authorization")

        if method == "POST" and url == cm.OAUTH.token_url:
            data = kwargs.get("data") or {}
            grant_type = data.get("grant_type")

            if grant_type == "authorization_code":
                expected = {
                    "grant_type": "authorization_code",
                    "code": "fixture-code",
                    "redirect_uri": REDIRECT_URI,
                    "client_id": "cid",
                    "client_secret": "sec",
                }
                assert data == expected
                self.exchanges += 1
                return _resp({
                    "access_token": "a1",
                    "refresh_token": "r1",
                    "expires_in": 1209600,
                })

            if grant_type == "refresh_token":
                expected = {
                    "grant_type": "refresh_token",
                    "refresh_token": "r1",
                }
                assert data == expected
                self.refreshes += 1
                return _resp({
                    "access_token": "a2",
                    "refresh_token": "r2",
                    "expires_in": 1209600,
                })

            raise AssertionError("unexpected token POST payload")

        if method == "GET" and url == cm.API_BASE + "/clients.json":
            if auth == "Bearer a1":
                response = _resp({"error": "invalid_token"})
                response.status_code = 401
                response._content_consumed = True
                return response
            if auth == "Bearer a2":
                return _resp([
                    {"ClientID": CLIENT_ID, "Name": "Station client"},
                ])
            raise AssertionError(f"unexpected authorization for {url}")

        if method == "GET" and url == cm.API_BASE + f"/clients/{CLIENT_ID}/lists.json":
            if auth != "Bearer a2":
                raise AssertionError(f"unexpected authorization for {url}")
            self.list_reads += 1
            return _resp([
                {"ListID": LIST_ID, "Name": "Station audience"},
            ])

        raise AssertionError(f"unexpected Campaign Monitor request: {method} {url}")


class CampaignMonitorConnectionTest(unittest.TestCase):
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
            "CUEPORCH_CAMPAIGN_MONITOR_CLIENT_ID": "cid",
            "CUEPORCH_CAMPAIGN_MONITOR_CLIENT_SECRET": "sec",
        }
        stack.enter_context(patch.dict(os.environ, env))
        stack.enter_context(
            patch("institution.library_worker.start", return_value=lambda: None)
        )
        self.provider = CampaignMonitorProvider()
        stack.enter_context(
            patch.object(
                workspace_oauth,
                "provider_request",
                side_effect=self.provider.request,
            )
        )
        self.app = create_app(temp_root, self.database, OPERATOR, "station")
        for name in list(self.app.extensions):
            if name.startswith("stop_"):
                stack.callback(self.app.extensions[name])
        self.client = self.app.test_client()
        self.auth = {
            "Authorization": "Bearer " + OPERATOR,
            "X-CuePorch-Integration": "1",
        }


    def connect(self):
        res = self.client.post(
            "/api/integrations/campaign-monitor/connect",
            json={},
            headers=self.auth,
        )
        self.assertEqual(res.status_code, 200)

        authorization_url = res.get_json()["authorization_url"]
        query = parse_qs(urlsplit(authorization_url).query)

        self.assertEqual(query["type"], ["web_server"])
        self.assertEqual(query["scope"], [cm.OAUTH.scope])
        self.assertEqual(query["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(query["client_id"], ["cid"])
        self.assertIn("state", query)
        self.assertTrue(query["state"][0])
        self.assertNotIn("response_type", query)

        state = query["state"][0]
        callback = "/integration/oauth/campaign-monitor/callback"
        params = {"state": state, "code": "fixture-code"}

        fresh = self.app.test_client()
        self.assertEqual(fresh.get(callback, query_string=params).status_code, 403)

        res = self.client.get(callback, query_string=params)
        self.assertEqual(res.status_code, 303)
        self.assertIn("result=connected", res.headers["Location"])

        self.assertEqual(
            self.client.get(callback, query_string=params).status_code,
            403,
        )


    def test_workspace_connection(self):
        root = "/api/integrations/campaign-monitor"
        clients = root + "/clients"
        self.assertEqual(self.client.get(clients).status_code, 403)
        self.assertEqual(self.client.get(clients, headers=self.auth).status_code, 409)

        self.connect()

        r = self.client.get(clients, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.get_json(),
            {"clients": [{"id": CLIENT_ID, "name": "Station client"}]},
        )
        r = self.client.get(f"{clients}/{CLIENT_ID}/lists", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.get_json(),
            {"lists": [{"id": LIST_ID, "name": "Station audience"}]},
        )

        bad = self.client.get(f"{clients}/bad-client-id/lists", headers=self.auth)
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(
            (self.provider.exchanges, self.provider.refreshes, self.provider.list_reads),
            (1, 1, 1),
        )

        with closing(sqlite3.connect(self.database)) as db:
            token = db.execute(
                "select token from workspace_integration_connection where provider = ?",
                ("campaign-monitor",),
            ).fetchone()[0]
        self.assertIsInstance(token, bytes)
        self.assertNotIn(b'"access":', token)

        r = self.client.post(root + "/disconnect", headers=self.auth, json={})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["connected"])
        self.assertEqual(self.client.get(clients, headers=self.auth).status_code, 409)

class CampaignMonitorSubscriberTest(unittest.TestCase):
    def test_new_subscriber_awaits_readback(self):
        conn = Mock()

        absent = _resp({"Code": 203})
        absent.status_code = 400

        created = _resp("new@example.test")
        created.status_code = 201

        readback = _resp({
            "EmailAddress": "new@example.test",
            "State": "Unconfirmed",
        })

        conn.api.side_effect = [absent, created, readback]
        gate = Mock()

        result = cm.send_registration(
            conn, "rev", LIST_ID,
            "New@example.test", " First ", " Last ",
            before_send=gate,
        )
        self.assertEqual("submitted", result)

        conn.api.assert_any_call(
            "rev",
            "POST",
            f"{cm.API_BASE}/subscribers/{LIST_ID}.json",
            json={
                "EmailAddress": "new@example.test",
                "Name": "First Last",
                "Resubscribe": False,
                "ConsentToTrack": "No",
            },
            before_send=gate,
        )

        self.assertEqual(
            "pending_confirmation",
            cm.read_registration(conn, "rev", LIST_ID, "new@example.test"),
        )


    def test_existing_statuses_preserved(self):
        conn = Mock()
        gate = Mock()
        cases = [
            ("Active", "sent"),
            ("Unconfirmed", "pending_confirmation"),
            ("Unsubscribed", "skipped"),
            ("Bounced", "skipped"),
            ("Deleted", "skipped"),
        ]
        for provider_state, expected_state in cases:
            with self.subTest(provider_state=provider_state):
                conn.api.reset_mock()
                conn.api.side_effect = None
                conn.api.return_value = _resp({
                    "EmailAddress": "viewer@example.test",
                    "State": provider_state,
                })
                gate.reset_mock()
                state = cm.send_registration(
                    conn,
                    "rev",
                    LIST_ID,
                    "viewer@example.test",
                    "A",
                    "B",
                    before_send=gate,
                )
                self.assertEqual(state, expected_state)
                self.assertEqual(conn.api.call_count, 1)
                self.assertEqual(conn.api.call_args[0][1], "GET")
                gate.assert_not_called()

    def test_suppressed_and_rate_limited(self):
        conn = Mock()
        gate = Mock()
        absent = _resp({"Code": 203})
        absent.status_code = 400
        suppressed = _resp({"Code": 204})
        suppressed.status_code = 400
        conn.api.reset_mock()
        conn.api.return_value = None
        conn.api.side_effect = [absent, suppressed]
        self.assertEqual(
            cm.send_registration(
                conn,
                "rev",
                LIST_ID,
                "viewer@example.test",
                "A",
                "B",
                before_send=gate,
            ),
            "skipped",
        )

        absent = _resp({"Code": 203})
        absent.status_code = 400
        rate = _resp({}, {"Retry-After": "45"})
        rate.status_code = 429
        rate._content_consumed = True
        conn.api.reset_mock()
        conn.api.side_effect = [absent, rate]
        with self.assertRaises(TooManyRequests) as ctx:
            cm.send_registration(
                conn,
                "rev",
                LIST_ID,
                "viewer@example.test",
                "A",
                "B",
                before_send=gate,
            )
        self.assertEqual(ctx.exception.retry_after, 45)

        readback = _resp({
            "EmailAddress": "other@example.test",
            "State": "Active",
        })
        conn.api.reset_mock()
        conn.api.side_effect = None
        conn.api.return_value = readback
        with self.assertRaises(BadGateway):
            cm.send_registration(
                conn,
                "rev",
                LIST_ID,
                "viewer@example.test",
                "A",
                "B",
                before_send=gate,
            )
        self.assertEqual(conn.api.call_count, 1)
        self.assertEqual(conn.api.call_args[0][1], "GET")
