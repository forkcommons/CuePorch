from contextlib import closing
import sqlite3
import time
from unittest.mock import patch

from browser import chromium
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

import library_list_registration
import library_salesforce_video_engagement as reports
import test_constant_contact_forms as cc
import test_salesforce_roster as roster
from test_drive_import import QuietHandler, service_worker
from test_salesforce_outbound import EMAIL
from test_salesforce_webinar_report import WebinarProvider
import salesforce_outbound


class TestSalesforceVideoReport(roster.TestSalesforceRoster):
    def test_roster(self):
        provider = WebinarProvider()

        original_run_one = reports.run_one
        original_start = library_list_registration.start

        def start(app, tx, database, connection, validate, **kwargs):
            provider.database = database
            return original_start(app, tx, database, connection, validate, **kwargs)

        def run_one(*args, **kwargs):
            now = provider.now
            kwargs["clock"] = lambda: now
            result = original_run_one(*args, **kwargs)
            provider.report_tick_at = now
            return result

        with patch.object(roster, "RosterProvider", lambda *args, **kwargs: provider), \
                patch.object(reports, "run_one", run_one), \
                patch.object(library_list_registration, "start", start):
            super().test_roster()

    def exercise(self, client, headers, provider, app):
        self.client = client
        self.auth = headers
        self.database = provider.database
        embed_path, _ = cc.ConstantContactFormsTest.enable_video(
            self, configure_delivery=False
        )
        work = embed_path.rstrip("/").rsplit("/", 1)[-1]
        server = make_server(
            "127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler
        )
        base = f"http://127.0.0.1:{server.server_port}"
        url = f"/api/library/work/{work}/registration/salesforce/outbound"
        with (
            service_worker(server),
            sync_playwright() as playwright,
            chromium(playwright) as browser,
        ):
            self.operator_enable(browser, base, work)
            self.verify_reporting(
                browser, base, embed_path, work, url, provider
            )

    def operator_enable(self, browser, base, work):
        video_path = f"/operator/videos/{work}/interactivity"
        import json
        staff = browser.new_page()
        staff.add_init_script(
            "sessionStorage.setItem('cueporchOperator', %s)" % json.dumps(self.auth["Authorization"].removeprefix("Bearer "))
        )
        staff.goto(f"{base}{video_path}")
        staff.click("#registrationSalesforceLoad")
        staff.click("#registrationSalesforceMore")
        staff.select_option("#registrationSalesforceList", roster.TARGET)
        staff.fill("#registrationSalesforceCompany", "Rehearsal Company")
        checkbox = staff.locator("#registrationSalesforceEngagement")
        expect(checkbox).to_be_enabled()
        expect(checkbox).not_to_be_checked()
        checkbox.check()
        staff.click("#registrationSalesforceSave")
        expect(staff.locator("#registrationSalesforceStatus")).to_contain_text(
            "Salesforce delivery is enabled."
        )
        staff.reload()
        expect(checkbox).to_be_checked()
        expect(staff.locator("#registrationSalesforceList")).to_have_value(
            roster.TARGET
        )
        expect(staff.locator("#registrationSalesforceCompany")).to_have_value(
            "Rehearsal Company"
        )
        staff.close()

    def play(self, browser, base, embed_path, *, tracking, registration=True):
        page = browser.new_page()
        page.goto(base + "/watch")
        page.set_content(f'<iframe src="{base}{embed_path}" width="640" height="360"></iframe>')
        target = page.frame_locator("iframe")
        target.locator("input[name=email]").fill(EMAIL)
        target.locator("input[name=first_name]").fill("Ada")
        target.locator("input[name=last_name]").fill("Viewer")
        consent = target.locator("input[name=salesforce_form_consent]")
        engagement = target.locator("input[name=salesforce_video_engagement_consent]")
        assert not consent.is_checked()
        assert not engagement.is_checked()
        if registration:
            consent.check()
        if tracking:
            engagement.check()
        target.get_by_role("button", name="Register and watch").click()
        video = target.locator("video")
        expect(video).to_be_visible()

        def completed_view(response):
            if (
                response.request.method != "POST"
                or "/viewer-analytics/" not in response.url
                or response.status != 204
            ):
                return False
            data = response.request.post_data_json
            return (
                data.get("played") is True
                and data.get("completed") is True
                and data.get("watched", 0) > 0
            )

        with page.expect_response(completed_view):
            video.evaluate("(v) => { v.muted = true; return v.play(); }")
            expect(video).to_have_js_property("ended", True)

        frames = video.evaluate(
            "(v) => v.getVideoPlaybackQuality().totalVideoFrames"
        )
        assert frames > 0
        return page, target

    def verify_reporting(self, browser, base, embed_path, work, url, provider):
        def receipt():
            return self.client.get(url, headers=self.auth).get_json()

        page, target = self.play(browser, base, embed_path, tracking=True, registration=False)
        assert receipt()["states"] == {}
        assert receipt()["engagement_states"] == {}
        expect(target.get_by_role("button", name="Stop sharing video activity with Salesforce")).to_have_count(0)
        page.close()

        response = self.client.put(
            "/api/policy/event_engagement_sync",
            json={"required": True},
            headers=self.auth,
        )
        assert response.status_code == 200

        page, target = self.play(browser, base, embed_path, tracking=True)
        assert receipt()["engagement_states"] == {"waiting": 1}
        self.wait_registration(receipt)

        next_due, expected_percent = self.playback_totals(work)
        provider.now = next_due + 1
        self.wait_tick(provider, provider.now)

        assert provider.outbound.writes.count("PATCH") == 0

        response = self.client.put(
            "/api/policy/event_engagement_sync",
            json={"required": False},
            headers=self.auth,
        )
        assert response.status_code == 200

        self.check_provider(provider, expected_percent)

        target.get_by_role("button", name="Stop sharing video activity with Salesforce").click()
        expect(target.locator("[data-salesforce-withdraw-status]")).to_have_text(
            "Salesforce video reporting stopped."
        )
        assert receipt()["engagement_states"] == {"canceled": 1}

        provider.now = next_due + reports.DAY + 1
        self.wait_tick(provider, provider.now)
        assert provider.outbound.writes.count("PATCH") == 1

        page.close()

    def wait_tick(self, provider, now):
        import time

        deadline = time.monotonic() + 8
        while getattr(provider, "report_tick_at", 0) < now:
            if time.monotonic() >= deadline:
                raise AssertionError("report tick timed out")
            time.sleep(0.1)

    def wait_registration(self, receipt):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if receipt()["states"].get("sent") == 1:
                return
            time.sleep(.1)
        self.fail(f"registration not sent within 8s: {receipt()!r}")

    def playback_totals(self, work):
        import json
        with closing(sqlite3.connect(self.database)) as con:
            rows = con.execute(
                "SELECT r.due, s.intervals FROM library_salesforce_video_report r "
                "JOIN library_salesforce_video_report_sample l ON l.report = r.id "
                "JOIN video_analytics_session s ON s.id = l.session "
                "WHERE r.work = ? AND r.state = 'queued' AND s.played = 1",
                (work,),
            ).fetchall()
        assert len(rows) == 1, rows
        due, intervals = rows[0]
        total_ms = sum(stop - start for start, stop in json.loads(intervals))
        assert total_ms > 0, intervals
        expected_percent = min(100, round(total_ms / 10))
        assert due > time.time() + 3500, due
        return due, expected_percent

    def check_provider(self, provider, expected_percent):
        writes = provider.outbound.writes
        deadline = time.monotonic() + 8
        while "PATCH" not in writes:
            self.assertLess(time.monotonic(), deadline, writes)
            time.sleep(0.1)
        self.assertEqual(writes.count("PATCH"), 1, writes)
        (registrant,) = (
            value
            for (name, _), value in provider.outbound.objects.items()
            if name == salesforce_outbound.REGISTRANT
        )
        self.assertEqual(registrant["Video_Views__c"], 1)
        self.assertEqual(registrant["Watch_Percent__c"], expected_percent)
        self.assertEqual(registrant["Status__c"], "Registered")
