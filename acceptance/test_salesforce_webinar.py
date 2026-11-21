from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
import event_salesforce_registration
from test_drive_import import QuietHandler, service_worker
import test_salesforce_roster as roster


class TestSalesforceWebinar(roster.TestSalesforceRoster):
    def exercise(self, client, headers, provider, app):
        start = datetime.now(timezone.utc) + timedelta(minutes=10)
        end = start + timedelta(minutes=50)
        response = client.post("/api/live-events", headers=headers, json={
            "title": "Salesforce outbound rehearsal",
            "slug": "salesforce-outbound-rehearsal",
            "visibility": "public",
            "type": "webinar",
            "latency": "standard",
            "save_replay": False,
            "schedule": {
                "timezone": "UTC",
                "repeat": "none",
                "start": start.strftime("%Y-%m-%dT%H:%M"),
                "end": end.strftime("%Y-%m-%dT%H:%M"),
            },
        })
        self.assertIn(response.status_code, (200, 201))
        event = response.get_json()
        opens = event["occurrences"][0]["id"]
        registration = f"/api/live-events/{event['id']}/registration"
        options = client.get(registration, headers=headers).get_json()
        options["settings"]["enabled"] = True
        options["settings"]["attendance_analytics"] = True
        self.assertEqual(client.put(registration, headers=headers, json=options).status_code, 200)

        url = f"/api/live-events/{event['id']}/{opens}/registration/salesforce/outbound"
        captured = []

        def publish(connection, revision, *, before_write, **payload):
            before_write()
            captured.append(payload)
            return "sent"

        adapter = event_salesforce_registration.salesforce_outbound
        with patch.object(adapter, "validate_installation") as describe, patch.object(
            adapter, "validate_engagement_installation"
        ), patch.object(
            adapter, "publish_registration", side_effect=publish
        ):
            response = client.put(url, headers=headers, json={"campaign": roster.TARGET})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertEqual(response.get_json()["company"], "Individual")
            self.assertEqual(response.get_json()["campaign"], roster.TARGET)
            describe.assert_called_once()
            self.verify_delivery(client, headers, app, opens, url, captured)

    def verify_delivery(self, client, headers, app, opens, url, captured):
        def receipt():
            response = client.get(url, headers=headers)
            self.assertEqual(response.status_code, 200)
            return response.get_json()

        def set_required(required):
            response = client.put("/api/policy/event_contact_sync", headers=headers,
                                  json={"required": required})
            self.assertEqual(response.status_code, 200)

        def wait_sent(count):
            deadline = monotonic() + 8
            while monotonic() < deadline:
                states = receipt()["states"]
                if states.get("sent") == count:
                    return
                sleep(0.1)
            self.fail(f"Expected {count} sent registrations, got {states!r}")

        service = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        watch = f"http://127.0.0.1:{service.server_port}/events/salesforce-outbound-rehearsal?occurrence={opens}"
        with service_worker(service), sync_playwright() as playwright, chromium(playwright) as browser:
            def register(email, consent, engagement=False):
                page = browser.new_page()
                try:
                    page.goto(watch)
                    page.locator("input[name=email]").fill(email)
                    page.locator("input[name=first_name]").fill("Ada")
                    page.locator("input[name=last_name]").fill("Example")
                    box = page.locator("input[name=salesforce_consent]")
                    expect(box).not_to_be_checked()
                    if consent:
                        box.check()
                    engagement_box = page.locator("input[name=salesforce_engagement_consent]")
                    expect(engagement_box).not_to_be_checked()
                    if engagement:
                        engagement_box.check()
                    page.locator("form button[type=submit]").first.click()
                    expect(box).to_have_count(0)
                    grant = page.locator("#eventLobby").get_attribute("data-viewer-grant")
                    self.assertTrue(grant)
                    return grant
                finally:
                    page.close()

            operator_page = browser.new_page()
            operator_page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {repr(headers['Authorization'].removeprefix('Bearer '))});")
            operator_page.goto(f'http://127.0.0.1:{service.server_port}/operator/live-events')
            operator_page.get_by_role('button', name='Open production for Salesforce outbound rehearsal').click()
            operator_page.get_by_text('Attendees for this occurrence', exact=True).click()
            panel = operator_page.locator('#eventSalesforceImport')
            status = panel.locator('#sfOutboundStatus')
            expect(status).to_contain_text('Connected.')
            panel.locator('#sfOutboundStop').click()
            expect(status).to_have_text('Off.')
            panel.locator('#sfCampaignLoad').click()
            panel.locator('#sfCampaignMore').click()
            panel.locator('#sfCampaign').select_option(label='Arts')
            panel.locator('#sfLeadCompany').fill('Rehearsal Company')
            panel.locator('#sfOutboundEngagement').check()
            panel.locator('#sfOutboundEnable').click()
            expect(status).to_contain_text('Campaign: Arts.')
            panel.locator('#sfOutboundRefresh').click()
            company = panel.locator('#sfLeadCompany')
            expect(company).to_have_value('Rehearsal Company')
            expect(status).to_contain_text('Campaign: Arts.')
            expect(panel.locator('#sfImportStatus')).to_have_text('Off.')
            expect(panel.locator('#sfOutboundComplete')).to_be_hidden()
            self.assertTrue(receipt()['engagement'])
            complete = client.post(url + '/complete', headers=headers, json={})
            self.assertEqual(complete.status_code, 409)
            self.assertEqual(receipt()['company'], 'Rehearsal Company')
            operator_page.close()
            register("declined@example.test", False)
            self.assertEqual(receipt()["states"], {})
            register("automatic@example.test", True)
            wait_sent(1)
            self.assertEqual(receipt()["engagement_states"], {})
            self.assertEqual(captured[0]["address"], "automatic@example.test")
            self.assertEqual(captured[0]["answers"]["first_name"], "Ada")
            self.assertEqual(datetime.fromisoformat(captured[0]["registered_at"]).utcoffset(), timedelta(0))
            set_required(True)
            response = client.put("/api/policy/event_engagement_sync", headers=headers, json={"required": True})
            self.assertEqual(response.status_code, 200)
            waiting_grant = register("waiting@example.test", True, engagement=True)
            self.assertEqual(receipt()["states"], {"sent": 1, "waiting": 1})
            self.assertEqual(len(captured), 1)
            set_required(False)
            wait_sent(2)
            self.assertEqual(receipt()["engagement_states"], {"waiting": 1})
            response = client.post(
                "/api/event-playback/withdraw-sharing",
                headers={"X-CuePorch-Event-Viewer": waiting_grant},
            )
            self.assertEqual(response.status_code, 204)
            self.assertEqual(receipt()["engagement_states"], {"canceled": 1})
            self.assertEqual(captured[1]["address"], "waiting@example.test")
            set_required(True)
            register("canceled@example.test", True)
            response = client.post("/api/integrations/salesforce/disconnect", headers=headers, json={})
            self.assertEqual(response.status_code, 200)
            set_required(False)
            self.assertFalse(receipt()["enabled"])
            self.assertEqual(len(captured), 2)
