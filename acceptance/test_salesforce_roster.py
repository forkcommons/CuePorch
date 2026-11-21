import json
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from institution import create_app
import event_hubspot_sync
import workspace_oauth
from test_salesforce_connection import ORIGIN, FIRST, SECOND, salesforce_request
from test_marketo_connection import _resp

TARGET = SECOND["Id"]
SOBJECT_URL = "/services/data/v64.0/sobjects/Campaign/" + TARGET
QUERY_URL = "/services/data/v64.0/query"
CONT_URL = "/services/data/v64.0/query/members-200"
WHERE = "WHERE CampaignId = '%s'" % TARGET


def _member(i, email, first="Ada", last="Example", opted=False, campaign=TARGET):
    return {"Id": "00v%012d" % i, "CampaignId": campaign,
            "LeadId": "00Q%012d" % i, "ContactId": None,
            "Email": email, "FirstName": first, "LastName": last,
            "HasOptedOutOfEmail": opted}


class RosterProvider:
    def __init__(self):
        self.now = time.time()
        self.retry = False
        self.include_scheduled = False
        self.foreign_member = False

    def _auth(self, kwargs):
        h = kwargs.get("headers") or {}
        auth = h.get("Authorization") or h.get("authorization") or ""
        assert auth in ("Bearer a1", "Bearer a2"), auth

    def request(self, method, url, **kwargs):
        path = url.removeprefix(ORIGIN)
        if method == "POST" and url == ORIGIN + "/services/oauth2/revoke":
            assert kwargs["data"]["token"] in ("r1", "r2")
            response = _resp({})
            response._content_consumed = True
            return response
        if method.upper() == "GET" and path == SOBJECT_URL:
            self._auth(kwargs)
            return _resp(SECOND)
        if method.upper() == "GET" and path == QUERY_URL:
            q = (kwargs.get("params") or {}).get("q", "")
            if "FROM CampaignMember" in q:
                self._auth(kwargs)
                assert WHERE in q, q
                if self.retry:
                    r = _resp({}, headers={"Retry-After": "15"})
                    r.status_code = 429
                    r._content_consumed = True
                    return r
                return _resp({"totalSize": 3, "done": False,
                              "nextRecordsUrl": CONT_URL,
                              "records": [
                                  _member(1, "online@example.test"),
                                  _member(2, "suppressed@example.test",
                                          "Sup", "Pressed", True)]})
        if method.upper() == "GET" and path == CONT_URL:
            self._auth(kwargs)
            recs = [_member(3, "ONLINE@example.test")]
            if self.foreign_member:
                recs.append(_member(4, "foreign@example.test",
                                    campaign="701000000000001"))
            if self.include_scheduled:
                recs.append(_member(5, "scheduled@example.test"))
            return _resp({"totalSize": 2 + len(recs), "done": True,
                          "records": recs})
        return salesforce_request(method, url, **kwargs)


