import json
import event_contact_delivery
import secrets
import time
import sqlite3
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from flask import Flask
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from institution import create_app
from test_drive_import import QuietHandler, service_worker
from browser import chromium
from test_marketo_connection import _resp


class MarketoBrowser(unittest.TestCase):
    def test_marketo_registration(self):
        provider = MarketoProvider()
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps(dict(institution="station", schema=1, works=[]))
            )
            database = root / "workspace.sqlite"
            provider.database = database
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
                        "CUEPORCH_MARKETO_ORIGIN": "https://mock.mktorest.com",
                        "CUEPORCH_MARKETO_CLIENT_ID": "cid",
                        "CUEPORCH_MARKETO_CLIENT_SECRET": "sec",
                    },
                )
            )
            cleanup.enter_context(patch("event_peer.host_available", return_value=True))
            cleanup.enter_context(
                patch("marketo_crm.provider_request", provider.request)
            )
            original_start_marketo = event_contact_delivery.start_marketo
            cleanup.enter_context(
                patch.object(
                    event_contact_delivery,
                    "start_marketo",
                    lambda *args, **kwargs: original_start_marketo(
                        *args,
                        **{
                            **kwargs,
                            "clock": lambda: time.time() + provider.clock_offset,
                        },
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
                    if name.startswith("stop_") and callable(stop)
                )
                service.app = app

            def stop_app():
                while stops:
                    stops.pop()()

            cleanup.callback(stop_app)
            start_app()
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                self.exercise(browser, base, operator, provider)

    def create_event(self, page, base, auth):
        start = datetime.now(timezone.utc) + timedelta(minutes=6)
        end = start + timedelta(minutes=50)
        payload = {
            "title": "Marketo rehearsal",
            "slug": "marketo-rehearsal",
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
        r = page.request.post(base + "/api/live-events", headers=auth, data=payload)
        assert r.ok
        created = r.json()
        eid = created["id"]
        rr = page.request.get(
            f"{base}/api/live-events/{eid}/registration", headers=auth
        )
        assert rr.ok
        form = rr.json()
        form["settings"]["enabled"] = True
        form["settings"]["fields"] = [
            {
                "id": "field_genre",
                "type": "text",
                "label": "Favorite genre",
                "required": True,
            }
        ]
        ru = page.request.put(
            f"{base}/api/live-events/{eid}/registration", headers=auth, data=form
        )
        assert ru.ok
        return created

    def exercise(self, browser, base, operator, provider):
        page = browser.new_page()
        page.add_init_script(
            "sessionStorage.setItem('cueporchOperator', %s)" % json.dumps(operator)
        )
        auth = {"Authorization": "Bearer " + operator}
        created = self.create_event(page, base, auth)
        event_id, opens = created["id"], created["occurrences"][1]["id"]
        page.goto(base + "/operator/live-events")
        page.get_by_role("button", name="Open production for Marketo rehearsal").nth(
            1
        ).click()
        page.get_by_text("Attendees for this occurrence", exact=True).click()
        page.get_by_text("Load programs and fields").click()
        page.locator("#eventMarketoProgram").select_option(label="Arts screening")
        page.select_option(
            '#eventMarketoMapping select[data-form-field="field_genre"]',
            "favoriteGenre",
        )
        page.get_by_text("Save Marketo delivery").click()
        expect(page.locator("#eventMarketoStatus")).to_contain_text(
            "Marketo delivery enabled."
        )
        outbound_url = "%s/api/live-events/%s/%s/registration/marketo/outbound" % (
            base,
            event_id,
            opens,
        )
        receipt = page.evaluate(
            "u => fetch(u, {headers: %s}).then(r => r.json())" % json.dumps(auth),
            outbound_url,
        )
        assert (
            receipt["program"] == 52
            and receipt["mapping"]["field_genre"] == "favoriteGenre"
        )
        self.verify_delivery(page, base, auth, created, opens, outbound_url, provider)

    def register_viewer(self, browser, base, opens, email, consent):
        visitor = browser.new_page()
        try:
            visitor.goto(f"{base}/events/marketo-rehearsal?occurrence={opens}")
            visitor.fill("input[name=email]", email)
            visitor.fill("input[name=first_name]", "Casey")
            visitor.fill("input[name=last_name]", "Example")
            visitor.fill("input[name=field_genre]", "Documentary")
            box = visitor.locator("input[name=marketo_consent]")
            expect(box).not_to_be_checked()
            if consent:
                box.check()
            visitor.locator("form button[type=submit]").first.click()
            expect(box).to_have_count(0)
        finally:
            visitor.close()

    def verify_delivery(self, page, base, auth, created, opens, outbound_url, provider):
        import time

        policy_url = base + "/api/policy/event_contact_sync"

        def set_required(value):
            r = page.request.put(policy_url, headers=auth, data={"required": value})
            assert r.ok

        def get_receipt():
            r = page.request.get(outbound_url, headers=auth)
            assert r.ok
            return r.json()

        set_required(True)
        self.register_viewer(
            page.context.browser, base, opens, "declined@example.test", False
        )
        self.register_viewer(
            page.context.browser, base, opens, "opted-out@example.test", True
        )
        self.register_viewer(
            page.context.browser, base, opens, "approved@example.test", True
        )
        receipt = get_receipt()
        assert receipt["states"]["waiting"] == 2
        assert (
            provider.writes == []
            and len(provider.members) == 0
            and provider.reads == []
        )
        set_required(False)
        deadline = time.monotonic() + 10
        import contextlib
        import sqlite3

        retry_row = None
        with contextlib.closing(sqlite3.connect(provider.database)) as connection:
            while time.monotonic() < deadline:
                row = connection.execute(
                    "SELECT due FROM live_event_contact_delivery "
                    "WHERE provider='marketo' AND state='queued' AND attempts=1"
                ).fetchone()
                if row:
                    retry_row = row
                    break
                page.wait_for_timeout(100)
        assert retry_row is not None
        assert retry_row[0] >= int(time.time()) + 40
        assert provider.membership_attempts == 1
        receipt = get_receipt()
        assert receipt["states"].get("queued") == 1
        assert len(provider.writes) == 1
        assert provider.members == []
        provider.clock_offset += 43
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            receipt = get_receipt()
            if (
                receipt["states"].get("sent") == 1
                and receipt["states"].get("skipped") == 1
            ):
                break
            page.wait_for_timeout(100)
        assert receipt["states"]["sent"] == 1 and receipt["states"]["skipped"] == 1
        assert provider.writes == [
            dict(
                email="approved@example.test",
                firstName="Casey",
                lastName="Example",
                favoriteGenre="Documentary",
            )
        ]
        assert len(provider.members) == 1
        assert provider.membership_attempts == 2
        assert (
            "opted-out@example.test" in provider.reads
            and "declined@example.test" not in provider.reads
        )
        writes_before = list(provider.writes)
        set_required(True)
        self.register_viewer(
            page.context.browser, base, opens, "canceled@example.test", True
        )
        assert get_receipt()["states"]["waiting"] == 1
        stop = page.get_by_role("button", name="Stop Marketo delivery")
        stop.wait_for(state="visible")
        stop.click()
        expect(page.locator("#eventMarketoStatus")).to_have_text(
            "Marketo delivery is off."
        )
        set_required(False)
        receipt = get_receipt()
        assert receipt["enabled"] is False
        assert provider.writes == writes_before


class MarketoProvider:
    def __init__(self):
        from test_marketo_connection import TestMarketoConnection

        self.metadata = TestMarketoConnection().marketo_mock
        self.leads = {
            "opted-out@example.test": {
                "id": 9,
                "email": "opted-out@example.test",
                "unsubscribed": True,
            }
        }
        self.writes = []
        self.members = []
        self.clock_offset = 0
        self.membership_attempts = 0
        self.reads = []

    def request(self, method, url, **kwargs):
        origin = "https://mock.mktorest.com"
        assert url == origin or url.startswith(origin + "/")
        path = url.removeprefix(origin)
        if path in (
            "/identity/oauth/token",
            "/rest/asset/v1/programs.json",
            "/rest/v1/leads/describe.json",
        ):
            return self.metadata(method, url, **kwargs)
        assert kwargs.get("headers", {}).get("Authorization") == "Bearer mock-token"
        if method == "GET" and path == "/rest/asset/v1/program/52.json":
            return _resp(
                {
                    "success": True,
                    "result": [
                        {
                            "id": 52,
                            "name": "Arts screening",
                            "type": "Event",
                            "channel": "Screenings",
                        }
                    ],
                }
            )
        if method == "GET" and path == "/rest/asset/v1/channel/byName.json":
            assert "name" in kwargs.get("params", {})
            return _resp(
                {
                    "success": True,
                    "result": [
                        {
                            "name": "Screenings",
                            "progressionStatuses": [
                                {"name": "Registered", "hidden": False}
                            ],
                        }
                    ],
                }
            )
        if path == "/rest/v1/leads.json":
            return self.lead(method, kwargs)
        if method == "POST" and path == "/rest/v1/programs/52/members/status.json":
            return self.member(kwargs)
        raise AssertionError(f"Unknown call: {method} {url}")

    def lead(self, method, kwargs):
        if method == "GET":
            p = kwargs["params"]
            assert p["filterType"] == "email"
            email = p["filterValues"]
            self.reads.append(email)
            return _resp(
                {
                    "success": True,
                    "result": [self.leads[email]] if email in self.leads else [],
                }
            )
        assert method == "POST"
        j = kwargs["json"]
        assert j["action"] == "createOnly"
        assert j["lookupField"] == "email"
        assert len(j["input"]) == 1
        row = j["input"][0]
        assert set(row) == {"email", "firstName", "lastName", "favoriteGenre"}
        email = row["email"]
        assert email not in self.leads
        saved = dict(row, id=17)
        self.leads[email] = saved
        self.writes.append(row.copy())
        return _resp({"success": True, "result": [{"status": "created", "id": 17}]})

    def member(self, kwargs):
        self.membership_attempts += 1
        if self.membership_attempts == 1:
            return _resp(
                {"success": False, "errors": [{"code": "606"}]},
                headers={"Retry-After": "42"},
            )
        expected = {"statusName": "Registered", "input": [{"leadId": 17}]}
        assert kwargs["json"] == expected
        self.members.append({"statusName": "Registered", "input": [{"leadId": 17}]})
        return _resp(
            {"success": True, "result": [{"seq": 0, "leadId": 17, "status": "created"}]}
        )
