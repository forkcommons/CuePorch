import json
import secrets
import sqlite3
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from flask import Flask
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker
from institution import create_app
import requests


class Provider:
    def __init__(self):
        self.refreshes = 0
        self.revoked = False
        self.corrupt = False
        self.rejected = False
        from threading import Event

        self.pause_batch = False
        self.throttle_once = False
        self.batch_waiting = Event()
        self.batch_release = Event()

    @staticmethod
    def response(data=None, status=200):
        r = requests.Response()
        r._content = json.dumps(data).encode()
        r._content_consumed = True
        r.status_code = status
        return r

    def request(self, method, url, **kwargs):
        prefix = "https://api.hubapi.com/"
        assert url.startswith(prefix), url
        path = url[len(prefix) - 1 :]
        if path.startswith("/crm/"):
            return self.crm(method, path, **kwargs)
        assert method == "POST"
        data = kwargs["data"]
        assert data["client_id"] == "fixture-client"
        assert data["client_secret"] == "fixture-secret"
        if path == "/oauth/2026-03/token":
            body = {
                "refresh_token": "fixture-refresh",
                "expires_in": 1800,
                "token_type": "Bearer",
            }
            if data["grant_type"] == "authorization_code":
                assert data["code"] == "fixture-code"
                body["access_token"] = "fixture-access-one"
            elif data["grant_type"] == "refresh_token":
                assert data["refresh_token"] == "fixture-refresh"
                self.refreshes += 1
                body["access_token"] = "fixture-access-two"
            else:
                raise AssertionError(data["grant_type"])
            return self.response(body)
        if path == "/oauth/2026-03/token/revoke":
            assert data["token"] == "fixture-refresh"
            self.revoked = True
            return self.response(status=204)
        raise AssertionError(path)

    def crm(self, method, path, **kwargs):
        headers = kwargs.get("headers") or {}
        auth = headers.get("Authorization") or headers.get("authorization")

        if auth == "Bearer fixture-access-one":
            self.rejected = True
            return self.response({"error": "unauthorized"}, status=401)
        assert auth == "Bearer fixture-access-two"

        if path == "/crm/lists/2026-09/all":
            assert method == "POST"
            payload = kwargs.get("json") or {}
            assert payload == {"objectTypeId": "0-1", "count": 50} or payload == {
                "objectTypeId": "0-1",
                "count": 50,
                "after": "fixture-after",
            }
            if payload.get("after") == "fixture-after":
                return self.response(
                    {
                        "results": [
                            {
                                "listId": "20",
                                "name": "Arts audience",
                                "objectTypeId": "0-1",
                                "processingType": "DYNAMIC",
                            }
                        ]
                    },
                    status=200,
                )

            return self.response(
                {
                    "results": [
                        {
                            "listId": "10",
                            "name": "Warm list",
                            "objectTypeId": "0-1",
                            "processingType": "DYNAMIC",
                        }
                    ],
                    "paging": {
                        "next": {
                            "after": "fixture-after",
                            "link": "https://unused.invalid/unsafe",
                        }
                    },
                },
                status=200,
            )

        if path == "/crm/lists/2026-09/20":
            if self.throttle_once:
                self.throttle_once = False
                response = self.response(status=429)
                response.headers["Retry-After"] = "27"
                return response
            return self.response(
                {
                    "list": {
                        "listId": "20",
                        "objectTypeId": "0-1",
                        "processingStatus": "COMPLETE",
                    }
                },
                status=200,
            )

        if path == "/crm/lists/2026-09/20/memberships":
            assert method == "GET"
            params = kwargs.get("params") or {}
            assert params.get("limit") == 100
            if params.get("after") == "2":
                return self.response(
                    {
                        "results": [
                            {"recordId": "3"},
                            {"recordId": "4"},
                        ]
                    },
                    status=200,
                )

            return self.response(
                {
                    "results": [
                        {"recordId": "1"},
                        {"recordId": "2"},
                    ],
                    "paging": {"next": {"after": "2"}},
                },
                status=200,
            )

        assert method == "POST"
        assert path == "/crm/objects/2026-09/contacts/batch/read"
        inputs = (kwargs.get("json") or {}).get("inputs") or []
        assert [item["id"] for item in inputs] == ["1", "2", "3", "4"]

        results = [
            {
                "id": "1",
                "archived": False,
                "properties": {
                    "email": "one@example.invalid",
                    "firstname": "First",
                    "lastname": "",
                },
            },
            {
                "id": "2",
                "archived": False,
                "properties": {
                    "email": "TWO@example.invalid",
                    "firstname": "Second",
                    "lastname": "",
                },
            },
            {
                "id": "3",
                "archived": False,
                "properties": {
                    "email": "Two@example.invalid",
                    "firstname": "Duplicate",
                    "lastname": "",
                },
            },
        ]

        if self.corrupt:
            results.append(
                {
                    "id": "99",
                    "archived": False,
                    "properties": {
                        "email": "",
                        "firstname": "",
                        "lastname": "",
                    },
                }
            )

        assert all(
            set(item["properties"]) == {"email", "firstname", "lastname"}
            for item in results
        )
        if self.pause_batch:
            self.batch_waiting.set()
            assert self.batch_release.wait(8)
            results.append(
                {
                    "id": "4",
                    "archived": False,
                    "properties": {
                        "email": "canceled@example.invalid",
                        "firstname": "Canceled",
                        "lastname": "",
                    },
                }
            )
        return self.response({"results": results}, status=200)