class TestSalesforceRoster(unittest.TestCase):
    def test_roster(self):
        provider = RosterProvider()
        with ExitStack() as stack:
            tmp = stack.enter_context(TemporaryDirectory())
            root = Path(tmp)
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps({
                "institution": "station", "schema": 1, "works": [],
            }))

            stack.enter_context(patch.dict("os.environ", {
                "CUEPORCH_PUBLIC_URL": "",
                "CUEPORCH_SALESFORCE_ORIGIN": ORIGIN,
                "CUEPORCH_SALESFORCE_CLIENT_ID": "cid",
                "CUEPORCH_SALESFORCE_CLIENT_SECRET": "sec",
            }))
            stack.enter_context(patch("institution.library_worker.start", return_value=lambda: None))
            stack.enter_context(patch.object(workspace_oauth, "provider_request", provider.request))
            stack.enter_context(patch("event_peer.host_available", return_value=True))

            original_start = event_hubspot_sync.start

            def start_wrapper(*args, **kwargs):
                if kwargs.get("provider") == "salesforce":
                    kwargs["clock"] = lambda: provider.now
                return original_start(*args, **kwargs)

            stack.enter_context(patch.object(event_hubspot_sync, "start", start_wrapper))

            db = root / "db.sqlite"
            app = create_app(root, db, "t" * 48, "station")
            for key, stop in app.extensions.items():
                if key.startswith("stop_") and callable(stop):
                    stack.callback(stop)

            client = app.test_client()
            headers = {
                "Authorization": "Bearer " + "t" * 48,
                "X-CuePorch-Integration": "1",
            }

            resp = client.post("/api/integrations/salesforce/connect", json={}, headers=headers)
            auth_url = resp.get_json()["authorization_url"]
            state = parse_qs(urlsplit(auth_url).query)["state"][0]

            resp = client.get(
                "/integration/oauth/salesforce/callback",
                query_string={"state": state, "code": "fixture-code"},
                headers=headers,
            )
            self.assertEqual(resp.status_code, 303)
            self.exercise(client, headers, provider, app)

    def exercise(self, client, headers, provider, app):
        now = datetime.now(timezone.utc)
        start = now + timedelta(minutes=10)
        end = start + timedelta(minutes=50)
        payload = {
            "title": "Salesforce rehearsal",
            "slug": "salesforce-rehearsal",
            "visibility": "public",
            "type": "webinar",
            "latency": "standard",
            "save_replay": False,
            "schedule": {
                "timezone": "UTC",
                "repeat": "days",
                "count": 2,
                "start": start.strftime("%Y-%m-%dT%H:%M"),
                "end": end.strftime("%Y-%m-%dT%H:%M"),
            },
        }
        r = client.post("/api/live-events", json=payload, headers=headers)
        assert r.status_code in (200, 201), r.get_json()
        event = r.get_json()
        event_id = event["id"]
        occurrences = event["occurrences"]
        opens = occurrences[1]["id"]
        r = client.get(f"/api/live-events/{event_id}/registration", headers=headers)
        reg = r.get_json()
        reg["settings"]["enabled"] = True
        assert client.put(f"/api/live-events/{event_id}/registration", json=reg, headers=headers).status_code == 200
        base = f"/api/live-events/{event_id}/{opens}"

        def registrants():
            return client.get(base + "/registrants", headers=headers).get_json()["attendees"]

        assert registrants() == []
        payload = {"campaign": TARGET}
        provider.foreign_member = True
        r = client.post(base + "/registration/salesforce", json=payload, headers=headers)
        assert r.status_code == 502 and registrants() == []
        provider.foreign_member = False
        self.exercise_browser(app, headers["Authorization"].removeprefix("Bearer "))
        data = client.post(base + "/registration/salesforce", json=payload, headers=headers).get_json()
        assert data["existing"] == 1 and data["added"] == 0
        first = f"/api/live-events/{event_id}/{occurrences[0]['id']}/registrants"
        assert client.get(first, headers=headers).get_json()["attendees"] == []

        r = client.put(base + "/registration/salesforce/sync", json=payload, headers=headers)
        assert r.status_code == 200
        data = r.get_json()
        due = event_hubspot_sync.local_midnight(opens, "UTC")
        assert data["campaign"] == TARGET
        assert data["next_at"] == due

        def poll(pred):
            deadline = time.monotonic() + 7
            while time.monotonic() < deadline:
                current = client.get(base + "/registration/salesforce/sync", headers=headers).get_json()
                if pred(current):
                    return current
                time.sleep(0.1)
            raise AssertionError(current)

        provider.retry = True
        provider.now = due
        data = poll(lambda x: x.get("state") == "retry")
        next_due = due + 15
        assert data["next_at"] == next_due
        provider.retry = False
        provider.include_scheduled = True
        provider.now = next_due
        data = poll(lambda x: x.get("last_at") == next_due)
        assert data["last_result"]["added"] == 1
        assert {a["email"]: a["source"] for a in registrants()} == {
            "online@example.test": "salesforce",
            "scheduled@example.test": "salesforce-scheduled",
        }
        assert client.post("/api/integrations/salesforce/disconnect", json={}, headers=headers).status_code == 200
        data = client.get(base + "/registration/salesforce/sync", headers=headers).get_json()
        assert data["state"] == "problem" and data["available"] is False

    def exercise_browser(self, app, operator):
        from playwright.sync_api import expect, sync_playwright
        from werkzeug.serving import make_server
        from test_drive_import import QuietHandler, service_worker
        from browser import chromium

        service = make_server("127.0.0.1", 0, app, threaded=True,
                              request_handler=QuietHandler)
        base = f"http://127.0.0.1:{service.server_port}"
        with service_worker(service), sync_playwright() as p, chromium(p) as browser:
            page = browser.new_page()
            page.add_init_script(
                "sessionStorage.setItem('cueporchOperator', %s);"
                % json.dumps(operator)
            )
            page.goto(f"{base}/operator/live-events")
            page.get_by_role(
                "button", name="Open production for Salesforce rehearsal"
            ).nth(1).click()
            page.get_by_text("Attendees for this occurrence", exact=True).click()
            panel = page.locator("#eventSalesforceImport")
            panel.get_by_role("button", name="Load Salesforce campaigns").click()
            panel.get_by_role("button", name="More Salesforce campaigns").click()
            panel.locator("#sfCampaign").select_option(label="Arts")
            panel.get_by_role("button", name="Import selected campaign").click()
            expect(page.locator("#sfImportStatus")).to_contain_text(
                "Added 1. Already registered 0. Duplicates 1. Skipped 1."
            )
            expect(
                page.locator("#eventRosterRows").get_by_text("online@example.test")
            ).to_be_visible()
            panel.get_by_role("button", name="Enable automatic import").click()
            expect(page.locator("#sfImportStatus")).to_contain_text("Scheduled.")
            panel.get_by_role("button", name="Stop automatic import").click()
            expect(page.locator("#sfImportStatus")).to_contain_text("Off.")
