import unittest
from unittest.mock import patch
import workspace_oauth
import constant_contact_crm as contact
from test_marketo_connection import _resp


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
