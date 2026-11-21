import unittest
from unittest.mock import Mock, patch

from werkzeug.exceptions import Conflict
import workspace_oauth
import constant_contact_crm as contact
from test_marketo_connection import _resp


_U1 = "11111111-1111-1111-1111-111111111111"
_U2 = "22222222-2222-2222-2222-222222222222"


class WorkspaceProvider:
    def __init__(self, redirect_uri):
        self.redirect_uri = redirect_uri
        self.exchanges = 0
        self.refreshes = 0
        self.reads = 0

    def request(self, method, url, **kwargs):
        if method == "POST" and url == contact.OAUTH.token_url:
            assert kwargs.get("auth") == ("cid", "sec")
            p = kwargs.get("params") or {}
            if p["grant_type"] == "authorization_code":
                assert p == {
                    "grant_type": "authorization_code",
                    "code": "fixture-code",
                    "redirect_uri": self.redirect_uri,
                }
                self.exchanges += 1
                a, r = "fixture-a1", "fixture-r1"
            else:
                assert p == {
                    "grant_type": "refresh_token",
                    "refresh_token": "fixture-r1",
                }
                self.refreshes += 1
                a, r = "fixture-a2", "fixture-r2"
            return _resp({
                "access_token": a,
                "refresh_token": r,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": contact.OAUTH.scope,
            })
        if method == "GET" and url == contact.API_BASE + "/contact_lists":
            self.reads += 1
            h = (kwargs.get("headers") or {}).get("Authorization")
            if h == "Bearer fixture-a1":
                resp = _resp({})
                resp.status_code = 401
                resp._content_consumed = True
                return resp
            assert h == "Bearer fixture-a2"
            p = kwargs.get("params") or {}
            if p == {"limit": 50, "status": "active", "channel_type": "email"}:
                return _resp({
                    "lists": [{"list_id": _U1, "name": "Station audience"}],
                    "_links": {"next": {"href": "/v3/contact_lists?cursor=next-1"}},
                })
            assert p == {"cursor": "next-1"}
            return _resp({
                "lists": [{"list_id": _U2, "name": "Arts audience"}],
                "_links": {},
            })
        raise AssertionError(f"unexpected request {method} {url}")


