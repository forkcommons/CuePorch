import json
import unittest
from unittest.mock import patch

import requests
from werkzeug.exceptions import TooManyRequests

import marketo_crm


def _resp(payload, headers=None):
    r = requests.Response()
    r.status_code = 200
    r._content = json.dumps(payload).encode()
    if headers:
        r.headers.update(headers)
    return r


class TestMarketoConnection(unittest.TestCase):
    def test_token_refresh_retry_and_cache(self):
        calls, writes = [], []
        tokens = iter(["tok-a", "tok-b"])

        def stub(method, url, **kw):
            calls.append((method, url, kw.get("headers")))
            if url.endswith("/identity/oauth/token"):
                self.assertEqual(method, "POST")
                self.assertEqual(
                    kw["params"],
                    {
                        "client_id": "cid",
                        "client_secret": "sec",
                        "grant_type": "client_credentials",
                    },
                )
                return _resp(
                    {
                        "access_token": next(tokens),
                        "expires_in": 3600,
                        "token_type": "bearer",
                    }
                )
            if len([c for c in calls if c[1].endswith("leads.json")]) == 1:
                return _resp({"success": False, "errors": [{"code": "602"}]})
            return _resp({"success": True, "result": [{"id": 1}]})

        conn = marketo_crm.MarketoConnection("https://ex.mktorest.com", "cid", "sec")
        with patch.object(marketo_crm, "provider_request", side_effect=stub):
            result = conn.api(
                "POST",
                "/rest/v1/leads.json",
                before_write=lambda: writes.append(1),
                json={"x": 1},
            )
            self.assertTrue(result["success"])
            self.assertEqual(len(calls), 4)
            self.assertEqual(len(writes), 2)
            self.assertEqual(calls[1][2]["Authorization"], "Bearer tok-a")
            self.assertEqual(calls[3][2]["Authorization"], "Bearer tok-b")
            conn.api("GET", "/rest/v1/leads.json")
            self.assertEqual(len(calls), 5)
            self.assertEqual(
                sum(1 for _, u, _ in calls if u.endswith("/identity/oauth/token")), 2
            )
            self.assertEqual(calls[4][2]["Authorization"], "Bearer tok-b")

    def test_rate_limit(self):
        calls, writes = [], []

        def stub(method, url, **kw):
            calls.append(url)
            if url.endswith("/identity/oauth/token"):
                return _resp(
                    {"access_token": "tok", "expires_in": 3600, "token_type": "bearer"}
                )
            return _resp(
                {"success": False, "errors": [{"code": "606"}]},
                headers={"Retry-After": "42"},
            )

        conn = marketo_crm.MarketoConnection("https://ex.mktorest.com", "cid", "sec")
        with patch.object(marketo_crm, "provider_request", side_effect=stub):
            with self.assertRaises(TooManyRequests) as ctx:
                conn.api(
                    "POST",
                    "/rest/v1/leads.json",
                    before_write=lambda: writes.append(1),
                    json={"x": 1},
                )
        self.assertEqual(ctx.exception.retry_after, 42)
        self.assertEqual(len(writes), 1)
        self.assertEqual(sum(1 for u in calls if u.endswith("leads.json")), 1)

    def test_program_selection(self):
        from unittest.mock import Mock
        from werkzeug.exceptions import BadGateway

        program = {
            "id": 52,
            "name": "Arts screening",
            "type": "Event",
            "channel": "Screening",
        }
        status = {"name": "Registered", "hidden": False}
        connection = Mock()
        connection.api.side_effect = [
            {"result": [program]},
            {"result": [{"name": "Screening", "progressionStatuses": [status]}]},
        ]
        self.assertEqual(marketo_crm.program(connection, 52), program)
        self.assertEqual(connection.api.call_count, 2)
        calls = connection.api.call_args_list
        self.assertEqual(calls[0][0][1], "/rest/asset/v1/program/52.json")
        self.assertEqual(calls[1][0][1], "/rest/asset/v1/channel/byName.json")
        self.assertEqual(calls[1][1]["params"], {"name": "Screening"})
        status["name"] = status["name"].lower()
        connection.api.side_effect = [
            {"result": [program]},
            {"result": [{"name": "Screening", "progressionStatuses": [status]}]},
        ]
        with self.assertRaises(BadGateway):
            marketo_crm.program(connection, 52)

    def marketo_mock(self, method, url, **kwargs):
        if method == "POST" and url == "https://mock.mktorest.com/identity/oauth/token":
            assert kwargs.get("params") == {
                "client_id": "cid",
                "client_secret": "sec",
                "grant_type": "client_credentials",
            }
            return _resp(
                {
                    "access_token": "mock-token",
                    "token_type": "bearer",
                    "expires_in": 3600,
                }
            )
        self.assertEqual(
            kwargs.get("headers", {}).get("Authorization"), "Bearer mock-token"
        )
        if (
            method == "GET"
            and url == "https://mock.mktorest.com/rest/asset/v1/programs.json"
        ):
            assert kwargs.get("params") == {"maxReturn": 50, "offset": 0}
            result = [
                {
                    "id": 52,
                    "name": "Arts screening",
                    "channel": "Screenings",
                    "type": "Event",
                }
            ]
            result += [{"type": "Default"} for _ in range(49)]
            return _resp({"success": True, "result": result})
        if (
            method == "GET"
            and url == "https://mock.mktorest.com/rest/v1/leads/describe.json"
        ):
            fields = [
                {
                    "rest": {"name": "favoriteGenre", "readOnly": False},
                    "displayName": "Favorite genre",
                    "dataType": "string",
                    "length": 80,
                },
                {
                    "rest": {"name": "id", "readOnly": True},
                    "displayName": "Id",
                    "dataType": "integer",
                    "length": 19,
                },
                {
                    "rest": {"name": "unsubscribed", "readOnly": False},
                    "displayName": "Unsubscribed",
                    "dataType": "boolean",
                },
                {
                    "rest": {"name": "emailOptOut", "readOnly": True},
                    "displayName": "Email opt out",
                    "dataType": "boolean",
                },
                {
                    "rest": {"name": "doNotCall", "readOnly": True},
                    "displayName": "Do not call",
                    "dataType": "boolean",
                },
            ]
            return _resp({"success": True, "result": fields})
        self.fail("Unknown URL: %s %s" % (method, url))

    def test_staff_reads_marketo_metadata(self):
        from institution import create_app
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from contextlib import ExitStack

        with ExitStack() as stack:
            tmp = stack.enter_context(TemporaryDirectory())
            root = Path(tmp)
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps({"institution": "station", "schema": 1, "works": []})
            )
            stack.enter_context(
                patch.dict(
                    "os.environ",
                    {
                        "CUEPORCH_PUBLIC_URL": "http://localhost",
                        "CUEPORCH_MARKETO_ORIGIN": "https://mock.mktorest.com",
                        "CUEPORCH_MARKETO_CLIENT_ID": "cid",
                        "CUEPORCH_MARKETO_CLIENT_SECRET": "sec",
                    },
                )
            )
            stack.enter_context(
                patch("institution.library_worker.start", return_value=lambda: None)
            )
            stack.enter_context(
                patch.object(
                    marketo_crm, "provider_request", side_effect=self.marketo_mock
                )
            )
            app = create_app(root, root / "workspace.sqlite", "t" * 48, "station")
            stops = [
                stop
                for name, stop in app.extensions.items()
                if name.startswith("stop_")
            ]

            def stop_app():
                while stops:
                    stops.pop()()

            stack.callback(stop_app)
            client = app.test_client()
            self.assertNotEqual(
                client.get("/api/integrations/marketo/programs").status_code, 200
            )
            self.assertNotEqual(
                client.get(
                    "/api/integrations/marketo/programs",
                    headers={"Authorization": "Bearer anonymous"},
                ).status_code,
                200,
            )
            auth = {"Authorization": "Bearer " + "t" * 48}
            r = client.get("/api/integrations/marketo/programs?offset=0", headers=auth)
            self.assertEqual(r.status_code, 200)
            body = r.get_json()
            self.assertEqual(body["programs"][0]["id"], 52)
            self.assertEqual(body["next_offset"], 50)
            r = client.get(
                "/api/integrations/marketo/programs?offset=bad", headers=auth
            )
            self.assertEqual(r.status_code, 400)
            r = client.get("/api/integrations/marketo/lead-fields", headers=auth)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(
                r.get_json()["fields"],
                [
                    {
                        "name": "favoriteGenre",
                        "label": "Favorite genre",
                        "type": "string",
                        "length": 80,
                    }
                ],
            )

    def test_engagement_provision(self):
        from unittest.mock import Mock

        guard = Mock()
        rows = [
            dict(name=name, dataType=dtype, isCustom=True, isHidden=False, length=255)
            for name, (_label, dtype) in marketo_crm.ENGAGEMENT_FIELDS.items()
        ]
        connection = Mock()
        connection.api.side_effect = [
            {"result": []},
            {"result": []},
            {"result": rows},
        ]
        result = marketo_crm.ensure_engagement_fields(connection, before_write=guard)
        self.assertEqual(set(result), set(marketo_crm.ENGAGEMENT_FIELDS))
        self.assertEqual(
            connection.api.call_args_list[1][0][1],
            "/rest/v1/programs/members/schema/fields.json",
        )
        self.assertIs(connection.api.call_args_list[1][1]["before_write"], guard)

    def test_origin(self):
        self.assertEqual(
            marketo_crm._origin("https://ex.mktorest.com"), "https://ex.mktorest.com"
        )
        self.assertIsNone(marketo_crm._origin("http://ex.mktorest.com"))
        self.assertEqual(
            marketo_crm._origin("http://127.0.0.1:8080"), "http://127.0.0.1:8080"
        )


if __name__ == "__main__":
    unittest.main()
