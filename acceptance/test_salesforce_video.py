from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import test_constant_contact_forms as cc
import test_salesforce_roster as roster
from library_list_registration import salesforce_outbound

class TestSalesforceVideo(roster.TestSalesforceRoster):
    def exercise(self, client, headers, provider, app):
        self.client = client
        self.auth = headers
        with TemporaryDirectory() as tmp:
            self.database = Path(tmp) / 'fixture.sqlite'
            embed_path, _other_outbound = cc.ConstantContactFormsTest.enable_video(
                self, configure_delivery=False
            )
            work = embed_path.rsplit("/", 1)[-1]
            url = f'/api/library/work/{work}/registration/salesforce/outbound'
            captured = []

            def publish(connection, revision, *, before_write, **payload):
                before_write()
                captured.append(payload)
                return 'sent'

            with patch.object(salesforce_outbound, 'validate_installation') as describe, \
                 patch.object(salesforce_outbound, 'publish_registration', side_effect=publish):
                self.verify_browser(app, embed_path, work, url, captured)
                describe.assert_called()


    def verify_browser(self, app, embed_path, work, url, captured):
        import json
        from browser import chromium
        from playwright.sync_api import expect, sync_playwright
        from test_drive_import import QuietHandler, service_worker
        from werkzeug.serving import make_server

        video_path = f"/operator/videos/{work}/interactivity"
        server = make_server(
            "127.0.0.1", 0, app,
            request_handler=QuietHandler, threaded=True,
        )
        base = f"http://127.0.0.1:{server.server_port}"

        with service_worker(server), sync_playwright() as pw, chromium(pw) as browser:
            staff = browser.new_page()
            staff.add_init_script(
                "sessionStorage.setItem('cueporchOperator', %s)" % json.dumps(self.auth["Authorization"].removeprefix("Bearer "))
            )
            staff.goto(f"{base}{video_path}")
            staff.click("#registrationSalesforceLoad")
            staff.click("#registrationSalesforceMore")
            staff.select_option("#registrationSalesforceList", roster.TARGET)
            staff.fill("#registrationSalesforceCompany", "Rehearsal Company")
            staff.click("#registrationSalesforceSave")
            expect(staff.locator("#registrationSalesforceStatus")).to_contain_text(
                "Salesforce delivery is enabled."
            )
            staff.reload()
            expect(staff.locator("#registrationSalesforceList")).to_have_value(
                roster.TARGET
            )
            expect(staff.locator("#registrationSalesforceCompany")).to_have_value(
                "Rehearsal Company"
            )
            self.verify_viewer_delivery(browser, base, embed_path, url, captured)
            staff.click("#registrationSalesforceStop")
            expect(staff.locator("#registrationSalesforceStatus")).to_contain_text(
                "Salesforce delivery is off."
            )


    def verify_viewer_delivery(self, browser, base, embed_path, url, captured):
        import time
        play = cc.ConstantContactFormsTest.play_in_browser

        def receipt():
            return self.client.get(url, headers=self.auth).get_json()['states']

        def wait_sent(count):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if receipt().get('sent') == count:
                    return
                time.sleep(0.1)
            self.fail('sent count %d not observed' % count)

        play(self, browser, base, embed_path, 'declined@example.test', False,
             embed=True, consent_field='salesforce_form_consent')
        self.assertEqual(receipt(), {})
        play(self, browser, base, embed_path, 'automatic@example.test', True,
             embed=True, consent_field='salesforce_form_consent')
        wait_sent(1)
        payload = captured[0]
        self.assertEqual(payload['address'], 'automatic@example.test')
        self.assertEqual(payload['kind'], 'video')
        self.assertEqual(payload['company'], 'Rehearsal Company')
        self.assertEqual(payload['campaign'], roster.TARGET)
        self.assertTrue(payload['title'])
        resp = self.client.put('/api/policy/event_contact_sync', headers=self.auth,
                               json={'required': True})
        self.assertEqual(resp.status_code, 200)
        play(self, browser, base, embed_path, 'waiting@example.test', True,
             embed=True, consent_field='salesforce_form_consent')
        self.assertEqual(receipt(), {'sent': 1, 'waiting': 1})
        self.assertEqual(len(captured), 1)
        resp = self.client.put('/api/policy/event_contact_sync', headers=self.auth,
                               json={'required': False})
        self.assertEqual(resp.status_code, 200)
        wait_sent(2)
        self.assertEqual(captured[1]['address'], 'waiting@example.test')