class TestConstantContactConnection(unittest.TestCase):
    def test_token_exchange(self):
        tok1 = {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600,
                "token_type": "Bearer", "scope": contact.OAUTH.scope}
        hdrs = {"Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded"}
        with patch.dict("os.environ", {
                "CUEPORCH_CONSTANT_CONTACT_CLIENT_ID": "cid",
                "CUEPORCH_CONSTANT_CONTACT_CLIENT_SECRET": "sec"}), \
             patch.object(workspace_oauth, "provider_request") as pr:
            pr.return_value = _resp(tok1)
            conn = workspace_oauth.Connection(contact.OAUTH, tx=None, operator="t" * 48)
            saved = conn.exchange({"grant_type": "authorization_code",
                                   "code": "code",
                                   "redirect_uri": "https://fixture.test/cb"})
            self.assertEqual(saved["access"], "a1")
            self.assertEqual(saved["refresh"], "r1")
            pr.assert_called_with(
                "POST", contact.OAUTH.token_url,
                params={"grant_type": "authorization_code", "code": "code",
                        "redirect_uri": "https://fixture.test/cb"},
                auth=("cid", "sec"), headers=hdrs)
            pr.return_value = _resp({"access_token": "a2", "refresh_token": "r2",
                                     "expires_in": 3600, "token_type": "Bearer",
                                     "scope": contact.OAUTH.scope})
            pr.reset_mock()
            rotated = conn.refresh(saved)
            self.assertEqual(rotated["access"], "a2")
            self.assertEqual(rotated["refresh"], "r2")
            self.assertNotEqual(rotated["refresh"], saved["refresh"])
            pr.assert_called_with(
                "POST", contact.OAUTH.token_url,
                params={"grant_type": "refresh_token", "refresh_token": "r1"},
                auth=("cid", "sec"), headers=hdrs)

    def test_contact_list_pages(self):
        from unittest.mock import Mock
        from werkzeug.exceptions import BadGateway

        u1, u2, u3 = (
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
        )
        p1 = {
            "lists": [
                {"list_id": u1, "name": "Main"},
                {
                    "list_id": u2,
                    "name": "Dynamic",
                    "dynamic_filtered_list": True,
                },
            ],
            "_links": {
                "next": {
                    "href": "/v3/contact_lists?cursor=abc%3D",
                },
            },
        }
        p2 = {
            "lists": [
                {"list_id": u3, "name": "Second"},
            ],
        }

        connection = Mock()
        connection.api.side_effect = [_resp(p1), _resp(p2)]

        result = contact.contact_lists(connection, "rev")
        self.assertEqual([{"id": u1, "name": "Main"}], result["lists"])
        self.assertEqual("abc=", result["next_cursor"])
        connection.api.assert_called_with(
            "rev",
            "GET",
            "https://api.cc.email/v3/contact_lists",
            params={
                "limit": 50,
                "status": "active",
                "channel_type": "email",
            },
        )

        result = contact.contact_lists(connection, "rev", "abc=")
        self.assertEqual([{"id": u3, "name": "Second"}], result["lists"])
        self.assertFalse(result["next_cursor"])
        connection.api.assert_called_with(
            "rev",
            "GET",
            "https://api.cc.email/v3/contact_lists",
            params={"cursor": "abc="},
        )

        connection.api.side_effect = [
            _resp({
                "lists": [],
                "_links": {
                    "next": {
                        "href": "https://evil.example/v3/contact_lists?cursor=x",
                    },
                },
            }),
        ]
        with self.assertRaises(BadGateway):
            contact.contact_lists(connection, "rev")
        self.assertEqual(3, connection.api.call_count)

    def test_workspace_connection_in_browser(self):
        from contextlib import ExitStack, closing
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import sqlite3
        import json
        import os
        from institution import create_app
        from browser import chromium
        from playwright.sync_api import sync_playwright
        from test_drive_import import QuietHandler, service_worker
        from werkzeug.serving import make_server

        with ExitStack() as stack:
            root = Path(stack.enter_context(TemporaryDirectory()))
            database = root / "station.sqlite3"
            (root / "manifest.json").write_text(json.dumps({"institution": "station", "schema": 1, "works": []}))
            stack.enter_context(patch.dict(os.environ, {
                "CUEPORCH_PUBLIC_URL": "",
                "CUEPORCH_CONSTANT_CONTACT_CLIENT_ID": "cid",
                "CUEPORCH_CONSTANT_CONTACT_CLIENT_SECRET": "sec",
            }))
            stack.enter_context(patch("institution.library_worker.start", return_value=lambda: None))
            provider = WorkspaceProvider("")
            stack.enter_context(patch.object(workspace_oauth, "provider_request", side_effect=provider.request))
            operator = "t" * 48
            (root / "objects").mkdir()
            app = create_app(root, database, operator, "station")
            for name, stop in app.extensions.items():
                if name.startswith("stop_") and callable(stop):
                    stack.callback(stop)
            server = make_server("127.0.0.1", 0, app, request_handler=QuietHandler, threaded=True)
            base = f"http://127.0.0.1:{server.server_port}"
            provider.redirect_uri = base + "/integration/oauth/constant-contact/callback"
            with service_worker(server), sync_playwright() as pw, chromium(pw) as browser:
                page = browser.new_page()
                callback_url = self.authorize_workspace(page, base, operator, provider)
                with closing(sqlite3.connect(str(database))) as conn:
                    row = conn.execute(
                        "SELECT token FROM workspace_integration_connection WHERE provider = ?",
                        ("constant-contact",),
                    ).fetchone()
                self.assertIsNotNone(row)
                self.assertIsInstance(row[0], bytes)
                self.assertNotIn(b"fixture-a1", row[0])
                self.verify_connected_workspace(page, base, operator, provider, callback_url)
                with closing(sqlite3.connect(str(database))) as conn:
                    row = conn.execute(
                        "SELECT token FROM workspace_integration_connection WHERE provider = ?",
                        ("constant-contact",),
                    ).fetchone()
                self.assertIsNone(row)

    def authorize_workspace(self, page, base, operator, provider):
        import json
        from urllib.parse import parse_qs, urlencode, urlsplit
        from playwright.sync_api import expect

        page.add_init_script("sessionStorage.setItem('cueporchOperator', " + json.dumps(operator) + ")")

        callback_url = None

        def authorize(route):
            nonlocal callback_url
            params = parse_qs(urlsplit(route.request.url).query)
            assert params["redirect_uri"][0] == provider.redirect_uri
            assert params["scope"][0] == contact.OAUTH.scope
            assert params["client_id"][0] == "cid"
            assert params.get("response_type", [None])[0] == "code"
            state = params["state"][0]
            callback_url = provider.redirect_uri + "?" + urlencode({"state": state, "code": "fixture-code"})
            route.fulfill(status=302, headers={"location": callback_url})

        page.route(contact.OAUTH.authorization_url + "**", authorize)

        page.goto(base + "/operator/integrations")
        page.get_by_role("button", name="Connect Constant Contact", exact=True).click()
        expect(page.get_by_role("button", name="Disconnect Constant Contact", exact=True)).to_be_visible()

        assert callback_url is not None
        return callback_url

    def verify_connected_workspace(self, page, base, operator, provider, callback_url):
        from playwright.sync_api import expect

        root = base + "/api/integrations/constant-contact/lists"
        auth = {"Authorization": "Bearer " + operator}

        response = page.request.get(root)
        self.assertEqual(response.status, 403)
        first_response = page.request.get(root, headers=auth)
        self.assertEqual(first_response.status, 200)
        first_json = first_response.json()
        self.assertEqual(first_json["lists"][0]["name"], "Station audience")
        self.assertEqual(first_json["next_cursor"], "next-1")
        second_response = page.request.get(root, headers=auth, params={"cursor": "next-1"})
        self.assertEqual(second_response.status, 200)
        second_json = second_response.json()
        self.assertEqual(second_json["lists"][0]["name"], "Arts audience")
        self.assertEqual(second_json["next_cursor"], None)
        self.assertEqual((provider.exchanges, provider.refreshes, provider.reads), (1, 1, 3))
        response = page.request.get(callback_url)
        self.assertEqual(response.status, 403)

        card = page.locator("#integrationList li").filter(
            has=page.get_by_role("heading", name="Constant Contact", exact=True)
        )
        expect(card.get_by_text("Browse videos")).to_have_count(0)

        card.get_by_role("button", name="Disconnect Constant Contact").click()
        expect(page.get_by_role("button", name="Connect Constant Contact")).to_be_visible()

        reads = provider.reads
        response = page.request.get(root, headers=auth)
        self.assertEqual(response.status, 409)
        self.assertEqual(provider.reads, reads)

    def test_existing_contact_opt_out(self):

        connection = Mock()
        connection.api.return_value = _resp(
            {
                "contacts": [
                    {
                        "contact_id": _U2,
                        "email_address": {
                            "address": "viewer@example.test",
                            "permission_to_send": "unsubscribed",
                            "confirm_status": "off",
                        },
                        "list_memberships": [],
                    }
                ]
            }
        )
        before_send = Mock()
        result = contact.send_registration(
            connection,
            "rev",
            _U1,
            "VIEWER@example.test",
            "Test",
            "Viewer",
            before_send=before_send,
        )
        self.assertEqual(result["state"], "skipped")
        connection.api.assert_called_once_with(
            "rev",
            "GET",
            contact.API_BASE + "/contacts",
            params={
                "email": "viewer@example.test",
                "status": "all",
                "include": "list_memberships",
                "limit": 2,
            },
        )
        before_send.assert_not_called()

    def test_new_contact_pending_confirmation(self):

        connection = Mock()
        connection.api.side_effect = [
            _resp({"contacts": []}),
            _resp(
                {
                    "contact_id": _U2,
                    "email_address": {
                        "address": "viewer@example.test",
                        "permission_to_send": "pending_confirmation",
                        "confirm_status": "pending",
                    },
                }
            ),
        ]
        before_send = Mock()
        result = contact.send_registration(
            connection,
            "rev",
            _U1,
            "VIEWER@example.test",
            "Test",
            "Viewer",
            before_send=before_send,
        )
        self.assertEqual(result["state"], "pending_confirmation")
        connection.api.assert_called_with(
            "rev",
            "POST",
            contact.API_BASE + "/contacts",
            json={
                "email_address": {
                    "address": "viewer@example.test",
                    "permission_to_send": "explicit",
                },
                "first_name": "Test",
                "last_name": "Viewer",
                "create_source": "Contact",
                "list_memberships": [_U1],
            },
            before_send=before_send,
        )

    def test_recheck_scope_after_401(self):
        conn = workspace_oauth.Connection(contact.OAUTH, tx=None, operator="t" * 48)
        conn.access_token = Mock(side_effect=["old", "new"])
        resp = _resp({})
        resp.status_code = 401
        resp._content_consumed = True
        guard = Mock(side_effect=[None, Conflict()])
        with patch.object(workspace_oauth, "provider_request", return_value=resp) as provider_request:
            with self.assertRaises(Conflict):
                conn.api("rev", "POST", contact.API_BASE + "/contacts", before_send=guard, json={})
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(provider_request.call_count, 1)
        conn.access_token.assert_called_with("rev", rejected="old")

    def test_existing_contact_adds_membership_only(self):
        guard = Mock()
        connection = Mock()
        connection.api.side_effect = [
            _resp({
                "contacts": [{
                    "contact_id": _U2,
                    "email_address": {
                        "address": "viewer@example.test",
                        "permission_to_send": "explicit",
                    },
                    "list_memberships": [],
                }]
            }),
            _resp({"activity_id": _U2}),
        ]

        result = contact.send_registration(
            connection, "rev", _U1, "viewer@example.test",
            "Test", "Viewer", before_send=guard,
        )

        assert result == {"state": "submitted", "activity_id": _U2}
        connection.api.assert_called_with(
            "rev", "POST",
            contact.API_BASE + "/activities/add_list_memberships",
            json={"source": {"contact_ids": [_U2]}, "list_ids": [_U1]},
            before_send=guard,
        )
