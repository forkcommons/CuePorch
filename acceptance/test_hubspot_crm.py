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


from urllib.parse import unquote


def outbound_fixture(provider, method, path, kwargs):
    path = path.split("?", 1)[0]
    contacts_prefix = "/crm/objects/2026-09/contacts/"

    if method == "GET" and path == "/crm/lists/2026-09/10":
        return provider.response(
            {
                "list": {
                    "listId": "10",
                    "name": "Warm list",
                    "processingType": "MANUAL",
                    "processingStatus": "COMPLETE",
                    "objectTypeId": "0-1",
                }
            }
        )

    if method == "GET" and path == "/communication-preferences/2026-09/definitions":
        return provider.response(
            {
                "status": "COMPLETE",
                "results": [
                    {
                        "id": "44",
                        "name": "Event updates",
                        "communicationMethod": "Email",
                        "isActive": True,
                        "isInternal": False,
                    }
                ],
            }
        )

    statuses_prefix = "/communication-preferences/2026-09/statuses/"
    if method == "GET" and path.startswith(statuses_prefix):
        assert kwargs["params"]["channel"] == "EMAIL"
        rest = path[len(statuses_prefix) :]
        unsub_all = rest.endswith("/unsubscribe-all")
        raw_email = rest[: -len("/unsubscribe-all")] if unsub_all else rest
        email = unquote(raw_email)
        if not unsub_all and email == "blocked@example.invalid":
            results = [
                {
                    "subscriptionId": 44,
                    "subscriberIdString": email,
                    "channel": "EMAIL",
                    "status": "UNSUBSCRIBED",
                }
            ]
        else:
            results = []
        return provider.response({"status": "COMPLETE", "results": results})

    if method == "GET" and path.startswith(contacts_prefix):
        assert kwargs["params"]["idProperty"] == "email"
        email = unquote(path[len(contacts_prefix) :])
        rec = provider.outbound_contacts.get(email)
        if rec is None or rec.get("archived", False):
            return provider.response(
                {
                    "status": "error",
                    "message": "Contact not found",
                    "category": "OBJECT_NOT_FOUND",
                },
                status=404,
            )
        props = {"email": rec["email"]}
        if "firstname" in rec:
            props["firstname"] = rec["firstname"]
        if "lastname" in rec:
            props["lastname"] = rec["lastname"]
        return provider.response(
            {"id": rec["id"], "properties": props, "archived": False}
        )

    if method == "POST" and path == "/crm/objects/2026-09/contacts":
        props = kwargs["json"]["properties"]
        assert "email" in props
        assert set(props) <= {"email", "firstname", "lastname"}
        email = props["email"]
        new_id = str(len(provider.outbound_contacts) + 1)
        record = {"id": new_id, "email": email, "archived": False}
        if "firstname" in props:
            record["firstname"] = props["firstname"]
        if "lastname" in props:
            record["lastname"] = props["lastname"]

        provider.outbound_writes.append(("create", email))
        if email == "conflict@example.invalid":
            if email not in provider.outbound_contacts:
                provider.outbound_contacts[email] = record
            return provider.response(
                {
                    "status": "error",
                    "message": "Contact already exists",
                    "category": "CONFLICT",
                },
                status=409,
            )

        provider.outbound_contacts[email] = record
        out_props = {"email": email}
        if "firstname" in record:
            out_props["firstname"] = record["firstname"]
        if "lastname" in record:
            out_props["lastname"] = record["lastname"]
        return provider.response(
            {"id": new_id, "properties": out_props, "archived": False}, status=201
        )

    if method == "PATCH" and path.startswith(contacts_prefix):
        contact_id = path[len(contacts_prefix) :]
        body = kwargs.get("json") or {}
        props = body.get("properties", {})
        assert set(props) <= {"firstname", "lastname"}
        rec = None
        for candidate in provider.outbound_contacts.values():
            if candidate["id"] == contact_id:
                rec = candidate
                break
        assert rec is not None
        if "firstname" in props:
            rec["firstname"] = props["firstname"]
        if "lastname" in props:
            rec["lastname"] = props["lastname"]
        provider.outbound_writes.append(("update", rec["email"]))
        out_props = {"email": rec["email"]}
        if "firstname" in rec:
            out_props["firstname"] = rec["firstname"]
        if "lastname" in rec:
            out_props["lastname"] = rec["lastname"]
        return provider.response(
            {"id": rec["id"], "properties": out_props, "archived": False}
        )

    if method == "PUT" and path == "/crm/lists/2026-09/10/memberships/add":
        record_ids = kwargs["json"]
        assert len(record_ids) == 1
        record_id = record_ids[0]
        known_ids = {rec["id"] for rec in provider.outbound_contacts.values()}
        assert record_id in known_ids
        provider.outbound_members.add(record_id)
        provider.outbound_writes.append(("add", record_id))
        return provider.response({})

    member_prefix = "/crm/lists/2026-09/records/0-1/"
    member_suffix = "/memberships"
    if (
        method == "GET"
        and path.startswith(member_prefix)
        and path.endswith(member_suffix)
    ):
        member_id = path[len(member_prefix) : -len(member_suffix)]
        if member_id in provider.outbound_members:
            return provider.response(
                {"results": [{"listId": "10", "recordId": member_id}], "total": 1}
            )
        return provider.response({"results": [], "total": 0})

    return None