class HubSpotCRM(unittest.TestCase):
    def test_import_segment(self):
        fixture = Provider()
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps(dict(institution="station", schema=1, works=[]))
            )
            database = root / "workspace.sqlite"
            sqlite3.connect(database).close()
            operator = secrets.token_urlsafe(32)
            remote = Flask("cueporch-service")
            service = make_server(
                "127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True
            )
            base = f"http://127.0.0.1:{service.server_port}"
            cleanup.enter_context(
                patch.dict(
                    "os.environ",
                    {
                        "CUEPORCH_PUBLIC_URL": base,
                        "CUEPORCH_HUBSPOT_CLIENT_ID": "fixture-client",
                        "CUEPORCH_HUBSPOT_CLIENT_SECRET": "fixture-secret",
                    },
                )
            )
            cleanup.enter_context(patch("event_peer.host_available", return_value=True))
            cleanup.enter_context(
                patch("workspace_oauth.provider_request", fixture.request)
            )
            cleanup.enter_context(
                patch(
                    "mail_relay.configured",
                    return_value=(
                        "127.0.0.1",
                        465,
                        "station@example.invalid",
                        "",
                        "",
                    ),
                )
            )
            import event_hubspot_sync
            from time import time

            actual_start = event_hubspot_sync.start
            clock_offset = 0

            cleanup.enter_context(patch("mail_relay.deliver"))
            cleanup.enter_context(
                patch.object(
                    event_hubspot_sync,
                    "start",
                    lambda *args: actual_start(
                        *args, clock=lambda: time() + clock_offset
                    ),
                )
            )

            stops = []

            def start_app():
                with patch(
                    "institution.library_worker.start", return_value=lambda: None
                ):
                    app = create_app(root, database, operator, "station")
                stops.extend(
                    stop
                    for name, stop in app.extensions.items()
                    if name.startswith("stop_")
                )
                service.app = app

            def stop_app():
                while stops:
                    stops.pop()()

            def restart(at):
                nonlocal clock_offset
                stop_app()
                clock_offset = at - time()
                start_app()

            self.restart = restart

            cleanup.callback(stop_app)
            start_app()
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                self.exercise(browser, base, operator, database, fixture)

    def exercise(self, browser, base, operator, database, fixture):
        page = browser.new_page()
        page.add_init_script(
            f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator)})"
        )

        def handle_hubspot(route):
            qs = parse_qs(urlsplit(route.request.url).query)
            assert "state" in qs
            scope = qs.get("scope", [""])[0]
            assert "crm.lists.read" in scope and "crm.objects.contacts.read" in scope
            redirect_uri = base + "/integration/oauth/hubspot/callback"
            assert qs["redirect_uri"][0] == redirect_uri
            location = (
                redirect_uri
                + "?"
                + urlencode({"state": qs["state"][0], "code": "fixture-code"})
            )
            route.fulfill(status=302, headers={"location": location})

        page.route("https://app.hubspot.com/oauth/authorize?*", handle_hubspot)

        page.goto(base + "/operator/integrations")
        page.click("text=Connect HubSpot")
        expect(page.get_by_text("Disconnect HubSpot")).to_be_visible()
        expect(page.get_by_role("link", name="Open webinar attendees")).to_be_visible()

        auth = {"Authorization": f"Bearer {operator}"}
        start = datetime.now(timezone.utc) + timedelta(minutes=6)
        payload = {
            "title": "CRM rehearsal",
            "slug": "crm-rehearsal",
            "visibility": "public",
            "type": "webinar",
            "latency": "standard",
            "save_replay": False,
            "schedule": {
                "timezone": "UTC",
                "repeat": "days",
                "count": 2,
                "start": start.strftime("%Y-%m-%dT%H:%M"),
                "end": (start + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M"),
            },
        }
        r = page.context.request.post(
            base + "/api/live-events", headers=auth, data=payload
        )
        assert r.ok, r.text()
        created = r.json()

        form_url = f"{base}/api/live-events/{created['id']}/registration"
        r = page.context.request.get(form_url, headers=auth)
        assert r.ok
        form = r.json()
        form["settings"]["enabled"] = True
        r = page.context.request.put(form_url, headers=auth, data=form)
        assert r.ok

        r = page.context.request.put(
            form_url + "/email",
            headers=auth,
            data={
                "revision": None,
                "settings": {
                    "confirmation": True,
                    "reminder_minutes": [1],
                    "follow_up_hours": None,
                },
            },
        )
        assert r.ok

        self.verify_import(page, base, auth, created, database, fixture)

    def verify_import(self, page, base, auth, created, database, fixture):
        page.goto(base + "/operator/live-events")
        page.get_by_role(
            "button", name="Open production for CRM rehearsal", exact=False
        ).nth(1).click()
        page.get_by_text("Attendees for this occurrence", exact=True).click()
        page.locator("#eventRoster").get_by_text("Load contact segments").click()
        page.locator("#eventRoster").get_by_text("More segments").click()
        page.locator("#eventHubSpotSegment").select_option(label="Arts audience")
        page.get_by_text("Import selected segment").click()
        expect(
            page.get_by_text(
                "2 added, 0 already registered, 1 duplicates, 1 skipped.", exact=True
            )
        ).to_be_visible()
        for label in ("First", "Second", "HubSpot"):
            expect(page.get_by_role("row").filter(has_text=label).first).to_be_visible()
        page.get_by_text("Import selected segment").click()
        expect(
            page.get_by_text(
                "0 added, 2 already registered, 1 duplicates, 1 skipped.", exact=True
            )
        ).to_be_visible()
        assert fixture.refreshes == 1
        self.verify_isolation(page, base, auth, created, database, fixture)

    def verify_isolation(self, page, base, auth, created, database, fixture):
        from contextlib import closing

        event_id = created["id"]
        first_occurrence_id = created["occurrences"][0]["id"]
        second_occurrence_id = created["occurrences"][1]["id"]
        url = f"{base}/api/live-events/{event_id}/{first_occurrence_id}/registration/hubspot"

        response = page.context.request.post(url, data={"segment": "20"}, headers=auth)
        assert response.status == 403

        headers = {**auth, "X-CuePorch-Integration": "1"}
        fixture.corrupt = True
        response = page.context.request.post(
            url, data={"segment": "20"}, headers=headers
        )
        assert response.status == 502

        with closing(sqlite3.connect(database)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT opens, source FROM live_event_registrant")
            registrant_rows = cursor.fetchall()
            assert len(registrant_rows) == 2
            for opens, source in registrant_rows:
                assert opens == second_occurrence_id
                assert source == "hubspot"

            cursor.execute("SELECT kind FROM live_event_mail_job")
            assert cursor.fetchall() == [("reminder",), ("reminder",)]

            cursor.execute(
                "SELECT token FROM workspace_integration_connection WHERE provider = 'hubspot'"
            )
            token = cursor.fetchone()[0]
            assert b"fixture-access" not in token
            assert b"fixture-refresh" not in token

        self.verify_automatic(page, base, auth, created, database, fixture)

        page.goto(f"{base}/operator/integrations")
        page.click("text=Disconnect HubSpot")
        expect(
            page.get_by_role("button", name="Connect HubSpot", exact=True)
        ).to_be_visible()
        assert fixture.revoked is True

        response = page.context.request.post(
            url, data={"segment": "20"}, headers=headers
        )
        assert response.status == 409

    def verify_automatic(self, page, base, auth, created, database, fixture):
        from time import monotonic, sleep

        fixture.corrupt = False

        page.goto(f"{base}/operator/live-events")

        assert len(created["occurrences"]) == 2

        page.get_by_role(
            "button", name="Open production for CRM rehearsal", exact=False
        ).first.click()
        page.get_by_text("Attendees for this occurrence", exact=True).click()

        page.get_by_role("button", name="Load contact segments").click()
        page.get_by_role("button", name="More segments").click()
        page.locator("#eventHubSpotSegment").select_option(label="Arts audience")

        page.get_by_role("button", name="Enable automatic refresh").click()
        expect(page.locator("#eventHubSpotSyncStatus")).to_contain_text(
            "Automatic refresh scheduled"
        )

        occurrence_id = created["occurrences"][0]["id"]
        sync_url = (
            f"{base}/api/live-events/{created['id']}/{occurrence_id}"
            "/registration/hubspot/sync"
        )

        def wait_for_receipt(ready):
            deadline = monotonic() + 6.0
            receipt = {}
            while monotonic() < deadline:
                resp = page.context.request.get(sync_url, headers=auth)
                if resp.ok:
                    receipt = resp.json()
                    if ready(receipt):
                        return receipt
                sleep(0.2)
            raise AssertionError("HubSpot sync did not reach the expected state.")

        receipt = wait_for_receipt(lambda value: value.get("last_result"))
        last_result = receipt["last_result"]

        assert last_result["added"] == 2
        assert last_result["occurrence"] == occurrence_id
        assert receipt["next_at"] > receipt["last_at"]

        page.get_by_role("button", name="Refresh sync status").click()
        expect(page.locator("#eventHubSpotSyncStatus")).to_contain_text("2 added")
        expect(page.locator("#eventRoster")).to_contain_text("First")

        due = receipt["next_at"]
        fixture.throttle_once = True
        self.restart(due)
        retry = wait_for_receipt(lambda value: value.get("state") == "retry")
        assert retry["next_at"] >= due + 27
        assert retry["last_at"] == receipt["last_at"]
        self.restart(retry["next_at"])
        refreshed = wait_for_receipt(
            lambda value: (
                value.get("last_result") and value["last_at"] >= retry["next_at"]
            )
        )
        assert refreshed["last_result"]["added"] == 0
        assert refreshed["last_result"]["existing"] == 2
        assert refreshed["last_result"]["occurrence"] == occurrence_id

        page.get_by_role("button", name="Stop automatic refresh").click()
        expect(page.locator("#eventHubSpotSyncStatus")).to_contain_text(
            "Automatic refresh is off"
        )

        disabled = page.context.request.get(sync_url, headers=auth).json()
        assert disabled["enabled"] is False
        assert disabled["state"] == "off"

        self.verify_canceled(page, auth, sync_url, database, fixture)

    def verify_canceled(self, page, auth, sync_url, database, fixture):
        from filelock import FileLock

        # Current browser remains on the first occurrence and the selected
        # segment even after automatic refresh is stopped.
        fixture.pause_batch = True
        try:
            page.get_by_role("button", name="Enable automatic refresh").click()
            assert fixture.batch_waiting.wait(5)
            page.get_by_role("button", name="Stop automatic refresh").click()
            expect(page.locator("#eventHubSpotSyncStatus")).to_have_text(
                "Automatic refresh is off"
            )
        finally:
            fixture.batch_release.set()

        with FileLock(str(database) + ".hubspot-sync.lock", timeout=5):
            sync_response = page.request.get(sync_url, headers=auth)
            sync_state = sync_response.json()
            assert sync_state["enabled"] is False
            assert sync_state["state"] == "off"

            roster_url = (
                sync_url.removesuffix("/registration/hubspot/sync") + "/registrants"
            )
            roster_response = page.request.get(roster_url, headers=auth)
            roster = roster_response.json()
            assert roster["total"] == 2
            attendees = roster["attendees"]
            assert all(
                attendee.get("email") != "canceled@example.invalid"
                for attendee in attendees
            )

        # Proves a pending provider response cannot mutate the canceled
        # occurrence.
