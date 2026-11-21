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
            import event_hubspot_sync

            original_start_sync = event_hubspot_sync.start

            def start_sync(*args, **kwargs):
                if kwargs.get("provider") == "marketo":
                    kwargs["clock"] = lambda: getattr(
                        provider, "sync_clock", time.time()
                    )
                return original_start_sync(*args, **kwargs)

            cleanup.enter_context(patch.object(event_hubspot_sync, "start", start_sync))
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

            def restart():
                stop_app()
                start_app()

            cleanup.callback(stop_app)
            start_app()
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                self.exercise(browser, base, operator, provider, restart)

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
        form["settings"]["attendance_analytics"] = True
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

    def exercise(self, browser, base, operator, provider, restart):
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
        self.verify_static_list_import(page, base, auth, event_id, opens, provider)

        self.verify_scheduled_import(
            page, base, auth, event_id, opens, provider, restart
        )

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

    def verify_scheduled_import(
        self, page, base, auth, event_id, opens, provider, restart
    ):
        import contextlib

        url = f"{base}/api/live-events/{event_id}/{opens}/registration/marketo/sync"
        hdrs = {**auth, "Origin": base, "X-CuePorch-Integration": "1"}
        midnight = int(
            datetime.fromtimestamp(opens, timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        retry_at = midnight + 15

        def get():
            r = page.request.get(url, headers=hdrs)
            assert r.ok, r.text()
            return r.json()

        def poll(pred):
            end = time.monotonic() + 10
            while time.monotonic() < end:
                j = get()
                if pred(j):
                    return j
                time.sleep(0.1)
            raise AssertionError(j)

        provider.scheduled_member, provider.healthy_import = True, False
        panel = page.locator("#eventMarketoImportPanel")
        panel.get_by_role("button", name="Enable automatic import", exact=True).click()
        expect(panel.locator("#eventMarketoImportStatus")).to_contain_text(
            "Automatic import scheduled."
        )
        b = get()
        assert b["list"] == 7 and b["next_at"] == midnight

        provider.sync_clock = midnight
        poll(lambda j: j["state"] == "retry" and j["next_at"] == retry_at)
        restart()
        saved = get()
        assert saved["state"] == "retry" and saved["next_at"] == retry_at

        provider.healthy_import, provider.sync_clock = True, retry_at
        r = poll(lambda j: j.get("last_at") == retry_at)
        assert r["last_result"]["added"] == 1 and r["last_result"]["existing"] == 1

        panel.get_by_role("button", name="Refresh import status", exact=True).click()
        expect(
            page.locator("#eventRosterRows").get_by_text(
                "scheduled@example.test", exact=True
            )
        ).to_be_visible()

        with contextlib.closing(sqlite3.connect(provider.database)) as c:
            rows = c.execute(
                "SELECT email, source FROM live_event_registrant WHERE event=? AND opens=?",
                (event_id, opens),
            ).fetchall()
        assert ("scheduled@example.test", "marketo-scheduled") in rows

        nxt = min(t for t in (opens - 3600, opens, opens + 900) if t > retry_at)
        assert r["next_at"] == nxt

        for tick in (opens - 3600, opens, opens + 900):
            if tick > retry_at:
                provider.sync_clock = tick
                current = poll(lambda status: status.get("last_at") == tick)
                assert (
                    current["last_result"]["added"] == 0
                    and current["last_result"]["existing"] == 2
                )
        assert current["next_at"] == opens + 1800

        provider.sync_clock = opens + 50 * 60
        poll(lambda j: j["state"] == "complete" and j["next_at"] is None)

        provider.sync_clock = time.time()
        panel.get_by_role("button", name="Enable automatic import", exact=True).click()
        expect(panel.locator("#eventMarketoImportStatus")).to_contain_text(
            "Automatic import scheduled."
        )
        panel.get_by_role("button", name="Stop automatic import", exact=True).click()
        expect(panel.locator("#eventMarketoImportStatus")).to_contain_text(
            "Automatic import is off."
        )
        assert get()["state"] == "off"

    def verify_static_list_import(self, page, base, auth, event_id, opens, provider):
        from contextlib import closing

        anon = page.request.get(base + "/api/integrations/marketo/static-lists")
        assert anon.status != 200, anon.text()
        authed = page.request.get(
            base + "/api/integrations/marketo/static-lists", headers=auth
        )
        assert authed.status == 200, authed.text()
        assert authed.json() == {
            "lists": [{"id": 8, "name": "Documentary"}],
            "next_offset": 50,
        }
        url = base + f"/api/live-events/{event_id}/{opens}/registration/marketo"
        headers = dict(auth, **{"Origin": base, "X-CuePorch-Integration": "1"})
        payload = {"list": 7, "mapping": {"field_genre": "favoriteGenre"}}
        bad = page.request.post(url, headers=headers, data=payload)
        assert bad.status == 502, bad.text()
        with closing(sqlite3.connect(provider.database)) as conn:
            rows = conn.execute(
                "SELECT opens,email,source,answers FROM live_event_registrant WHERE event=?",
                (event_id,),
            ).fetchall()
            assert rows == []
        provider.healthy_import = True
        panel = page.locator("#eventMarketoImportPanel")
        panel.get_by_role(
            "button", name="Load static lists and fields", exact=True
        ).click()
        panel.get_by_role("button", name="More lists", exact=True).click()
        panel.locator("#eventMarketoList").select_option(label="Screening")
        panel.locator(
            '#eventMarketoImportMapping select[data-form-field="field_genre"]'
        ).select_option("favoriteGenre")
        panel.get_by_role("button", name="Import selected list", exact=True).click()

        expect(panel.locator("#eventMarketoImportStatus")).to_contain_text(
            "Added 1. Already registered 0. Duplicates 1. Skipped 1."
        )
        expect(
            page.locator("#eventRosterRows").get_by_text(
                "import@example.test", exact=True
            )
        ).to_be_visible()

        second = page.request.post(url, headers=headers, data=payload)
        assert second.status == 200, second.text()
        assert second.json() == {
            "added": 0,
            "duplicates": 1,
            "skipped": 1,
            "existing": 1,
            "occurrence": opens,
        }
        with closing(sqlite3.connect(provider.database)) as conn:
            rows = conn.execute(
                "SELECT opens,email,source,answers FROM live_event_registrant WHERE event=?",
                (event_id,),
            ).fetchall()
        assert len(rows) == 1
        opens_val, email, source, answers = rows[0]
        assert opens_val == opens
        assert email == "import@example.test"
        assert source == "marketo"
        assert json.loads(answers)["field_genre"] == "Documentary"

    def register_viewer(
        self, browser, base, opens, email, consent, *, engagement=False
    ):
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
            if engagement:
                engagement_box = visitor.locator(
                    "input[name=marketo_engagement_consent]"
                )
                assert not engagement_box.is_checked()
                engagement_box.check()
            visitor.locator("form button[type=submit]").first.click()
            expect(box).to_have_count(0)
            return visitor.locator("#eventLobby").get_attribute("data-viewer-grant")
        finally:
            visitor.close()

    def record_activity(self, page, base, auth, event_id, opens, grant):
        engagement_url = f"{base}/api/live-events/{event_id}/{opens}/engagement"
        response = page.request.put(
            engagement_url,
            headers=auth,
            data={
                "feature": "qa",
                "enabled": True,
                "review": False,
                "anonymous": True,
            },
        )
        assert response.ok

        response = page.request.post(
            engagement_url,
            headers=auth,
            data={
                "feature": "poll",
                "type": "draft",
                "question": "Format?",
                "choices": ["Film", "Talk"],
            },
        )
        assert response.ok
        poll_id = response.json()["polls"][0]["id"]

        response = page.request.post(
            engagement_url,
            headers=auth,
            data={"feature": "poll", "type": "open", "id": poll_id},
        )
        assert response.ok

        audience_url = f"{base}/events/marketo-rehearsal/{opens}/engagement"
        response = page.request.get(
            audience_url,
            headers={"X-CuePorch-Event-Viewer": grant},
        )
        session = response.json()["session"]
        session_headers = {
            "X-CuePorch-Event-Viewer": grant,
            "X-CuePorch-Engagement-Session": session,
        }

        response = page.request.post(
            audience_url,
            headers=session_headers,
            data={
                "feature": "qa",
                "type": "question",
                "question": "Can I screen this?",
                "name": "Casey",
            },
        )
        assert response.ok

        response = page.request.post(
            audience_url,
            headers=session_headers,
            data={"feature": "poll", "id": poll_id, "choice": 0},
        )
        assert response.ok

        response = page.request.get(
            audience_url,
            headers={"X-CuePorch-Event-Viewer": grant},
        )
        fresh_session = response.json()["session"]
        fresh_session_headers = {
            "X-CuePorch-Event-Viewer": grant,
            "X-CuePorch-Engagement-Session": fresh_session,
        }

        response = page.request.post(
            audience_url,
            headers=fresh_session_headers,
            data={
                "feature": "qa",
                "type": "question",
                "anonymous": True,
                "question": "Private question",
            },
        )
        assert response.ok

    def complete_fixture(self, provider, event_id, opens, email):
        from contextlib import closing
        import event_marketo_engagement

        with closing(sqlite3.connect(provider.database)) as con, con:
            row = con.execute(
                "SELECT j.generation, j.sharing FROM live_event_marketo_engagement j "
                "JOIN live_event_registrant r ON r.id=j.registrant "
                "WHERE j.event=? AND j.opens=? AND j.state='queued' AND r.email=?",
                (event_id, opens, email),
            ).fetchone()
            assert row is not None
            generation, sharing = row
            now = time.time()
            con.execute(
                "INSERT INTO live_playback VALUES (?,?,?,?,?,?,?,?,?)",
                ("marketo-browser-live", event_id, opens, "live", 1, 1, 0, 30000, now),
            )
            con.execute(
                "INSERT INTO live_attendee_session VALUES ('marketo-browser-live', ?)",
                (sharing,),
            )
            assert event_marketo_engagement.complete(con, generation, now, 120)
            return generation

    def verify_engagement_retry(self, page, provider, generation, email, get_receipt):
        from contextlib import closing
        import sqlite3
        import time

        deadline = time.monotonic() + 12
        row = None
        with closing(sqlite3.connect(provider.database)) as conn:
            while time.monotonic() < deadline:
                row = conn.execute(
                    "SELECT fields_sent,attended_live,attempts,due "
                    "FROM live_event_marketo_engagement "
                    "WHERE generation=? AND state='queued' AND fields_sent=1",
                    (generation,),
                ).fetchone()
                if row and row[0] == 1 and row[1] == 1 and row[2] == 1 and row[3] > 0:
                    break
                page.wait_for_timeout(100)
            assert row, "expected a populated queued row"
            fields_sent, attended_live, attempts, due = row
            assert (fields_sent, attended_live, attempts) == (1, 1, 1)
            assert due > time.time() + provider.clock_offset
            lead_id = provider.leads[email]["id"]
            expected_engagement = {
                "leadId": lead_id,
                "cuePorchWatchPercentage": 25,
                "cuePorchQuestions": "Can I screen this?",
                "cuePorchPolls": "Format?: Film",
            }
            expected_attendance = {
                "statusName": "Attended Live",
                "input": [{"leadId": lead_id}],
            }
            assert provider.engagement_posts == [expected_engagement]
            assert provider.attendance_posts == [expected_attendance]
            conn.execute(
                "UPDATE live_playback SET seen = seen + 100000 "
                "WHERE id = 'marketo-browser-live'"
            )
            conn.commit()
        provider.clock_offset += 42
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if get_receipt().get("engagement_states") == {"sent": 1, "skipped": 1}:
                break
            page.wait_for_timeout(100)
        assert get_receipt()["engagement_states"] == {"sent": 1, "skipped": 1}
        assert provider.engagement_posts == [expected_engagement]
        assert provider.attendance_posts == [expected_attendance, expected_attendance]

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

        import uuid

        page.locator("#eventMarketoEngagement").check()
        page.get_by_role("button", name="Save Marketo delivery").click()
        expect(page.locator("#eventMarketoStatus")).to_contain_text(
            "Marketo delivery enabled."
        )
        assert get_receipt()["engagement"] is True
        self.register_viewer(
            page.context.browser,
            base,
            opens,
            f"marketo-no-consent-{uuid.uuid4().hex}@example.com",
            False,
            engagement=True,
        )
        assert get_receipt()["engagement_states"] == {}
        engagement_policy_url = base + "/api/policy/event_engagement_sync"
        policy_response = page.request.put(
            engagement_policy_url,
            headers=auth,
            data={"required": True},
        )
        assert policy_response.ok
        email = f"marketo-consent-{uuid.uuid4().hex}@example.com"
        grant = self.register_viewer(
            page.context.browser,
            base,
            opens,
            email,
            True,
            engagement=True,
        )
        assert get_receipt()["engagement_states"] == {"waiting": 1}
        self.record_activity(page, base, auth, created["id"], opens, grant)
        secondary = "later-optout@example.test"
        self.register_viewer(
            page.context.browser, base, opens, secondary, True, engagement=True
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if get_receipt()["states"].get("sent") == 2:
                break
            page.wait_for_timeout(100)
        assert get_receipt()["states"].get("sent") == 2
        provider.leads[secondary]["unsubscribed"] = True
        generation = self.complete_fixture(provider, created["id"], opens, email)
        assert provider.engagement_posts == []
        policy_response = page.request.put(
            engagement_policy_url,
            headers=auth,
            data={"required": False},
        )
        assert policy_response.ok
        self.verify_engagement_retry(page, provider, generation, email, get_receipt)
        stop.click()
        expect(page.locator("#eventMarketoStatus")).to_contain_text(
            "Marketo delivery is off."
        )


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
        self.field_schema = []
        self.engagement_posts = []
        self.attendance_posts = []
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
                                {"name": "Registered", "hidden": False},
                                {"name": "Attended Live", "hidden": False},
                            ],
                        }
                    ],
                }
            )
        if method == "GET" and path == "/rest/asset/v1/staticLists.json":
            params = kwargs["params"]
            offset = params.get("offset", 0)
            assert offset in (0, 50)
            if offset == 0:
                return _resp(
                    dict(
                        success=True,
                        result=[dict(id=8, name="Documentary")] + [{}] * 49,
                    )
                )
            return _resp(dict(success=True, result=[dict(id=7, name="Screening")]))
        if method == "GET" and path == "/rest/asset/v1/staticList/7.json":
            return _resp(dict(success=True, result=[dict(id=7, name="Screening")]))
        if method == "GET" and path == "/rest/v1/lists/7/leads.json":
            params = kwargs.get("params", {})
            tok = params.get("nextPageToken")
            if not tok:
                return _resp(
                    dict(
                        success=True,
                        result=[
                            dict(
                                id=10,
                                email="import@example.test",
                                firstName="Alex",
                                favoriteGenre="Documentary",
                            )
                        ],
                        nextPageToken="page2",
                    )
                )
            assert params.get("batchSize") == 300
            assert "favoriteGenre" in (params.get("fields") or "")
            assert tok == "page2"
            rows = [
                dict(
                    id=11,
                    email="IMPORT@example.test",
                    firstName="Alex",
                    favoriteGenre="Documentary",
                ),
                dict(id=12, email="skipped@example.test", firstName="Sam"),
            ]
            if getattr(self, "scheduled_member", False):
                rows.append(
                    {
                        "id": 13,
                        "email": "scheduled@example.test",
                        "firstName": "Riley",
                        "favoriteGenre": "Experimental",
                    }
                )

            nxt = None if getattr(self, "healthy_import", False) else "page2"
            return _resp(dict(success=True, result=rows, nextPageToken=nxt))

        if path == "/rest/v1/leads.json":
            return self.lead(method, kwargs)
        if path == "/rest/v1/programs/members/schema/fields.json":
            return self.engagement(method, path, kwargs)
        if method == "POST" and path == "/rest/v1/programs/52/members.json":
            return self.engagement(method, path, kwargs)
        if (
            method == "POST"
            and path == "/rest/v1/programs/52/members/status.json"
            and kwargs.get("json", {}).get("statusName") == "Attended Live"
        ):
            return self.engagement(method, path, kwargs)
        if method == "POST" and path == "/rest/v1/programs/52/members/status.json":
            return self.member(kwargs)
        raise AssertionError(f"Unknown call: {method} {url}")

    def engagement(self, method, path, kwargs):
        if method == "GET" and path == "/rest/v1/programs/members/schema/fields.json":
            return _resp({"success": True, "result": self.field_schema})
        if method == "POST" and path == "/rest/v1/programs/members/schema/fields.json":
            input_rows = kwargs["json"]["input"]
            schema = []
            statuses = []
            for row in input_rows:
                schema.append(
                    {
                        "name": row["name"],
                        "displayName": row["displayName"],
                        "dataType": row["dataType"],
                        "isHidden": False,
                        "isCustom": True,
                        "length": 255,
                    }
                )
                statuses.append({"status": "created"})
            self.field_schema = schema
            return _resp({"success": True, "result": statuses})
        if method == "POST" and path == "/rest/v1/programs/52/members.json":
            data = kwargs["json"]["input"]
            rows = data if isinstance(data, list) else [data]
            results = []
            for row in rows:
                self.engagement_posts.append(row)
                results.append(
                    {
                        "status": "updated",
                        "seq": 0,
                        "leadId": row["leadId"],
                    }
                )
            return _resp({"success": True, "result": results})
        if method == "POST" and path == "/rest/v1/programs/52/members/status.json":
            body = kwargs["json"]
            assert body["statusName"] == "Attended Live"
            self.attendance_posts.append(body)
            attempt = getattr(self, "_status_attempt", 0)
            if attempt == 0:
                setattr(self, "_status_attempt", 1)
                return _resp(
                    {"success": False, "errors": [{"code": "606"}]},
                    headers={"Retry-After": "41"},
                )
            setattr(self, "_status_attempt", 0)
            lead_id = body["input"][0]["leadId"]
            return _resp(
                {
                    "success": True,
                    "result": [{"status": "updated", "seq": 0, "leadId": lead_id}],
                }
            )
        raise AssertionError("unexpected endpoint: %s %s" % (method, path))

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
        lead_id = 17 + len(self.writes)
        saved = dict(row, id=lead_id)
        self.leads[email] = saved
        self.writes.append(row.copy())
        return _resp(
            {"success": True, "result": [{"status": "created", "id": lead_id}]}
        )

    def member(self, kwargs):
        self.membership_attempts += 1
        if self.membership_attempts == 1:
            return _resp(
                {"success": False, "errors": [{"code": "606"}]},
                headers={"Retry-After": "42"},
            )
        lead_id = kwargs["json"]["input"][0]["leadId"]
        assert lead_id in {lead["id"] for lead in self.leads.values()}
        expected = {"statusName": "Registered", "input": [{"leadId": lead_id}]}
        assert kwargs["json"] == expected
        self.members.append(expected)
        return _resp(
            {
                "success": True,
                "result": [{"seq": 0, "leadId": lead_id, "status": "created"}],
            }
        )