class Provider:
    def __init__(self):
        self.refreshes = 0
        self.revoked = False
        self.outbound_contacts = {}
        self.outbound_members = set()
        self.outbound_writes = []
        self.engagement_posts = []
        self.engagement_throttled = False
        self.corrupt = False
        self.rejected = False
        from threading import Event

        self.pause_batch = False
        self.throttle_once = False
        self.segment_empty = False
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
        if method == "POST" and path == "/events/2026-09/send":
            assert kwargs["headers"]["Authorization"] in (
                "Bearer fixture-access-one",
                "Bearer fixture-access-two",
            )
            payload = kwargs["json"]
            assert set(payload) == {
                "eventName",
                "objectId",
                "occurredAt",
                "uuid",
                "properties",
            }
            self.engagement_posts.append(payload)
            if not self.engagement_throttled:
                self.engagement_throttled = True
                response = self.response({}, status=429)
                response.headers["Retry-After"] = "420"
                return response
            return self.response({}, status=204)
        if path.startswith(("/crm/", "/communication-preferences/")):
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

        response = outbound_fixture(self, method, path, kwargs)
        if response is not None:
            return response

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
                            "processingType": "MANUAL",
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
                        "processingType": "DYNAMIC",
                        "processingStatus": "COMPLETE",
                    }
                },
                status=200,
            )

        if path == "/crm/lists/2026-09/20/memberships":
            assert method == "GET"
            if self.segment_empty:
                return self.response({"results": []})
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
                        "CUEPORCH_HUBSPOT_OUTBOUND": "1",
                        "CUEPORCH_HUBSPOT_ENGAGEMENT": "1",
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
            import event_hubspot_outbound
            import event_hubspot_sync
            from time import time

            actual_start = event_hubspot_sync.start
            clock_offset = 0
            actual_engagement_start = event_hubspot_outbound.start
            engagement_clock_offset = 0

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
            cleanup.enter_context(
                patch.object(
                    event_hubspot_outbound,
                    "start",
                    lambda *args, **kwargs: actual_engagement_start(
                        *args, clock=lambda: time() + engagement_clock_offset, **kwargs
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

            def set_engagement_clock(at, *, restart=False):
                nonlocal engagement_clock_offset
                if restart:
                    stop_app()
                engagement_clock_offset = at - time()
                if restart:
                    start_app()

            self.set_engagement_clock = set_engagement_clock
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
            assert (
                "analytics.behavioral_events.send"
                in qs.get("optional_scope", [""])[0].split()
            )
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
                "end": (start + timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M"),
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
        self.verify_outbound(page, base, auth, created, fixture)
        self.verify_engagement(page, base, auth, created, database, fixture)

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

    def verify_outbound(self, page, base, auth, created, fixture):
        import time

        event_id = created["id"]
        opens = created["occurrences"][1]["id"]
        outbound_url = (
            f"{base}/api/live-events/{event_id}/{opens}/registration/hubspot/outbound"
        )

        def wait_for(**counts):
            deadline = time.time() + 10
            receipt = None
            while time.time() < deadline:
                response = page.request.get(outbound_url, headers=auth)
                assert response.ok
                receipt = response.json()
                states = receipt.get("states") or {}
                if all(states.get(key, 0) == value for key, value in counts.items()):
                    return receipt
                time.sleep(0.2)
            raise AssertionError(
                f"Timed out waiting for outbound states {counts}; last receipt: {receipt!r}"
            )

        def register(email, first_name, last_name, consent):
            visitor = page.context.browser.new_page()
            try:
                visitor.goto(f"{base}/events/crm-rehearsal?occurrence={opens}")
                visitor.fill("input[name=email]", email)
                visitor.fill("input[name=first_name]", first_name)
                visitor.fill("input[name=last_name]", last_name)
                box = visitor.locator("input[name=hubspot_consent]")
                expect(box).not_to_be_checked()
                if consent:
                    box.check()
                visitor.locator("form button[type=submit]").first.click()
                expect(box).to_have_count(0)
            finally:
                visitor.close()

        page.goto(f"{base}/operator/live-events")
        page.get_by_role("button", name="Open production for CRM rehearsal").nth(
            1
        ).click()

        page.get_by_text("Attendees for this occurrence", exact=True).click()

        page.get_by_role("button", name="Load contact segments").click()
        page.locator("#eventHubSpotSegment").select_option(label="Warm list")
        page.get_by_role("button", name="Load email subscriptions").click()
        page.locator("#eventHubSpotSubscription").select_option(label="Event updates")

        page.get_by_role("button", name="Enable registration delivery").click()
        expect(page.locator("#eventHubSpotOutboundStatus")).to_contain_text(
            "Registration delivery enabled"
        )

        receipt = wait_for(sent=0, skipped=0, waiting=0)
        assert receipt.get("enabled") is True

        register("local@example.invalid", "Local", "User", False)
        assert "local@example.invalid" not in fixture.outbound_contacts
        assert not any(
            op in ("create", "update") and value == "local@example.invalid"
            for op, value in fixture.outbound_writes
        )
        wait_for(sent=0, skipped=0, waiting=0)

        register("blocked@example.invalid", "Blocked", "User", True)
        wait_for(skipped=1)
        assert "blocked@example.invalid" not in fixture.outbound_contacts
        assert not any(
            op in ("create", "update") and value == "blocked@example.invalid"
            for op, value in fixture.outbound_writes
        )

        register("agreed@example.invalid", "Agreed", "User", True)
        wait_for(sent=1)
        contact = fixture.outbound_contacts["agreed@example.invalid"]
        assert (contact["firstname"], contact["lastname"]) == ("Agreed", "User")

        register("conflict@example.invalid", "Conflict", "User", True)
        wait_for(sent=2)

        creates = [value for op, value in fixture.outbound_writes if op == "create"]
        updates = [value for op, value in fixture.outbound_writes if op == "update"]
        assert creates == ["agreed@example.invalid", "conflict@example.invalid"]
        assert len(updates) == 1
        assert updates == ["conflict@example.invalid"]

        expected_members = {
            fixture.outbound_contacts["agreed@example.invalid"]["id"],
            fixture.outbound_contacts["conflict@example.invalid"]["id"],
        }
        assert fixture.outbound_members == expected_members

        response = page.request.put(
            f"{base}/api/policy/event_contact_sync",
            headers=auth,
            data={"required": True},
        )
        assert response.ok

        register("held@example.invalid", "Held", "User", True)
        wait_for(waiting=1)
        assert "held@example.invalid" not in fixture.outbound_contacts
        assert not any(
            op in ("create", "update") and value == "held@example.invalid"
            for op, value in fixture.outbound_writes
        )

        response = page.request.put(
            f"{base}/api/policy/event_contact_sync",
            headers=auth,
            data={"required": False},
        )
        assert response.ok
        wait_for(sent=3, waiting=0)

        page.get_by_role("button", name="Stop registration delivery").click()
        expect(page.locator("#eventHubSpotOutboundStatus")).to_contain_text(
            "Registration delivery is off"
        )

        visitor = page.context.browser.new_page()
        try:
            visitor.goto(f"{base}/events/crm-rehearsal?occurrence={opens}")
            expect(visitor.locator("input[name=hubspot_consent]")).to_have_count(0)
        finally:
            visitor.close()

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

        self.verify_active_segment(
            page, auth, sync_url, database, fixture, refreshed, wait_for_receipt
        )

        page.get_by_role("button", name="Stop automatic refresh").click()
        expect(page.locator("#eventHubSpotSyncStatus")).to_contain_text(
            "Automatic refresh is off"
        )

        disabled = page.context.request.get(sync_url, headers=auth).json()
        assert disabled["enabled"] is False
        assert disabled["state"] == "off"

        self.verify_canceled(page, auth, sync_url, database, fixture)

    def verify_active_segment(
        self, page, auth, sync_url, database, fixture, receipt, wait_for_receipt
    ):
        from contextlib import closing
        import event_attendance

        roster_url = (
            sync_url.removesuffix("/registration/hubspot/sync") + "/registrants"
        )

        response = page.request.post(
            roster_url,
            headers=auth,
            data={"email": "TWO@example.invalid", "first_name": "Second"},
        )
        expect(response).to_be_ok()

        link_sql = (
            "SELECT l.* FROM live_attendance_link l "
            "JOIN live_event_hubspot_member m ON m.link = l.id "
            "WHERE m.contact_id = '1' AND m.event = l.event"
        )

        with closing(sqlite3.connect(database)) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(link_sql).fetchone()
            assert row is not None, "expected attendance link for contact_id=1"
            link = dict(row)
            active_now = event_attendance.active(
                con,
                link["id"],
                [
                    link["event"],
                    link["event_revision"],
                    link["opens"],
                    link["form_revision"],
                    False,
                ],
            )
            assert active_now is True, "link for contact_id=1 should be active"

        def check_active(expected):
            with closing(sqlite3.connect(database)) as con:
                con.row_factory = sqlite3.Row
                row = con.execute(link_sql).fetchone()
                assert row is not None, "stored attendance link row is missing"
                assert row["id"] == link["id"]
                active_now = event_attendance.active(
                    con,
                    row["id"],
                    [
                        row["event"],
                        row["event_revision"],
                        row["opens"],
                        row["form_revision"],
                        False,
                    ],
                )
                assert active_now is expected, f"expected link active={expected}"

        def roster_emails():
            payload = page.request.get(roster_url, headers=auth).json()
            assert "attendees" in payload
            return sorted(item["email"].casefold() for item in payload["attendees"])

        fixture.segment_empty = True
        self.restart(receipt["next_at"])
        removed = wait_for_receipt(
            lambda value: (value.get("last_result") or {}).get("removed") == 1
        )

        page.get_by_role("button", name="Refresh sync status").click()
        expect(page.locator("#eventHubSpotSyncStatus")).to_contain_text(
            "1 filtered out"
        )

        assert roster_emails() == ["two@example.invalid"]
        check_active(False)

        fixture.segment_empty = False
        self.restart(removed["next_at"])
        wait_for_receipt(
            lambda value: (value.get("last_result") or {}).get("restored") == 1
        )

        page.get_by_role("button", name="Refresh sync status").click()
        expect(page.locator("#eventHubSpotSyncStatus")).to_contain_text("1 restored")

        assert roster_emails() == ["one@example.invalid", "two@example.invalid"]
        check_active(True)

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

    def verify_engagement(self, page, base, auth, created, database, fixture):
        eid = created["id"]
        url = f"{base}/api/live-events/{eid}/registration"
        form = page.request.get(url, headers=auth).json()
        form.setdefault("settings", {})["attendance_analytics"] = True
        response = page.request.put(url, headers=auth, data=form)
        assert response.ok
        page.goto(f"{base}/operator/live-events")
        page.get_by_role("button", name="Open production for CRM rehearsal").nth(
            1
        ).click()
        page.get_by_text("Attendees for this occurrence", exact=True).click()
        page.get_by_role("button", name="Load contact segments").click()
        page.locator("#eventHubSpotSegment").select_option(label="Warm list")
        page.get_by_role("button", name="Load email subscriptions").click()
        page.locator("#eventHubSpotSubscription").select_option(label="Event updates")
        page.locator("#eventHubSpotEngagementEvent").fill("pe123_cueporch_webinar")
        page.get_by_role("button", name="Enable registration delivery").click()
        expect(page.locator("#eventHubSpotOutboundStatus")).to_contain_text(
            "pe123_cueporch_webinar"
        )
        slot = created["occurrences"][1]
        cases = (
            ("Approved", "appr@example.invalid", True),
            ("Withdrew", "withdrew@example.invalid", True),
            ("Declined", "declined@example.invalid", False),
            ("Blocked", "blocked@example.invalid", True),
        )
        withdrawn = None
        browser = page.context.browser
        for name, email, opt_in in cases:
            p = browser.new_page()
            try:
                p.goto(f"{base}/events/crm-rehearsal?occurrence={slot['id']}")
                p.fill("input[name=email]", email)
                p.fill("input[name=first_name]", name)
                p.fill("input[name=last_name]", "Viewer")
                consent = p.locator("input[name=hubspot_engagement_consent]")
                expect(consent).not_to_be_checked()
                if opt_in:
                    consent.check()
                p.locator("form button[type=submit]").first.click()
                expect(consent).to_have_count(0)
                grant = p.locator("#eventLobby").get_attribute("data-viewer-grant")
                if opt_in:
                    assert grant
                if email == "withdrew@example.invalid":
                    withdrawn = grant
            finally:
                p.close()
        return self.exercise_engagement_delivery(
            page, base, auth, created, database, fixture, withdrawn
        )

    def exercise_engagement_delivery(
        self, page, base, auth, created, database, fixture, withdrawn
    ):
        import sqlite3
        import uuid
        from contextlib import closing
        import event_analytics
        import event_registrants

        event = created["id"]
        opens = created["occurrences"][1]["id"]

        fixture.outbound_contacts.update(
            {
                "appr@example.invalid": dict(
                    id="71", email="appr@example.invalid", archived=False
                ),
                "blocked@example.invalid": dict(
                    id="72", email="blocked@example.invalid", archived=False
                ),
            }
        )

        with closing(sqlite3.connect(database)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT j.sharing, r.email
                FROM live_event_hubspot_engagement_job j
                JOIN live_event_registrant r ON r.id = j.registrant
                WHERE j.event = ? AND j.opens = ?
                """,
                (event, opens),
            ).fetchall()

            emails = {row["email"] for row in rows}
            assert emails == {
                "appr@example.invalid",
                "blocked@example.invalid",
                "withdrew@example.invalid",
            }, emails

            for row in rows:
                sharing = row["sharing"]
                for phase, watched in (("live", 3300), ("replay", 1200)):
                    nonce = f"synthetic-{phase}-{sharing}-{uuid.uuid4().hex}"
                    event_analytics.record(
                        con,
                        event,
                        opens,
                        phase,
                        nonce,
                        body={
                            "sequence": 1,
                            "watched": watched,
                            "played": True,
                            "playing": False,
                        },
                    )
                    event_registrants.link_sample(con, nonce, sharing)
            con.commit()

        resp = page.request.post(
            f"{base}/api/event-playback/withdraw-sharing",
            headers={"X-CuePorch-Event-Viewer": withdrawn, "Origin": base},
        )
        assert resp.status == 204

        self.verify_engagement_retry(page, base, auth, created, database, fixture)

    def verify_engagement_retry(self, page, base, auth, created, database, fixture):
        from time import monotonic, sleep
        from contextlib import closing

        slot = created["occurrences"][1]
        end_dt = datetime.fromisoformat(slot["end"])
        end = int(end_dt.timestamp())

        policy_url = f"{base}/api/policy/event_engagement_sync"
        outbound_url = (
            f"{base}/api/live-events/{created['id']}/{slot['id']}"
            "/registration/hubspot/outbound"
        )

        r = page.request.put(policy_url, headers=auth, data={"required": True})
        assert r.ok, f"engagement PUT required=True failed: {r.status} {r.text()}"

        self.set_engagement_clock(end + 3601)

        initial_writes = list(fixture.outbound_writes)

        deadline = monotonic() + 10.0
        states = None
        while monotonic() < deadline:
            resp = page.request.get(outbound_url, headers=auth)
            assert resp.ok, f"engagement GET failed: {resp.status}"
            states = resp.json()["engagement_states"]
            if (
                states.get("waiting") == 2
                and states.get("canceled") == 1
                and states.get("engagement_posts", 0) == 0
            ):
                break
            sleep(0.1)
        else:
            raise AssertionError(
                "engagement_states did not reach waiting=2, canceled=1, "
                f"engagement_posts=0 while on hold: {states}"
            )
        assert list(fixture.outbound_writes) == initial_writes, (
            "fixture.outbound_writes changed during the engagement-only hold phase"
        )
        assert list(fixture.engagement_posts) == []

        r = page.request.put(policy_url, headers=auth, data={"required": False})
        assert r.ok, f"engagement PUT required=False failed: {r.status} {r.text()}"

        deadline = monotonic() + 10.0
        job_row = None
        while monotonic() < deadline:
            with closing(sqlite3.connect(database)) as conn:
                job_row = conn.execute(
                    "SELECT j.due, j.attempts FROM live_event_hubspot_engagement_job j "
                    "JOIN live_event_registrant r ON r.id = j.registrant "
                    "WHERE j.state = 'queued' AND r.email = ? "
                    "AND j.attempts = 1",
                    ("appr@example.invalid",),
                ).fetchone()
            has_429 = len(fixture.engagement_posts) == 1
            if job_row is not None and has_429:
                break
            sleep(0.1)
        else:
            raise AssertionError(
                "no queued live_event_hubspot_engagement_job (attempts=1) for "
                "appr@example.invalid paired with a 429 engagement post"
            )

        job_due, job_attempts = job_row
        assert job_attempts == 1, job_attempts
        assert job_due >= end + 4021, (
            f"retry due {job_due} must be >= end+4021 ({end + 4021})"
        )

        self.set_engagement_clock(job_due - 30, restart=True)
        sleep(1.25)
        assert len(fixture.engagement_posts) == 1, (
            f"expected exactly 1 engagement post before retry due, got "
            f"{len(fixture.engagement_posts)}"
        )

        self.set_engagement_clock(job_due + 1)

        deadline = monotonic() + 10.0
        states = None
        while monotonic() < deadline:
            resp = page.request.get(outbound_url, headers=auth)
            assert resp.ok, f"engagement GET failed: {resp.status}"
            states = resp.json()["engagement_states"]
            if (
                states.get("accepted") == 1
                and states.get("skipped") == 1
                and states.get("canceled") == 1
            ):
                break
            sleep(0.1)
        else:
            raise AssertionError(
                "engagement_states did not reach accepted=1, skipped=1, "
                f"canceled=1 after retry: {states}"
            )

        assert len(fixture.engagement_posts) == 2, (
            f"expected 2 engagement posts (429 + retry), got "
            f"{len(fixture.engagement_posts)}"
        )
        original_json = fixture.engagement_posts[0]
        retry_json = fixture.engagement_posts[1]
        assert original_json == retry_json, (
            "retry payload must exactly match the original request"
        )

        payload = retry_json
        assert payload["objectId"] == "71", payload.get("objectId")
        assert payload["eventName"] == "pe123_cueporch_webinar", payload.get(
            "eventName"
        )
        expected_occurred_at = datetime.fromtimestamp(end, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        assert payload["occurredAt"] == expected_occurred_at, (
            f"occurredAt {payload['occurredAt']!r} != {expected_occurred_at!r}"
        )

        props = payload["properties"]
        assert set(props.keys()) == {
            "cueporch_live_ms",
            "cueporch_replay_ms",
            "cueporch_registered_at",
            "cueporch_event_title",
        }, f"unexpected property keys: {sorted(props.keys())}"
        assert props["cueporch_live_ms"] == "3300"
        assert props["cueporch_replay_ms"] == "1200"
        assert props["cueporch_event_title"] == "CRM rehearsal"
        datetime.fromisoformat(props["cueporch_registered_at"].replace("Z", "+00:00"))
        assert list(fixture.outbound_writes) == initial_writes
