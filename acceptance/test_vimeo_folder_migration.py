"""Verify the Vimeo folder transfer through the operator and Library screens."""

import csv
import json
import secrets
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from cueporch import work_id
from demo_source import build_server
from institution import create_app
from test_drive_import import QuietHandler, service_worker
from vimeo_transfer import (SourceFailure, event_schedule_draft, folder_inventory,
                            live_event_archive_inventory, resolve_archive_recordings)


class VimeoFolderInventory(unittest.TestCase):
    def test_event_options_are_strict(self):
        event = {
            "uri": "/live_events/37", "recordings": [],
            "schedule": {"start_time": "2030-01-07T10:00:00Z",
                         "end_time": "2030-01-07T10:30:00Z", "time_zone": "UTC"},
            "chat_enabled": "false", "dvr": True, "auto_cc_enabled": True,
            "auto_cc_language": {}, "has_forced_lead_capture_form": "true",
        }

        class Source:
            def pages(self, path, *, fields):
                return [event]

        inventory, _ = live_event_archive_inventory("station", Source(), "/users/10", set())
        item, = inventory
        self.assertNotIn("chat_enabled", item)
        self.assertNotIn("captions", item)
        self.assertIs(True, item["dvr_enabled"])
        self.assertEqual(["Vimeo automatic captions use an unsupported or missing language. "
                          "Set the caption language in Live Events."], item["issues"])

        event.update(chat_enabled=False, auto_cc_enabled=False)
        inventory, _ = live_event_archive_inventory("station", Source(), "/users/10", set())
        self.assertEqual(dict(enabled=False), inventory[0]["captions"])
        self.assertIs(False, inventory[0]["chat_enabled"])

    def test_archive_only_owner_check(self):
        from io import BytesIO

        account = "/users/10"
        opened = []

        class SourceDouble:
            base = "https://api.vimeo.com"

            def open(self, url, authenticated):
                opened.append((url, authenticated))
                vid = url.rsplit("/", 1)[-1]
                if vid == "999":
                    raise SourceFailure(403)
                owner = "/users/11" if vid == "997" else account
                uri = "/videos/995" if vid == "996" else "/videos/" + vid
                return BytesIO(json.dumps(dict(uri=uri, user=dict(uri=owner))).encode())

        recordings = [dict(source_id=vid, work=None) for vid in ("998", "997", "996", "999", "71")]
        repeated = [dict(source_id=vid, work=None) for vid in ("998", "999")]
        events = [dict(recordings=recordings), dict(recordings=repeated)]
        videos, ids = [], {"71"}
        resolve_archive_recordings("station", SourceDouble(), account, events, videos, ids)
        self.assertEqual([dict(uri="/videos/998", user=dict(uri=account))], videos)
        self.assertEqual({"71", "998"}, ids)
        self.assertEqual(work_id("station", "vimeo", "998"), recordings[0]["work"])
        self.assertEqual(recordings[0], repeated[0])
        self.assertEqual(recordings[3], repeated[1])
        for rejected in recordings[1:4]:
            self.assertIsNone(rejected["work"])
            self.assertIn("issue", rejected)
        self.assertEqual([(f"https://api.vimeo.com/videos/{vid}", True)
                          for vid in ("998", "997", "996", "999")], opened)

    def test_event_schedule_draft(self):
        source = dict(start_time="2030-01-07T17:00:00Z", end_time="2030-01-07T18:00:00Z", time_zone="America/Denver")
        result = event_schedule_draft(source)
        self.assertEqual(("2030-01-07T10:00", "2030-01-07T11:00", "none"),
                         (result["start"], result["end"], result["repeat"]))
        for rrule in ("FREQ=YEARLY", "FREQ=WEEKLY;BYDAY=TU"):
            with self.assertRaises(ValueError):
                event_schedule_draft(dict(source, rrule=rrule))
        with self.assertRaises(ValueError):
            event_schedule_draft(dict(source, start_time="2030-11-03T01:30:00-07:00",
                                      end_time="2030-11-03T03:00:00-07:00"))

        transition = dict(start_time="2030-03-09T10:00:00-07:00", end_time="2030-03-09T11:00:00-07:00",
                          time_zone="America/Denver")
        for cutoff, last_date in (("20300311T155959Z", "2030-03-10"), ("20300311T160000Z", "2030-03-11")):
            result = event_schedule_draft(dict(transition, rrule=f"FREQ=DAILY;UNTIL={cutoff}"))
            self.assertEqual(last_date, result["until"])
        for malformed in ("FREQ=DAILY;COUNT=3;UNTIL=20300311T160000Z", "FREQ=DAILY;UNTIL=20300311T160000"):
            with self.assertRaises(ValueError):
                event_schedule_draft(dict(transition, rrule=malformed))

    def test_denied_folder_list_uses_complete_video_ancestry(self):
        class Denied:
            def pages(self, path):
                if path != "/users/10/projects":
                    raise AssertionError(path)
                raise SourceFailure(403)

        project = {
            "name": "Projections",
            "uri": "/users/10/projects/103",
            "metadata": {"connections": {
                "parent_folder": {"uri": "/users/10/projects/103/parent"},
                "ancestor_path": [
                    {"name": "Screenings", "uri": "/users/10/projects/102"},
                    {"name": "Archive", "uri": "/users/10/projects/100"},
                ],
            }},
        }
        videos = [
            {"uri": "/videos/71", "parent_folder": project},
            {"uri": "/videos/72", "parent_folder": {"name": "Loose", "uri": "/me/projects/999"}},
            {"uri": "/videos/73", "parent_folder": {"name": "Other", "uri": "/users/99/projects/5"}},
            {"uri": "/videos/74", "parent_folder": None},
        ]
        folders, by_video, issue = folder_inventory("station", Denied(), "/users/10", {}, videos)
        self.assertEqual(["Archive", "Archive / Screenings", "Archive / Screenings / Projections"],
                         [row["path"] for row in folders])
        self.assertEqual({"/videos/71": folders[-1], "/videos/72": None, "/videos/73": None}, by_video)
        self.assertIn("HTTP 403", issue)
        self.assertIn("could not be resolved", issue)


class VimeoFolderMigration(unittest.TestCase):
    def setUp(self):
        temp = Path(self.enterContext(TemporaryDirectory()))
        home = temp / "collection"
        (home / "objects").mkdir(parents=True)
        (home / "manifest.json").write_text(
            json.dumps(dict(institution="station", schema=1, works=[])), encoding="utf-8",
        )
        source = self.enterContext(build_server(temp / "vimeo"))
        self.source = source
        source.folder_fixture = True
        self.enterContext(service_worker(source))
        self.operator = secrets.token_urlsafe(32)
        self.auth = {"Authorization": "Bearer " + self.operator}
        self.enterContext(patch.dict("os.environ", {"CUEPORCH_PUBLIC_URL": "", "CUEPORCH_PEERTUBE_ORIGIN": ""}))
        with patch("institution.library_worker.start", return_value=lambda: None):
            app = create_app(home, temp / "workspace.sqlite", self.operator, "station",
                             simulation=f"http://127.0.0.1:{source.server_port}")
        for name, stop in app.extensions.items():
            if name.startswith("stop_") and callable(stop):
                self.addCleanup(stop)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        self.enterContext(service_worker(server))
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.client = app.test_client()

    def test_team_series_exposes_private_child_to_invited_viewer(self):
        team_headers = {'X-CuePorch-Team': '1'}
        start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        schedule = dict(timezone="UTC", repeat="none",
                        start=start.strftime("%Y-%m-%dT%H:%M"), end=end.strftime("%Y-%m-%dT%H:%M"))
        response = self.client.post("/api/live-events", headers=self.auth, json=dict(
            title="Private child", slug="private-child", visibility="private",
            type="live", latency="standard", save_replay=False, schedule=schedule))
        self.assertEqual(201, response.status_code, response.get_data(as_text=True))
        child = response.get_json()["id"]

        response = self.client.post("/api/event-series", headers=self.auth, json={})
        self.assertEqual(201, response.status_code, response.get_data(as_text=True))
        created = response.get_json()

        response = self.client.put(
            "/api/event-series/" + str(created["id"]), headers=self.auth,
            json=dict(revision=created["revision"], slug="team-series",
                      settings=dict(title="Team series", visibility="team", events=[child])))
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))

        response = self.client.post(
            "/api/team/invitations", headers={**self.auth, **team_headers},
            json={"email": "viewer@example.com", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        invitation = response.get_json()["path"].split("#", 1)[1]

        series_url = self.base + "/event-series/team-series"
        with sync_playwright() as playwright, chromium(playwright) as browser:
            anon = browser.new_context()
            self.assertEqual(anon.request.get(series_url).status, 404)

            team = browser.new_context()
            joined = team.request.post(self.base + "/api/team/join", headers=team_headers, data={
                "invitation": invitation, "name": "Viewer", "password": "viewer-password",
            })
            self.assertEqual(joined.status, 201, joined.text())

            series_page = team.new_page()
            self.assertEqual(series_page.goto(series_url).status, 200)
            href = series_page.locator('a:has-text("Private child")').first.get_attribute("href")
            self.assertTrue(href)
            child_url = href if href.startswith("http") else self.base + href
            self.assertEqual(anon.request.get(child_url).status, 404)

            child_page = team.new_page()
            self.assertEqual(child_page.goto(child_url).status, 200)

            logout = team.request.delete(self.base + "/api/team/session", headers=team_headers, data={})
            self.assertEqual(logout.status, 204)
            self.assertEqual(team.request.get(series_url).status, 404)
            self.assertEqual(team.request.get(child_url).status, 404)

    def test_event_series_password_gate(self):
        start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        schedule = dict(timezone="UTC", repeat="none",
                        start=start.strftime("%Y-%m-%dT%H:%M"), end=end.strftime("%Y-%m-%dT%H:%M"))

        response = self.client.post("/api/live-events", headers=self.auth, json=dict(
            title="Locked program", slug="locked-program", visibility="password",
            type="live", latency="standard", save_replay=False, schedule=schedule,
            view_password="inner-fixture-pass"))
        self.assertEqual(201, response.status_code, response.get_data(as_text=True))
        event_id = response.get_json()["id"]

        response = self.client.post("/api/event-series", headers=self.auth, json={})
        self.assertEqual(201, response.status_code, response.get_data(as_text=True))
        draft = response.get_json()
        draft_id = draft["id"]

        series_payload = dict(
            revision=draft["revision"],
            slug="outer-password-series",
            settings=dict(
                title="Locked series",
                visibility="password",
                events=[event_id],
                localization=dict(
                    base="en",
                    translations={"fr": dict(published=True, title="Série protégée")},
                ),
            ),
            view_password="outer-series-pass",
        )
        response = self.client.put(f"/api/event-series/{draft_id}", headers=self.auth, json=series_payload)
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        published = response.get_json()
        self.assertTrue(published.get("has_password"))
        raw = response.get_data(as_text=True)
        self.assertNotIn("outer-series-pass", raw)
        self.assertNotIn("inner-fixture-pass", raw)
        for marker in ("$argon", "$2a$", "$2b$", "$2y$", "scrypt:", "pbkdf2:"):
            self.assertNotIn(marker, raw, marker)

        with sync_playwright() as playwright, chromium(playwright) as browser:
            guest = browser.new_page()
            guest.goto(f"{self.base}/event-series/outer-password-series")
            guest.wait_for_selector('input[name="password"]')
            initial_html = guest.content()
            self.assertIn("Event password", initial_html)
            self.assertIn("Open event series", initial_html)
            self.assertNotIn("Locked program", initial_html)
            self.assertNotIn("Locked series", initial_html)

            guest.fill('input[name="password"]', "wrong-password")
            with guest.expect_navigation() as wrong_nav:
                guest.get_by_role("button", name="Open event series").click()
            self.assertEqual(403, wrong_nav.value.status)
            wrong_html = guest.content()
            self.assertNotIn("Locked program", wrong_html)
            self.assertNotIn("Locked series", wrong_html)

            guest.goto(f"{self.base}/event-series/outer-password-series")
            guest.wait_for_selector('input[name="password"]')
            guest.fill('input[name="password"]', "outer-series-pass")
            with guest.expect_navigation() as ok_nav:
                guest.get_by_role("button", name="Open event series").click()
            self.assertEqual(200, ok_nav.value.status)
            self.assertIn("Locked series", guest.content())

            with guest.expect_navigation():
                guest.get_by_role("link", name="Français", exact=True).click()
            guest.wait_for_selector("h1")
            self.assertIn("Série protégée", guest.locator("h1").inner_text())

            with guest.expect_navigation():
                guest.get_by_role("link", name="English", exact=True).click()
            guest.wait_for_selector("h1")
            self.assertIn("Locked series", guest.locator("h1").inner_text())

            with guest.expect_navigation():
                guest.get_by_role("link", name="Locked program").click()
            guest.wait_for_selector('input[name="password"]')
            child_html = guest.content()
            self.assertIn("Event password", child_html)
            self.assertIn("Unlock event", child_html)
            self.assertNotIn("Locked program", child_html)

            guest.fill('input[name="password"]', "outer-series-pass")
            with guest.expect_navigation() as child_wrong_nav:
                guest.get_by_role("button", name="Unlock event").click()
            self.assertEqual(403, child_wrong_nav.value.status)
            self.assertNotIn("Locked program", guest.content())

            guest.goto(f"{self.base}/event-series/outer-password-series")
            guest.wait_for_selector("h1")
            self.assertTrue(guest.locator("h1").is_visible())
            self.assertIn("Locked series", guest.locator("h1").inner_text())
            with guest.expect_navigation():
                guest.get_by_role("link", name="Locked program").click()
            guest.wait_for_selector('input[name="password"]')
            guest.fill('input[name="password"]', "inner-fixture-pass")
            with guest.expect_navigation() as child_ok_nav:
                guest.get_by_role("button", name="Unlock event").click()
            self.assertEqual(200, child_ok_nav.value.status)

            admin = browser.new_page()
            admin.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            admin.goto(self.base + '/operator/event-series')
            admin.get_by_role("button", name="Locked series", exact=True).click()
            view_password = admin.locator('#seriesViewPassword')
            assert view_password.is_visible()
            assert view_password.input_value() == ''
            assert view_password.get_attribute('required') is None
            admin.locator('#seriesDescription').fill('rotated series description')
            with admin.expect_response(lambda r: r.request.method == 'PUT') as kept:
                admin.get_by_role("button", name="Save Event Series", exact=True).click()
            kept_response = kept.value
            assert kept_response.status == 200
            assert json.loads(kept_response.text())['has_password'] is True
            admin.wait_for_function("!document.getElementById('seriesEdit').inert")
            admin.locator('#seriesViewPassword').fill('rotated-series-pass')
            with admin.expect_response(lambda r: r.request.method == 'PUT') as kept:
                admin.get_by_role("button", name="Save Event Series", exact=True).click()
            kept_response = kept.value
            admin.wait_for_function("document.getElementById('seriesViewPassword').value === ''")
            assert kept_response.status == 200
            assert json.loads(kept_response.text())['has_password'] is True
            assert 'rotated-series-pass' not in kept_response.text()

            guest.goto(f"{self.base}/event-series/outer-password-series")
            guest.wait_for_selector('input[name="password"]')
            guest.fill('input[name="password"]', "outer-series-pass")
            with guest.expect_navigation() as stale_nav:
                guest.get_by_role("button", name="Open event series").click()
            self.assertEqual(403, stale_nav.value.status)
            self.assertNotIn("Locked series", guest.content())

            guest.goto(f"{self.base}/event-series/outer-password-series")
            guest.wait_for_selector('input[name="password"]')
            guest.fill('input[name="password"]', "rotated-series-pass")
            with guest.expect_navigation() as rotated_nav:
                guest.get_by_role("button", name="Open event series").click()
            self.assertEqual(200, rotated_nav.value.status)
            self.assertIn("Locked series", guest.content())

    def test_event_series_publication_gate(self):
        start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        schedule = dict(timezone="UTC", repeat="none",
                        start=start.strftime("%Y-%m-%dT%H:%M"), end=end.strftime("%Y-%m-%dT%H:%M"))
        events = {}
        for visibility, title in (("public", "Public program"), ("private", "Private program")):
            response = self.client.post("/api/live-events", headers=self.auth, json=dict(
                title=title, slug=title.lower().replace(" ", "-"), visibility=visibility,
                type="live", latency="standard", save_replay=False, schedule=schedule))
            self.assertEqual(201, response.status_code, response.get_data(as_text=True))
            events[visibility] = response.get_json()["id"]

        with sync_playwright() as playwright, chromium(playwright) as browser:
            admin = browser.new_page()
            admin.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            admin.goto(f"{self.base}/operator/live-events")
            admin.get_by_role("link", name="Event series").click()
            admin.click("#seriesNew")
            admin.fill("#seriesTitle", "Council program")
            admin.fill("#seriesSlug", "council-program")
            for event_id in events.values():
                admin.check(f'input[name=events][value="{event_id}"]')
            admin.click("text=Save Event Series")
            admin.wait_for_selector('#seriesStatus:has-text("Private draft saved.")')
            self.assertEqual(404, self.client.get("/event-series/council-program").status_code)

            admin.click("#seriesPreview")
            admin.wait_for_selector("#seriesPreviewLink", state="visible")
            preview = admin.get_attribute("#seriesPreviewLink", "href")
            preview_response = self.client.get(preview)
            self.assertEqual(200, preview_response.status_code)
            preview_text = preview_response.get_data(as_text=True)
            self.assertIn("Public program", preview_text)
            self.assertIn("Private program", preview_text)
            self.assertEqual(404, self.client.get(preview + "invalid").status_code)

            self.assertEqual(200, self.client.put("/api/policy/publication", headers=self.auth,
                                                   json={"required": True}).status_code)

            admin.select_option("#seriesVisibility", "public")
            admin.click("text=Save Event Series")
            admin.wait_for_selector('#seriesStatus:has-text("Waiting for publication approval.")')
            self.assertEqual(404, self.client.get("/event-series/council-program").status_code)

            actions = self.client.get("/api/actions", headers=self.auth).get_json()
            waiting = [action for action in actions if action["step"] == "publication"]
            self.assertEqual(1, len(waiting))
            action_id = waiting[0]["id"]
            self.assertEqual(204, self.client.post(
                f"/api/actions/{action_id}/decision", headers=self.auth,
                json={"decision": "approve"}).status_code)

            guest = browser.new_page()
            guest.goto(f"{self.base}/event-series/council-program")
            guest.wait_for_selector('a:has-text("View Public program")')
            guest.wait_for_selector('h1:has-text("Council program")')
            expect(guest.get_by_text("Private program", exact=True)).to_have_count(0)

    def test_event_series_upcoming_modes(self):
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = now + timedelta(days=2)
        end = start + timedelta(hours=1)
        schedule = dict(
            timezone="UTC",
            repeat="days",
            count=3,
            start=start.strftime("%Y-%m-%dT%H:%M"),
            end=end.strftime("%Y-%m-%dT%H:%M"),
        )
        response = self.client.post(
            "/api/live-events",
            headers=self.auth,
            json=dict(
                title="Daily program",
                slug="daily-program",
                visibility="public",
                type="live",
                latency="standard",
                save_replay=False,
                schedule=schedule,
            ),
        )
        self.assertEqual(201, response.status_code, response.get_data(as_text=True))
        event_id = response.get_json()["id"]

        with sync_playwright() as playwright, chromium(playwright) as browser:
            admin = browser.new_page()
            admin.add_init_script(
                f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})"
            )
            admin.goto(f"{self.base}/operator/event-series")
            admin.click("#seriesNew")
            admin.fill("#seriesTitle", "Daily program")
            admin.fill("#seriesSlug", "program-schedule")
            admin.check(f'input[name=events][value="{event_id}"]')
            admin.select_option("#seriesVisibility", "public")
            with admin.expect_response(
                lambda r: r.request.method == "PUT"
            ) as response_info:
                admin.get_by_role("button", name="Save Event Series").click()
            self.assertEqual(200, response_info.value.status)
            admin.wait_for_function(
                "!document.querySelector('#seriesTitle').closest('form').inert"
            )

            viewer = browser.new_page()
            viewer.goto(f"{self.base}/event-series/program-schedule")
            section = viewer.locator('section[aria-labelledby="upcoming-heading"]')
            initial_links = section.get_by_role("link", name="View Daily program")
            expect(initial_links).to_have_count(3)
            time = section.locator('time').first
            expect(time).to_have_attribute('datetime', start.isoformat(timespec='milliseconds').replace('+00:00', 'Z'))
            for i in range(3):
                href = initial_links.nth(i).get_attribute("href") or ""
                self.assertIn("occurrence=", href)
                self.assertNotIn("mode", href)

            admin.select_option("#seriesUpcomingMode", "next")
            with admin.expect_response(
                lambda r: r.request.method == "PUT"
            ) as response_info:
                admin.get_by_role("button", name="Save Event Series").click()
            self.assertEqual(200, response_info.value.status)
            admin.wait_for_function(
                "!document.querySelector('#seriesUpcomingMode').closest('form').inert"
            )

            viewer.reload()
            section = viewer.locator('section[aria-labelledby="upcoming-heading"]')
            next_links = section.get_by_role("link", name="View Daily program")
            expect(next_links).to_have_count(1)
            href = next_links.first.get_attribute("href") or ""
            self.assertIn("occurrence=", href)
            self.assertNotIn("mode", href)

            admin.select_option("#seriesUpcomingMode", "grouped")
            with admin.expect_response(
                lambda r: r.request.method == "PUT"
            ) as response_info:
                admin.get_by_role("button", name="Save Event Series").click()
            self.assertEqual(200, response_info.value.status)
            admin.wait_for_function(
                "!document.querySelector('#seriesUpcomingMode').closest('form').inert"
            )

            viewer.reload()
            section = viewer.locator('section[aria-labelledby="upcoming-heading"]')
            grouped_links = section.get_by_role("link", name="View Daily program")
            expect(grouped_links).to_have_count(3)
            for i in range(3):
                href = grouped_links.nth(i).get_attribute("href") or ""
                self.assertIn("occurrence=", href)
                self.assertNotIn("mode", href)
            expect(section.locator('h3:has-text("Daily program")')).to_have_count(1)

    def test_event_series_released_replay(self):
        start = (datetime.now(timezone.utc) - timedelta(days=1)).replace(second=0, microsecond=0)
        opens = int(start.timestamp())
        end = start + timedelta(minutes=30)
        first_day, last_day = start.date().isoformat(), (start + timedelta(days=2)).date().isoformat()
        council = work_id("station", "vimeo", "71")

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.locator("#inventory").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            page.locator("#transfer").click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=15000)

            pub = self.client.post("/api/migration/public-catalog", headers=self.auth, json=dict(works=[council]))
            self.assertEqual(200, pub.status_code, pub.get_data(as_text=True))

            created = self.client.post(f"/api/live-events?from={first_day}&to={last_day}",
                headers=self.auth, json=dict(
                    title="Migrated replay", slug="migrated-replay", visibility="public",
                    type="live", latency="standard", save_replay=True,
                    schedule=dict(timezone="UTC", repeat="none",
                        start=start.strftime("%Y-%m-%dT%H:%M"),
                        end=end.strftime("%Y-%m-%dT%H:%M"))))
            self.assertEqual(201, created.status_code, created.get_data(as_text=True))
            event = created.get_json()
            replay_path = f"/api/live-events/{event['id']}/{opens}/imported-replay"
            choices = self.client.get(replay_path, headers=self.auth).get_json()["choices"]
            chosen = next(item for item in choices if item["work"] == council)
            attached = self.client.put(replay_path, headers=self.auth,
                                       json={"work": chosen["work"], "digest": chosen["digest"]})
            self.assertEqual(200, attached.status_code, attached.get_data(as_text=True))

            created_series = self.client.post("/api/event-series", headers=self.auth, json={})
            self.assertEqual(201, created_series.status_code, created_series.get_data(as_text=True))
            series = created_series.get_json()
            updated_series = self.client.put(f"/api/event-series/{series['id']}", headers=self.auth,
                json=dict(revision=series["revision"], slug="test-series",
                    settings=series["settings"] | dict(title="Test series", visibility="public", events=[event["id"]])))
            self.assertEqual(200, updated_series.status_code, updated_series.get_data(as_text=True))
            self.assertEqual("eligible", updated_series.get_json()["publication"])

            index = self.client.get("/event-series/test-series")
            self.assertEqual(200, index.status_code)
            self.assertNotIn(b"/event-media/", index.data)

            viewer = browser.new_page()
            viewer.goto(self.base + "/event-series/test-series")
            viewer.get_by_role("link", name="Watch Migrated replay").click()
            expect(viewer).to_have_url(self.base + f"/events/migrated-replay?occurrence={opens}")
            video = viewer.locator("#eventPlayer video")
            expect(video).to_be_visible()
            video.evaluate("async video => { video.muted = true; await video.play(); }")
            viewer.wait_for_function(
                "v => v.videoWidth === 320 && v.currentTime > 0.15", arg=video.element_handle(),
            )

            page.goto(self.base + "/operator/event-series")
            page.get_by_role("button", name="Test series", exact=True).click()
            hide_recording = page.get_by_role("checkbox", name="Hide recording: Migrated replay")
            expect(hide_recording).to_be_visible()
            expect(hide_recording).not_to_be_checked()
            hide_recording.check()
            with page.expect_response(
                lambda response: response.request.method == "PUT"
                and response.url.endswith(f"/api/event-series/{series['id']}")
            ) as response_info:
                page.get_by_role("button", name="Save Event Series").click()
            assert response_info.value.status == 200

            page.goto(self.base + "/event-series/test-series")
            expect(page.get_by_role("link", name="Watch Migrated replay")).to_have_count(0)

            viewer.goto(self.base + f"/events/migrated-replay?occurrence={opens}")
            video = viewer.locator("#eventPlayer video")
            expect(video).to_be_visible()
            video.evaluate("async video => { video.muted = true; await video.play(); }")
            viewer.wait_for_function(
                "v => v.videoWidth === 320 && v.currentTime > 0.15", arg=video.element_handle(),
            )

            page.goto(self.base + "/operator/event-series")
            page.get_by_role("button", name="Test series", exact=True).click()
            hide_recording = page.get_by_role("checkbox", name="Hide recording: Migrated replay")
            expect(hide_recording).to_be_checked()
            hide_recording.uncheck()
            with page.expect_response(
                lambda response: response.request.method == "PUT"
                and response.url.endswith(f"/api/event-series/{series['id']}")
            ) as response_info:
                page.get_by_role("button", name="Save Event Series").click()
            assert response_info.value.status == 200

            page.goto(self.base + "/event-series/test-series")
            expect(page.get_by_role("link", name="Watch Migrated replay")).to_have_count(1)

            withdrawn = self.client.delete(f"/api/live-events/{event['id']}/{opens}/delivery", headers=self.auth)
            self.assertEqual(204, withdrawn.status_code, withdrawn.get_data(as_text=True))

            viewer.goto(self.base + "/event-series/test-series")
            expect(viewer.get_by_role("link", name="Watch Migrated replay")).to_have_count(0)

    def test_bilingual_event_series_translation_preview_and_publish(self):
        create_resp = self.client.post("/api/event-series", json={}, headers=self.auth)
        self.assertEqual(create_resp.status_code, 201)
        entry = create_resp.get_json()

        update_resp = self.client.put(
            f"/api/event-series/{entry['id']}",
            json={
                "revision": entry.get("revision"),
                "slug": "bilingual-series",
                "settings": {
                    "title": "Festival program",
                    "description": "Original description",
                    "visibility": "public",
                },
            },
            headers=self.auth,
        )
        self.assertEqual(update_resp.status_code, 200)

        def is_series_put(response):
            return (
                response.request.method == "PUT"
                and f"/api/event-series/{entry['id']}" in response.url
            )

        with sync_playwright() as playwright, chromium(playwright) as browser:
            operator_context = browser.new_context()
            guest_context = browser.new_context()
            operator_context.add_init_script(
                script=f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})"
            )

            operator_page = operator_context.new_page()
            guest_page = guest_context.new_page()

            operator_page.goto(self.base + "/operator/event-series")
            operator_page.get_by_role("button", name="Festival program", exact=True).click()
            operator_page.locator("#seriesTranslationLanguage").select_option("fr")
            operator_page.locator("#translatedTitle").fill("Programme du festival")

            with operator_page.expect_response(is_series_put) as first_save_info:
                operator_page.get_by_role("button", name="Save Event Series", exact=True).click()
            self.assertEqual(first_save_info.value.status, 200)
            operator_page.wait_for_function("!document.getElementById('seriesEdit').inert")

            preview_resp = self.client.post(
                f"/api/event-series/{entry['id']}/preview",
                headers=self.auth,
            )
            self.assertEqual(preview_resp.status_code, 200)
            preview = preview_resp.get_json()["preview"]

            guest_page.goto(self.base + preview + "&lang=fr")
            expect(
                guest_page.get_by_role(
                    "heading", level=1, name="Programme du festival", exact=True
                )
            ).to_be_visible()

            guest_page.goto(self.base + "/event-series/bilingual-series?lang=fr")
            expect(
                guest_page.get_by_role(
                    "heading", level=1, name="Festival program", exact=True
                )
            ).to_be_visible()
            expect(
                guest_page.get_by_role(
                    "heading", level=1, name="Programme du festival", exact=True
                )
            ).not_to_be_visible()

            operator_page.locator("#seriesTranslationLanguage").select_option("fr")
            operator_page.locator("#translationPublished").check()
            with operator_page.expect_response(
                lambda response: is_series_put(response) and response.status == 200
            ) as publish_save_info:
                operator_page.get_by_role("button", name="Save Event Series", exact=True).click()
            self.assertEqual(publish_save_info.value.status, 200)

            guest_page.goto(self.base + "/event-series/bilingual-series")
            francais_link = guest_page.get_by_role("link", name="Français", exact=True)
            expect(francais_link).to_be_visible()
            with guest_page.expect_navigation() as french_navigation:
                francais_link.click()
            french_response = french_navigation.value
            self.assertEqual(french_response.headers.get("content-language"), "fr")

            expect(
                guest_page.get_by_role(
                    "heading", level=1, name="Programme du festival", exact=True
                )
            ).to_be_visible()
            expect(guest_page.get_by_text("Original description", exact=True)).to_be_visible()
            expect(guest_page.locator("html")).to_have_attribute("lang", "fr")
            negotiated = self.client.get(
                '/event-series/bilingual-series',
                headers={'Accept-Language': 'fr-CA, en;q=0.8'},
            )
            self.assertEqual('fr', negotiated.headers.get('Content-Language'))
            self.assertIn('Programme du festival', negotiated.get_data(as_text=True))

    def test_operator_event_series_sort_filter_grid_copy_link(self):
        for slug, title in (("gallery-night", "Gallery night"), ("community-brief", "Community brief")):
            post_data = dict(slug=slug, settings=dict(title=title, visibility='public'))
            response = self.client.post('/api/event-series', headers=self.auth, json=post_data)
            self.assertEqual(response.status_code, 201)
            entry = response.get_json()
            response = self.client.put(f"/api/event-series/{entry['id']}", headers=self.auth, json=dict(revision=entry['revision'], slug=slug, settings=dict(title=title, visibility='public')))
            self.assertEqual(response.status_code, 200)

        viewer_url = self.base+'/event-series/gallery-night'
        with sync_playwright() as playwright, chromium(playwright) as browser:
            context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
            context.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)});")
            page = context.new_page()
            try:
                page.goto(self.base + '/operator/event-series')
                expect(page.locator("#seriesWorkspace")).to_be_visible()
                series_buttons = page.locator("#seriesList button")
                expect(series_buttons).to_have_count(2)

                page.select_option("#seriesSort", "title")
                titles = page.locator("#seriesList button").evaluate_all("els => els.map(el => el.textContent.trim())")
                self.assertEqual(titles, ["Community brief", "Gallery night"])

                page.fill("#seriesSearch", "GALLERY-NIGHT")
                expect(page.locator("#seriesList button")).to_have_count(1)
                expect(page.locator("#seriesList button")).to_contain_text("Gallery night")

                page.select_option("#seriesView", "grid")
                expect(page.locator("#seriesList")).to_have_css("display", "grid")
                self.assertEqual(page.locator("#seriesView").input_value(), "grid")

                page.get_by_role("button", name="Gallery night").click()
                page.get_by_role("button", name="Copy link", exact=True).click()
                expect(page.locator("#seriesStatus")).to_have_text("Link copied.")
                copied = page.evaluate("() => navigator.clipboard.readText()")
                self.assertEqual(copied, viewer_url)

                page.fill("#seriesSearch", "does-not-exist")
                expect(page.locator("#seriesList button")).to_have_count(0)
                expect(page.locator("#seriesEmpty")).to_be_visible()

                page.fill("#seriesSearch", "")
                page.select_option("#seriesView", "list")
                expect(page.locator("#seriesList button")).to_have_count(2)
                self.assertEqual(page.locator("#seriesView").input_value(), "list")
            finally:
                context.close()

    def test_event_series_builder_publication_flow(self):
        import base64
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(
                f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(f"{self.base}/operator/event-series")
            page.click("#seriesNew")
            page.fill("#seriesTitle", "Synthetic series")
            page.fill("#seriesSlug", "synthetic-series")
            page.check("#es-block-faq")
            page.click("text=Add FAQ")
            page.get_by_label("Question").fill("What is this?")
            page.get_by_label("Answer").fill("A synthetic series.")
            for _ in range(3):
                page.get_by_role("button", name="Move FAQ up").click()
            page.check("#es-block-navigation")
            page.check("#es-block-agenda")
            page.get_by_role("button", name="Add navigation link").click()
            page.get_by_label("Link label").fill("Program information")
            page.get_by_label("Link URL").fill("https://example.org/program")
            page.check('input[name="seriesDark"]')
            png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPw4AAAAASUVORK5CYII=")
            fixture = dict(name="fixture.png", mimeType="image/png", buffer=png)
            page.locator('input[data-series-image="logo"]').set_input_files(fixture)
            page.locator('input[data-series-image="hero_image"]').set_input_files(fixture)
            page.wait_for_selector("#seriesEdit:not([inert])")
            with page.expect_response(lambda resp: "/api/event-series/" in resp.url and resp.request.method == "PUT") as info:
                page.click("text=Save Event Series")
            resp = info.value
            self.assertEqual(200, resp.status)
            saved = resp.json()["settings"]["presentation"]
            self.assertTrue(saved["logo"] and saved["hero_image"])
            page.wait_for_selector("#seriesPreview", state="visible")
            page.wait_for_selector("#seriesEdit:not([inert])")
            page.click("#seriesPreview")
            page.wait_for_selector("#seriesPreviewLink", state="visible")
            href = page.get_attribute("#seriesPreviewLink", "href")
            mobile = browser.new_context(viewport={"width": 390, "height": 844})
            preview = mobile.new_page()
            preview.goto(self.base + href)
            expect(preview.locator("body")).to_have_attribute("data-theme", "dark")
            heads = preview.eval_on_selector_all("main h1, main h2", "els => els.slice(0, 2).map(e => e.tagName)")
            self.assertEqual(["H2", "H1"], heads)
            preview.locator("summary", has_text="What is this?").click()
            expect(preview.get_by_text("A synthetic series.")).to_be_visible()
            expect(preview.get_by_role("heading", name="Agenda")).to_be_visible()
            expect(preview.get_by_role("link", name="Program information")).to_have_attribute(
                "href", "https://example.org/program",
            )
            expect(preview.locator("img.event-series-logo")).to_have_js_property("naturalWidth", 1)
            expect(preview.locator("img.event-series-hero")).to_have_js_property("naturalWidth", 1)
            self.assertEqual(404, self.client.get("/event-series/synthetic-series").status_code)
            page.fill("#seriesDescription", "Updated synthetic series.")
            page.wait_for_selector("#seriesEdit:not([inert])")
            with page.expect_response(lambda resp: "/api/event-series/" in resp.url and resp.request.method == "PUT") as info:
                page.click("text=Save Event Series")
            resp = info.value
            self.assertEqual(200, resp.status)
            saved2 = resp.json()["settings"]["presentation"]
            self.assertTrue(saved2["logo"] and saved2["hero_image"])
            self.assertEqual((saved["logo"], saved["hero_image"]), (saved2["logo"], saved2["hero_image"]))
            page.wait_for_selector("#seriesPreview", state="visible")
            page.wait_for_selector("#seriesEdit:not([inert])")
            page.click("#seriesPreview")
            page.wait_for_selector("#seriesPreviewLink", state="visible")
            preview.goto(self.base + page.get_attribute("#seriesPreviewLink", "href"))
            expect(preview.locator("img.event-series-logo")).to_have_js_property("naturalWidth", 1)
            expect(preview.locator("img.event-series-hero")).to_have_js_property("naturalWidth", 1)
            self.assertEqual(404, self.client.get("/event-series/synthetic-series").status_code)
            mobile.close()

    def test_event_series_collection_move_flow(self):
        parent = self.client.post(
            "/api/library/folders",
            headers=self.auth,
            json=dict(name="Smoke parent collection", color="#3266aa", parent=None),
        )
        self.assertEqual(201, parent.status_code)
        parent_id = parent.get_json()["id"]

        nested = self.client.post(
            "/api/library/folders",
            headers=self.auth,
            json=dict(name="Smoke nested collection", color="#3266aa", parent=parent_id),
        )
        self.assertEqual(201, nested.status_code)
        nested_id = nested.get_json()["id"]

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(
                f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(f"{self.base}/operator/library")
            page.get_by_role("button", name="Load library").click()
            page.get_by_role("link", name="Event series", exact=True).click()
            page.select_option("#seriesCollectionFilter", str(nested_id))
            page.click("#seriesNew")
            page.fill("#seriesTitle", "Synthetic collection series")
            page.fill("#seriesSlug", "synthetic-collection-series")
            page.select_option("#seriesVisibility", "public")
            page.wait_for_selector("#seriesEdit:not([inert])")
            with page.expect_response(
                lambda resp: "/api/event-series/" in resp.url and resp.request.method == "PUT"
            ) as info:
                page.click("text=Save Event Series")
            self.assertEqual(200, info.value.status)
            series = info.value.json()
            assert series["collection"] == nested_id
            assert series["publication"] == "eligible"
            resp = self.client.get(series["watch"])
            assert resp.status_code == 200

            page.select_option("#seriesCollectionDestination", parent_id)
            page.get_by_role("button", name="Move series").click()
            expect(page.locator("#seriesStatus")).to_have_text("Series moved.")

            item = page.locator("#seriesList").get_by_role("button", name="Synthetic collection series", exact=True)
            expect(item).to_have_count(0)
            page.select_option("#seriesCollectionFilter", parent_id)
            expect(item).to_be_visible()

            after = self.client.get(f"/api/event-series/{series['id']}", headers=self.auth).get_json()
            assert after["revision"] == series["revision"]
            assert after["publication"] == series["publication"]
            assert after["watch"] == series["watch"]
            assert after["collection"] == parent_id
            assert self.client.get(after["watch"]).status_code == 200

            unauth = self.client.put(
                f"/api/event-series/{series['id']}/collection",
                json={"folder": parent_id},
            )
            assert unauth.status_code == 403

            missing = self.client.put(
                f"/api/event-series/{series['id']}/collection",
                json={"folder": f"{parent_id}-missing"},
                headers=self.auth,
            )
            assert missing.status_code == 404

    def test_timed_video_registration(self):
        uid = work_id("station", "vimeo", "71")
        endpoint = f"/api/library/work/{uid}/registration"
        watch = f"/watch/work/{uid}"
        email = "viewer@example.test"

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(f"{self.base}/operator/migration")
            page.locator("#inventory").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            page.locator("#transfer").click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=30000)
            published = self.client.post("/api/migration/public-catalog", headers=self.auth, json=dict(works=[uid]))
            self.assertEqual(200, published.status_code)

            page.goto(f"{self.base}/operator/videos/{uid}/interactivity")
            editor = page.locator("#registrationEditor")
            editor.locator('input[name="enabled"]').check()
            timing = editor.locator('select[name="timing"]')
            timing.select_option("during")
            editor.locator('input[name="time_seconds"]').fill("1")
            editor.get_by_role("button", name="Save registration").click()
            expect(editor.locator("#registrationStatus")).to_have_text("Registration saved.")

            viewer = browser.new_page()
            viewer.goto(self.base + watch)
            video = viewer.locator("video")
            prompt = viewer.locator('[data-timed-registration="during"]')
            expect(prompt).to_be_hidden()
            expect(video).to_be_visible()
            video.evaluate("async video => { video.muted = true; await video.play(); }")
            expect(prompt).to_be_visible()
            self.assertTrue(video.evaluate("video => video.paused && video.videoWidth > 0 && video.currentTime >= 1"))
            prompt.locator('input[name="email"]').fill(email)
            prompt.locator('button[type="submit"]:not([formnovalidate])').click()
            expect(prompt).to_have_count(0)
            media_path = video.get_attribute("src").split("#", 1)[0]
            self.assertIn("#t=", video.get_attribute("src"))
            registrants, = self.client.get(endpoint, headers=self.auth).get_json()["registrants"]
            self.assertEqual(email, registrants["email"])

            timing.select_option("after")
            editor.get_by_role("button", name="Save registration").click()
            expect(editor.locator("#registrationStatus")).to_have_text("Registration saved.")
            self.assertEqual(404, self.client.get(media_path).status_code)
            viewer.goto(self.base + watch)
            prompt = viewer.locator('[data-timed-registration="after"]')
            expect(prompt).to_be_hidden()
            video.evaluate("async video => { video.muted = true; await video.play(); }")
            expect(prompt).to_be_visible()
            self.assertTrue(video.evaluate("video => video.ended"))
            prompt.get_by_role("button", name="Dismiss registration").click()
            expect(prompt).to_be_hidden()
            expect(video).to_be_visible()

            embed_page = browser.new_page()
            embed_page.goto(self.base + "/watch")
            embed_page.set_content(f'<iframe title="Example embed" src="{self.base}/embed/work/{uid}?loop=1"></iframe>')
            frame = embed_page.frame_locator("iframe")
            player = frame.locator("video")
            embedded_prompt = frame.locator('[data-timed-registration="after"]')
            expect(player).to_be_visible()
            expect(embedded_prompt).to_be_hidden()
            player.evaluate("async video => { video.muted = true; await video.play(); }")
            expect(embedded_prompt).to_be_visible()
            self.assertTrue(player.evaluate("video => video.ended"))
            embedded_prompt.get_by_role("button", name="Dismiss registration").click()
            expect(embedded_prompt).to_be_hidden()
            self.assertTrue(player.evaluate("video => video.loop"))

    def test_timed_video_registration_showcase(self):
        uid = work_id("station", "vimeo", "71")
        endpoint = f"/api/library/work/{uid}/registration"
        email = "viewer@example.test"

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(f"{self.base}/operator/migration")
            page.locator("#inventory").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            page.locator("#transfer").click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=30000)
            published = self.client.post("/api/migration/public-catalog", headers=self.auth, json=dict(works=[uid]))
            self.assertEqual(200, published.status_code)

            page.goto(f"{self.base}/operator/videos/{uid}/interactivity")
            editor = page.locator("#registrationEditor")
            editor.locator('input[name="enabled"]').check()
            timing = editor.locator('select[name="timing"]')
            timing.select_option("during")
            editor.locator('input[name="time_seconds"]').fill("1")
            editor.get_by_role("button", name="Save registration").click()
            expect(editor.locator("#registrationStatus")).to_have_text("Registration saved.")

            resp = self.client.post("/api/showcases", headers=self.auth)
            self.assertEqual(201, resp.status_code)
            created = resp.get_json()
            resp = self.client.put(f"/api/showcases/{created['id']}", headers=self.auth, json=dict(
                revision=created["revision"],
                slug=created["slug"],
                settings=created["settings"] | dict(visibility="public", layout="featured", next_video=True, loop=True, videos=[uid])
            ))
            self.assertEqual(200, resp.status_code)
            watch = f"/showcases/{created['slug']}"
            preview = self.client.post(f"/api/showcases/{created['id']}/preview", headers=self.auth)
            self.assertEqual(200, preview.status_code)
            internal = self.client.get(preview.get_json()["preview"])
            self.assertNotIn(b'id="showcaseMemberRegistration"', internal.data)

            viewer = browser.new_page()
            viewer.goto(self.base + watch)
            video = viewer.locator(".showcase-screen video")
            prompt = viewer.locator("#showcaseMemberRegistration")
            form = viewer.frame_locator("#showcaseMemberRegistration iframe")
            expect(prompt).to_be_hidden()
            video.evaluate("async video => { video.muted = true; await video.play(); }")
            expect(prompt).to_be_visible()
            self.assertTrue(video.evaluate("video => video.paused && video.currentTime >= 1 && video.videoWidth > 0"))
            member_url = prompt.locator("iframe").get_attribute("src")
            self.assertEqual(404, self.client.get(member_url).status_code)
            form.get_by_label("Email").fill(email)
            form.get_by_role("button", name="Register and watch", exact=True).click()
            expect(prompt).to_be_hidden()
            expect(video).to_have_js_property("paused", False)
            media_path = video.get_attribute("src")
            registrant, = self.client.get(endpoint, headers=self.auth).get_json()["registrants"]
            self.assertEqual(email, registrant["email"])
            with self.client.get(media_path) as response:
                self.assertEqual(200, response.status_code)

            timing.select_option("after")
            editor.get_by_role("button", name="Save registration").click()
            expect(editor.locator("#registrationStatus")).to_have_text("Registration saved.")
            self.assertEqual(404, self.client.get(media_path).status_code)
            self.assertEqual(404, self.client.get(member_url, headers={"Sec-Fetch-Dest": "iframe"}).status_code)

            saved = resp.get_json()
            grid = self.client.put(f"/api/showcases/{saved['id']}", headers=self.auth, json=dict(
                revision=saved["revision"], slug=saved["slug"],
                settings=saved["settings"] | dict(layout="grid", grid_media="players"),
            ))
            self.assertEqual(200, grid.status_code)
            viewer.goto(self.base + "/watch")
            viewer.set_content(f'<iframe title="Embedded gallery" src="{self.base}{watch}/embed"></iframe>')
            gallery = viewer.frame_locator('iframe[title="Embedded gallery"]')
            video = gallery.locator(".showcase-playlist video")
            prompt = gallery.locator("#showcaseMemberRegistration")
            form = gallery.frame_locator("#showcaseMemberRegistration iframe")
            expect(prompt).to_be_hidden()
            video.evaluate("async video => { video.muted = true; await video.play(); }")
            expect(prompt).to_be_visible()
            expect(form.get_by_label("Email")).to_be_visible()
            self.assertTrue(video.evaluate("video => video.ended && video.videoWidth > 0"))
            prompt.get_by_role("button", name="Dismiss registration").click()
            expect(prompt).to_be_hidden()
            expect(video).to_have_js_property("paused", False)

            timing.select_option("before")
            editor.get_by_role("button", name="Save registration").click()
            expect(editor.locator("#registrationStatus")).to_have_text("Registration saved.")
            reg_url = f"/api/showcases/{saved['id']}/registration"
            registration = self.client.get(reg_url, headers=self.auth).get_json()
            put_resp = self.client.put(reg_url, headers=self.auth, json=dict(
                revision=registration["revision"],
                settings=registration["settings"] | dict(enabled=True, heading="Gallery guest register"),
            ))
            self.assertEqual(put_resp.status_code, 200)
            viewer.goto(self.base + watch)
            gallery = viewer.locator(".showcase-playlist")
            expect(gallery.locator("video")).to_have_count(0)
            expect(viewer.locator("#showcaseMemberRegistration")).to_have_count(0)
            viewer.get_by_label("Email", exact=True).fill("gallery@example.com")
            viewer.get_by_role("button", name="Register and watch", exact=True).click()
            video = gallery.locator("video")
            expect(video).to_be_hidden()
            self.assertIsNone(video.get_attribute("src"))
            self.assertEqual(video.evaluate("v => v.currentSrc || v.src || ''"), "")
            expect(gallery.locator("video[src]")).to_have_count(0)
            panel = viewer.locator("#showcaseMemberRegistration")
            expect(panel).to_be_hidden()
            viewer.get_by_role("button", name="Register to watch", exact=True).click()
            expect(panel).to_be_visible()
            iframe = panel.locator("iframe")
            iframe_url = iframe.get_attribute("src")
            self.assertEqual(self.client.get(iframe_url).status_code, 404)
            frame = viewer.frame_locator("#showcaseMemberRegistration iframe")
            pending = iframe_url.split('?')[0].rsplit('/', 1)[-1]
            base_asset = f"/showcase-media/{pending}"
            for suffix in ("", "?download=1", "?poster=1", "?track=0"):
                self.assertEqual(self.client.get(base_asset + suffix).status_code, 404)
            frame.get_by_label("Email").fill("viewer@example.test")
            frame.get_by_role("button", name="Register and watch", exact=True).click()
            expect(panel).to_be_hidden()
            expect(video).to_be_visible()
            viewer.wait_for_function(
                "v => v.readyState >= 2 && v.videoWidth > 0", arg=video.element_handle())
            self.assertGreaterEqual(video.evaluate("v => v.readyState"), 2)
            self.assertGreater(video.evaluate("v => v.videoWidth"), 0)
            video.evaluate("async v => { v.muted = true; await v.play(); }")
            expect(video).to_have_js_property("paused", False)

    def test_showcase_style_migration_roundtrip(self):
        self.source.channel_fixture = True
        self.source.showcase_logo_fixture = "valid"
        self.source.showcase_style_fixture = dict(
            theme="dark", layout="player", brand_color="267ABC",
            web_brand_color=True, embed_brand_color=True, autoplay=True, loop=True,
            allow_share=False, allow_downloads=True, hide_nav=True, seo_title="Festival screenings",
            seo_allow_indexed=True, privacy=dict(view="password", password="no-copy-source-password"),
            embed=dict(html="<button>no-copy-source-html</button>"),
        )

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(f"{self.base}/operator/migration")
            page.locator("#inventory").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            page.locator("#transfer").click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=30000)

            response = self.client.get("/api/migration", headers=self.auth)
            self.assertNotIn("no-copy-source-password", response.get_data(as_text=True))
            self.assertNotIn("no-copy-source-html", response.get_data(as_text=True))
            self.assertNotIn("/media/vimeo-logo.png", response.get_data(as_text=True))
            snapshot = response.get_json()
            album, = snapshot["showcases"]
            channel, inaccessible = snapshot["channels"]
            self.assertEqual("901", channel["source_id"])
            self.assertEqual("/channels/901", channel["uri"])
            self.assertEqual("https://vimeo.com/channels/local-cinema", channel["source_url"])
            ordered = [work_id("station", "vimeo", source_id) for source_id in ("72", "71")]
            self.assertEqual(ordered, channel["members"])
            self.assertEqual(["999"], channel["missing"])
            self.assertEqual("https://vimeo.com/channels/902", inaccessible["source_url"])
            self.assertTrue(any("403" in issue for issue in inaccessible["issues"]))
            channel_endpoint = "/api/showcases/" + channel["target"]
            draft = self.client.get(channel_endpoint, headers=self.auth).get_json()
            self.assertEqual("private", draft["settings"]["visibility"])
            self.assertTrue(draft["settings"]["channel_listed"])
            self.assertEqual("Arts", draft["settings"]["channel_category"])
            self.assertEqual(ordered, draft["settings"]["videos"])
            self.assertEqual("local-cinema", draft["slug"])
            self.assertIsNone(draft["channel"])
            self.assertEqual(channel["source_channel_cover"]["sha256"], draft["settings"]["channel_cover"])
            self.assertEqual(channel["source_channel_header"]["sha256"], draft["settings"]["feature_image"])
            self.assertRegex(draft["settings"]["channel_cover"], r"^[0-9a-f]{64}$")
            self.assertRegex(draft["settings"]["feature_image"], r"^[0-9a-f]{64}$")
            self.assertEqual(404, self.client.get("/channels/local-cinema").status_code)
            row = page.get_by_role("row").filter(has_text="Local Cinema")
            expect(row).to_contain_text("999")
            row.get_by_role("link", name="Open channel").click()
            expect(page.locator('input[name="title"]')).to_have_value("Local Cinema")
            page.get_by_role("button", name="Preview saved showcase").click()
            preview = page.frame_locator("#showcaseFrame")
            self.assertGreater(preview.locator(".showcase-feature-image").evaluate(
                "async image => { await image.decode(); return image.naturalWidth; }"
            ), 0)
            self.assertEqual(320, preview.locator(".showcase-screen video").evaluate(
                "async v => {v.muted = true; await v.play(); return v.videoWidth;}"
            ))
            page.locator('input[name="title"]').fill("Local Cinema edited")
            page.get_by_role("button", name="Save showcase").click()
            expect(page.locator("#showcaseStatus")).to_have_text("Showcase saved.")
            page.goto(f"{self.base}/operator/migration")
            expect(page.locator("#status")).to_have_text("transferred")
            uid = album["target"]
            endpoint = f"/api/showcases/{uid}"
            gallery = self.client.get(endpoint, headers=self.auth).get_json()
            settings = gallery["settings"]
            self.assertRegex(settings["logo"], r"^[0-9a-f]{64}$")
            self.assertEqual("password", album["source_visibility"])
            self.assertEqual("private", settings["visibility"])
            self.assertFalse(settings["indexable"])
            self.assertFalse(gallery["has_password"])
            self.assertIsNone(gallery["watch"])
            self.assertFalse(settings["copy_link"])
            self.assertTrue(settings["download"])
            self.assertEqual("Festival screenings", settings["seo_title"])
            self.assertEqual(404, self.client.get(f"/showcases/{gallery['slug']}").status_code)

            page.get_by_role("link", name="Open showcase").click()
            accent = page.locator('input[name="accent"]')
            expect(accent).to_have_value("#267abc")
            expect(page.locator("#showcaseLayout")).to_have_value("featured")
            page.get_by_role("button", name="Preview saved showcase").click()
            frame_body = page.frame_locator("#showcaseFrame").locator("body.showcase-page")
            expect(frame_body).to_have_attribute("data-theme", "dark")
            expect(frame_body).to_have_attribute("data-next", "yes")
            expect(frame_body).to_have_attribute("data-loop", "yes")
            expect(frame_body).to_have_attribute("style", "--showcase-accent:#267abc")
            logo = frame_body.locator('img[alt="Publisher logo"]')
            expect(logo).to_be_visible()
            expect(logo).to_have_js_property("naturalWidth", 1)

            accent.fill("#123456")
            page.get_by_role("button", name="Save showcase").click()
            expect(page.locator("#showcaseStatus")).to_have_text("Showcase saved.")

            self.source.showcase_logo_fixture = "invalid"
            self.source.showcase_style_fixture.update(brand_color="dddddd", embed_brand_color=False)
            page.goto(f"{self.base}/operator/migration")
            page.locator("#inventory").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            expect(page.locator("#showcaseInventoryRows")).to_contain_text("web and embed accent preferences differ")
            page.locator("#transfer").click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=30000)
            expect(page.locator("#showcaseInventoryRows")).to_contain_text("invalid_image_type")
            updated = self.client.get(endpoint, headers=self.auth).get_json()
            self.assertEqual("#123456", updated["settings"]["accent"])
            self.assertEqual(settings["logo"], updated["settings"]["logo"])
            refreshed = self.client.get("/api/migration", headers=self.auth).get_json()["channels"]
            self.assertEqual(channel["target"], refreshed[0]["target"])
            after = self.client.get(channel_endpoint, headers=self.auth).get_json()
            self.assertEqual("Local Cinema edited", after["settings"]["title"])

    def test_reconciliation_csv_export(self):
        self.source.extra_inventory_count = 101
        endpoint = "/api/migration/reconciliation.csv"
        self.assertEqual(403, self.client.get(endpoint).status_code)
        self.assertEqual(409, self.client.get(endpoint, headers=self.auth).status_code)

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.locator("#inventory").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=60000)
            page.locator("#transfer").click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=60000)
            page.locator("#migrationNextPage").click()
            expect(page.locator("#migrationPageNumber")).to_have_text("Page 2 of 2")

            with page.expect_download() as event:
                page.get_by_role("button", name="Download reconciliation CSV").click()
            download = event.value
            self.assertEqual("cueporch-vimeo-reconciliation.csv", download.suggested_filename)
            with Path(download.path()).open(newline="", encoding="utf8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                self.assertFalse(any("url" in name or "token" in name for name in reader.fieldnames))

        expected_ids = {"71", "72", "73", *(str(source_id) for source_id in range(1000, 1101))}
        by_source_id = {row["source_id"]: row for row in rows}
        self.assertEqual(104, len(rows))
        self.assertEqual(expected_ids, set(by_source_id))
        self.assertEqual("original recovered", by_source_id["71"]["transfer_result"])
        self.assertEqual("playable", by_source_id["71"]["destination_media"])
        self.assertEqual("ready", by_source_id["71"]["public_catalog"])
        self.assertEqual("playback copy only", by_source_id["72"]["transfer_result"])
        self.assertEqual("playable", by_source_id["72"]["destination_media"])
        self.assertEqual("owner file needed", by_source_id["73"]["transfer_result"])
        self.assertEqual("missing", by_source_id["73"]["destination_media"])

    def test_vimeo_folder_migration_public_catalog(self):
        self.source.catalog_fixture = True
        public = work_id("station", "vimeo", "71")
        restricted_embed = work_id("station", "vimeo", "72")
        private = work_id("station", "vimeo", "73")
        restore = "/api/migration/public-catalog"

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.locator("#inventory").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            page.locator("#transfer").click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=30000)

            self.assertEqual(403, self.client.post(restore, json=dict(works=[public])).status_code)
            for uid in (public, restricted_embed, private):
                self.assertEqual(404, self.client.get(f"/watch/work/{uid}").status_code)
            expect(page.get_by_role("checkbox", name="Restore public listing for Exhibition cut", exact=True)).to_have_count(1)

            page.get_by_label("Restore public listing for Station meeting").check()
            page.locator("#migrationRestoreCatalog").click()
            expect(page.locator("#migrationCatalogStatus")).to_contain_text("1 public")
            viewer = browser.new_page()
            viewer.goto(f"{self.base}/watch/work/{public}")
            video = viewer.locator("video")
            expect(video).to_be_visible()
            video.evaluate("async video => { video.muted = true; await video.play(); }")
            viewer.wait_for_function("""() => {
                const video = document.querySelector('video');
                return video.videoWidth > 0 && video.currentTime > 0;
            }""")

            policy = self.client.put("/api/policy/catalog", headers=self.auth, json=dict(required=True))
            self.assertEqual(200, policy.status_code)
            expect(page.get_by_label("Restore public listing for Station meeting")).to_have_count(0)
            page.get_by_label("Restore public listing for Exhibition cut").check()
            page.locator("#migrationRestoreCatalog").click()
            expect(page.locator("#migrationCatalogStatus")).to_contain_text("1 pending")

            retry = self.client.post(restore, headers=self.auth, json=dict(works=[restricted_embed, private]))
            self.assertEqual(200, retry.status_code)
            self.assertEqual({restricted_embed: "pending", private: "unavailable"},
                             {item["work"]: item["state"] for item in retry.get_json()["items"]})
            for uid in (restricted_embed, private):
                self.assertEqual(404, self.client.get(f"/watch/work/{uid}").status_code)

            actions = self.client.get("/api/actions", headers=self.auth).get_json()
            waiting, = [item for item in actions if item["step"] == "catalog"]
            self.assertEqual(restricted_embed, json.loads(waiting["payload"])["work"])
            decision = self.client.post(f"/api/actions/{waiting['id']}/decision", headers=self.auth,
                                        json=dict(decision="approve"))
            self.assertEqual(204, decision.status_code)
            self.assertEqual(200, self.client.get(f"/watch/work/{restricted_embed}").status_code)
            self.assertEqual(404, self.client.get(f"/watch/work/{private}").status_code)
            embed = f"/embed/work/{restricted_embed}"
            denied = self.client.get(embed, headers={"Sec-Fetch-Dest": "iframe", "Referer": "https://other.example/"})
            self.assertEqual(403, denied.status_code)
            allowed = self.client.get(embed, headers={"Sec-Fetch-Dest": "iframe", "Referer": "https://museum.example/"})
            self.assertEqual(200, allowed.status_code)
            self.assertIn("museum.example", allowed.headers["Content-Security-Policy"])

    def test_live_event_recordings(self):
        self.assertEqual(403, self.client.get("/api/migration").status_code)
        self.source.live_event_fixture = True
        self.source.live_event_denied = True
        uid = work_id("station", "vimeo", "71")
        archive_uid = work_id("station", "vimeo", "998")
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            preview = page.get_by_role("button", name="Preview transfer")
            preview.click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            expect(page.locator("#liveEventInventoryIssue")).to_contain_text("HTTP 403")
            expect(page.get_by_role("cell", name="71", exact=True)).to_be_visible()

            self.source.live_event_denied = False
            preview.click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            event = page.locator("details").filter(has=page.get_by_text("Council replay archive", exact=False))
            event.locator("summary").click()
            expect(event.locator("li").filter(has_text="Council replay")).to_contain_text("In source inventory")
            expect(event.locator("li").filter(has_text="Archive-only meeting")).to_contain_text("In source inventory")
            expect(event.get_by_text("Owner file or video access needed")).to_be_visible()
            expect(page.get_by_text("No past streams")).to_be_visible()
            snapshot = self.client.get("/api/migration", headers=self.auth).get_json()
            recordings = snapshot["live_event_archives"][0]["recordings"]
            self.assertEqual([("71", uid), ("999", None), ("998", archive_uid)],
                             [(r["source_id"], r["work"]) for r in recordings])
            self.assertEqual('2026-11-04T01:30:45.125000+00:00', recordings[0]['streamed_on'])
            for recording in recordings[1:]:
                self.assertNotIn('streamed_on', recording)
            streamed = event.locator('li').filter(has_text='Council replay').locator('time')
            expect(streamed).to_have_attribute('datetime', '2026-11-04T01:30:45.125Z')
            expect(streamed).to_have_attribute('title', '2026-11-04T01:30:45.125000+00:00')
            self.assertIn("HTTP 404", recordings[1]["issue"])
            expect(page.get_by_role("cell", name="998", exact=True)).to_be_visible()
            self.assertNotIn("fixture-ingest-key", json.dumps(snapshot))
            self.assertEqual([], self.client.get("/api/library", headers=self.auth).get_json())

            page.get_by_role("button", name="Transfer privately").click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=15000)
            event.locator("summary").click()
            old_link = event.locator("li").filter(has_text="Council replay").get_by_role("link", name="Open in Library")
            expect(old_link).to_have_attribute("href", f"/operator/videos/{uid}")
            open_link = event.locator("li").filter(has_text="Archive-only meeting").get_by_role("link", name="Open in Library")
            expect(open_link).to_have_attribute("href", f"/operator/videos/{archive_uid}")
            expect(event.get_by_text("Owner file or video access needed")).to_be_visible()
            open_link.click()
            expect(page).to_have_url(f"{self.base}/operator/videos/{archive_uid}")
            expect(page.locator("#videoHeading")).to_have_text("Archive-only meeting")
            operator_preview = page.locator("#workspacePreview")
            expect(operator_preview).to_be_visible()
            operator_preview.evaluate("async video => { video.muted = true; await video.play(); }")
            page.wait_for_function("document.querySelector('#workspacePreview').videoWidth === 320")

            review = self.client.post(f"/api/library/work/{archive_uid}/review-links", headers=self.auth,
                                      json={"name": "Live event recording"})
            self.assertEqual(201, review.status_code, review.get_data(as_text=True))
            viewer = browser.new_page()
            self.assertEqual(404, viewer.request.get(self.base + f"/watch/work/{archive_uid}").status)
            viewer.goto(self.base + review.get_json()["path"])
            video = viewer.locator("#reviewTourPlayer")
            expect(video).to_be_visible()
            video.evaluate("async video => { video.muted = true; await video.play(); }")
            viewer.wait_for_function("""() => {
                const video = document.querySelector('#reviewTourPlayer');
                return video.videoWidth === 320 && video.currentTime > .2;
            }""")

    def test_migrated_recordings_play_in_recurring_event(self):
        self.source.live_event_fixture = True
        start = (datetime.now(timezone.utc) - timedelta(hours=28)).replace(second=0, microsecond=0)
        end = start + timedelta(minutes=10)
        first_day, last_day = start.date().isoformat(), (start + timedelta(days=2)).date().isoformat()
        archive = work_id("station", "vimeo", "998")
        council = work_id("station", "vimeo", "71")
        title = "Migrated recurring replay"
        created = self.client.post(f"/api/live-events?from={first_day}&to={last_day}", headers=self.auth, json=dict(
            title=title, slug="migrated-recurring-replay", visibility="private", type="live",
            latency="standard", save_replay=True, schedule=dict(
                timezone="UTC", repeat="days", interval=1, count=2,
                start=start.strftime("%Y-%m-%dT%H:%M"), end=end.strftime("%Y-%m-%dT%H:%M")),
        ))
        self.assertEqual(201, created.status_code, created.get_data(as_text=True))
        event = created.get_json()
        first, second = [entry["id"] for entry in event["occurrences"]]
        base_path = f"/api/live-events/{event['id']}"
        first_replay = f"{base_path}/{first}/imported-replay"
        second_replay = f"{base_path}/{second}/imported-replay"
        self.assertEqual(200, self.client.put(base_path + "/playlist", headers=self.auth,
                         json=dict(enabled=True, autoplay_next=False, loop=False)).status_code)

        with sync_playwright() as playwright, chromium(playwright) as browser:
            producer = browser.new_page()
            producer.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            producer.goto(self.base + "/operator/migration")
            producer.get_by_role("button", name="Preview transfer").click()
            expect(producer.locator("#status")).to_have_text("inventoried", timeout=15000)
            producer.get_by_role("button", name="Transfer privately").click()
            expect(producer.locator("#status")).to_have_text("transferred", timeout=15000)

            choices = self.client.get(first_replay, headers=self.auth).get_json()["choices"]
            by_work = {item["work"]: item for item in choices}
            chosen = by_work[archive]
            producer.goto(self.base + "/operator/live-events")
            producer.locator("#calendarFirst").fill(first_day)
            producer.locator("#calendarLast").fill(last_day)
            producer.get_by_role("button", name="Apply dates").click()
            producer.get_by_role("button", name=f"Open production for {title}", exact=False).first.click()
            producer.get_by_text("Imported replay", exact=True).click()
            select = producer.get_by_label("Vimeo recording")
            expect(select).to_be_enabled()
            select.select_option(label=chosen["title"])
            producer.get_by_role("button", name="Attach replay").click()
            expect(producer.locator("#importedReplayStatus")).to_contain_text("Replay released.")
            self.assertEqual(chosen, self.client.get(first_replay, headers=self.auth).get_json()["attachment"])
            self.assertEqual(200, self.client.put(first_replay, headers=self.auth,
                             json={key: chosen[key] for key in ("work", "digest")}).status_code)
            self.assertEqual(409, self.client.put(first_replay, headers=self.auth,
                             json=dict(work=council, digest=chosen["digest"])).status_code)

            policy = self.client.put("/api/policy/replay", headers=self.auth, json=dict(required=True))
            self.assertEqual(200, policy.status_code, policy.get_data(as_text=True))
            gated = self.client.put(second_replay, headers=self.auth,
                                    json=dict(work=council, digest=by_work[council]["digest"]))
            self.assertEqual("archive_pending", gated.get_json()["state"])
            actions = self.client.get("/api/actions", headers=self.auth).get_json()
            gate, = [item["id"] for item in actions if item["step"] == "replay"]

            invitation = self.client.post("/api/team/invitations", headers={**self.auth, "X-CuePorch-Team": "1"},
                                          json=dict(email="replay-viewer@example.com", role="viewer"))
            self.assertEqual(201, invitation.status_code, invitation.get_data(as_text=True))
            code = invitation.get_json()["path"].split("#", 1)[1]
            viewer = browser.new_page()
            joined = viewer.request.post(self.base + "/api/team/join", headers={"X-CuePorch-Team": "1"},
                                         data=dict(invitation=code, name="Viewer", password="viewer-password"))
            self.assertEqual(201, joined.status, joined.text())
            watch = self.base + event["watch"]
            first_watch = f"{watch}?occurrence={first}"
            self.assertEqual(404, producer.request.get(first_watch).status)
            viewer.goto(first_watch)
            expect(viewer.locator("#eventStatus")).to_have_text("Replay")
            playlist = viewer.get_by_label("Choose a broadcast")
            expect(playlist.locator("option:not([disabled])")).to_have_count(1)
            playback = viewer.locator("#eventPlayer video")
            playback.evaluate("async video => { video.muted = true; await video.play(); }")
            viewer.wait_for_function("""() => {
                const video = document.querySelector('#eventPlayer video');
                return video.videoWidth === 320 && video.currentTime > .2;
            }""")
            media = playback.evaluate("video => video.currentSrc")
            self.assertTrue(media.endswith("/replay.mp4"), media)
            ranged = viewer.request.get(media, headers={"Range": "bytes=0-31"})
            self.assertEqual(206, ranged.status)
            self.assertEqual(32, len(ranged.body()))
            self.assertIn("no-store", ranged.headers["cache-control"])
            self.assertEqual(404, producer.request.get(media).status)

            decision = self.client.post(f"/api/actions/{gate}/decision", headers=self.auth, json=dict(decision="approve"))
            self.assertEqual(204, decision.status_code, decision.get_data(as_text=True))
            expect(playlist.locator("option:not([disabled])")).to_have_count(2, timeout=15000)
            playlist.select_option(str(second))
            viewer.wait_for_function("(opens) => new URL(location.href).searchParams.get('occurrence') === opens",
                                     arg=str(second))
            expect(viewer.locator("#eventStatus")).to_have_text("Replay", timeout=15000)
            viewer.wait_for_function("""() => {
                const video = document.querySelector('#eventPlayer video');
                return video && video.currentSrc;
            }""")
            second_media = viewer.locator("#eventPlayer video").evaluate("video => video.currentSrc")
            trashed = self.client.post("/api/library/recently-deleted", headers=self.auth,
                                       json=dict(kind="work", items=[council]))
            self.assertEqual(202, trashed.status_code, trashed.get_data(as_text=True))
            self.assertEqual(404, viewer.request.get(second_media).status)
            expect(viewer.locator("#eventStatus")).to_have_text("The replay is unavailable", timeout=15000)
            expect(viewer.locator("#eventPlayer video")).to_have_count(0)

            producer.get_by_role("button", name="Withdraw this occurrence").click()
            expect(producer.locator("#eventsStatus")).to_have_text("Occurrence withdrawn.")
            self.assertEqual(404, viewer.request.get(media).status)
            library = self.client.get("/api/library", headers=self.auth).get_json()
            self.assertIn(archive, {item["id"] for item in library})

    def test_migrated_creator_profile_starts_private(self):
        self.source.profile_fixture = True
        self.assertEqual(403, self.client.get("/api/migration/profile").status_code)
        invitation = self.client.post("/api/team/invitations", headers={**self.auth, "X-CuePorch-Team": "1"},
                                      json=dict(email="profile-admin@example.com", role="admin"))
        self.assertEqual(201, invitation.status_code, invitation.get_data(as_text=True))
        code = invitation.get_json()["path"].split("#", 1)[1]
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            joined = page.request.post(self.base + "/api/team/join", headers={"X-CuePorch-Team": "1"},
                                       data=dict(invitation=code, name="Existing member", password="admin-password"))
            self.assertEqual(201, joined.status, joined.text())
            page.goto(self.base + "/operator/migration")
            page.locator("#key").fill(self.operator)
            page.get_by_role("button", name="Preview transfer").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)

            result = self.client.get("/api/migration", headers=self.auth).get_json()
            self.assertIs(True, result["profile_available"])
            self.assertNotIn("source_profile", result)
            self.assertEqual(403, self.client.get("/api/migration/profile").status_code)
            draft_url = self.base + "/api/migration/profile"
            team_session = {"Authorization": "Bearer team-session"}
            profile = page.request.get(draft_url, headers=team_session)
            self.assertEqual(200, profile.status, profile.text())
            self.assertIn("no-store", profile.headers["cache-control"])
            biography = "Films and exhibitions. <em>Archive</em>"
            website = "https://gallery.example.org/films"
            expected = dict(name="Gallery Archive", bio=biography, links=[website])
            self.assertEqual(expected, profile.json())

            page.get_by_role("link", name="Set up creator profile").click()
            form = page.locator("#creatorForm")
            expect(form.locator('[name="name"]')).to_have_value("Gallery Archive")
            expect(form.locator('[name="bio"]')).to_have_value(biography)
            expect(form.locator('[name="links"]')).to_have_value(website)
            expect(form.locator('[name="handle"]')).to_have_value("")
            expect(form.locator('[name="visible"]')).not_to_be_checked()
            api = self.base + "/api/team/profile"
            self.assertEqual("", page.request.get(api).json()["revision"])
            form.locator('[name="handle"]').fill("gallery-archive")
            save = page.get_by_role("button", name="Save creator profile")
            save.click()
            expect(page.locator("#creatorStatus")).to_have_text("Creator profile saved.")
            self.assertEqual(404, self.client.get("/creators/gallery-archive").status_code)
            self.assertIs(False, page.request.get(api).json()["visible"])

            form.locator('[name="bio"]').fill("Local edits")
            save.click()
            expect(page.locator("#creatorStatus")).to_have_text("Creator profile saved.")
            page.goto(self.base + "/team/profile?fromVimeo=1")
            expect(form.locator('[name="bio"]')).to_have_value("Local edits")
            expect(page.locator("#creatorStatus")).to_have_text("Your existing creator profile was left as saved.")

    def test_vimeo_schedule_reconstruction(self):
        self.source.live_event_fixture = True
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.get_by_role("button", name="Preview transfer").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            unsupported = page.locator("details").filter(has=page.get_by_text("No past streams", exact=False))
            unsupported.locator("summary").click()
            expect(unsupported).to_contain_text("Recreate the Vimeo schedule in Live Events.")
            expect(unsupported.get_by_role("link", name="Set up in Live Events")).to_have_count(0)
            source = page.locator("details").filter(has=page.get_by_text("Council replay archive", exact=False))
            source.locator("summary").click()
            expect(source).to_contain_text("Vimeo reports required registration.")
            source.get_by_role("link", name="Set up in Live Events").click()
            form = page.locator("#newEventForm")
            for name, value in dict(title="Council replay archive", description="Recurring council meeting",
                                    timezone="America/Denver", start="2030-01-07T10:00", end="2030-01-07T11:00",
                                    repeat="weeks", interval="2", count="4", visibility="private", embed_mode="disabled",
                                    latency="low", method="encoder").items():
                expect(form.locator(f'[name="{name}"]')).to_have_value(value)
            expect(form.locator('[name="chat"]')).to_be_checked()
            expect(form.locator('[name="captions"]')).to_be_checked()
            expect(form.locator('[name="registration"]')).not_to_be_checked()
            expect(form.locator('[name="dvr_seconds"]')).to_have_value("0")
            expect(form.locator('[name="caption_language"]')).to_have_value("fr")
            self.assertEqual([], self.client.get("/api/live-events", headers=self.auth).get_json()["events"])
            with page.expect_response(lambda response: response.request.method == "POST"
                                      and response.url.endswith("/api/live-events")) as saved:
                form.get_by_role("button", name="Save event", exact=True).click()
            self.assertEqual(201, saved.value.status)
            calendar = self.client.get("/api/live-events?from=2030-01-01&to=2030-01-31", headers=self.auth).get_json()
            event, = calendar["events"]
            settings = event["event"]
            self.assertIs(True, settings["setup"]["chat"]["enabled"])
            self.assertEqual(dict(enabled=True, language="fr"), settings["setup"]["captions"])
            self.assertEqual(0, settings["dvr_seconds"])
            registration = self.client.get(f"/api/live-events/{event['id']}/registration", headers=self.auth).get_json()
            self.assertIs(False, registration["settings"]["enabled"])
            self.assertEqual("private", event["event"]["visibility"])
            self.assertEqual("disabled", event["event"]["embed_policy"]["mode"])
            self.assertEqual([0, 2], event["event"]["schedule"]["weekdays"])
            self.assertEqual([f"2030-01-{date}T17:00:00+00:00" for date in ("07", "09", "21", "23")],
                             [row["start"] for row in event["occurrences"]])
            self.assertIsNone(event["embed"])
            self.assertEqual(404, self.client.get(event["watch"]).status_code)

    def test_preserves_folders_and_local_changes(self):
        local = self.client.post("/api/library/folders", headers=self.auth,
                                 json={"name": "Foundation archive", "color": "#6789ab"})
        self.assertEqual(201, local.status_code, local.get_data(as_text=True))
        local_id = local.get_json()["id"]
        gallery_work, archive_work, unfiled_work = (work_id("station", "vimeo", uid) for uid in ("71", "72", "73"))

        def folders():
            return {row["id"]: row for row in self.client.get("/api/library/folders", headers=self.auth).get_json()}

        def placements():
            return {row["id"]: row["folder"] for row in self.client.get("/api/library", headers=self.auth).get_json()}

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.get_by_role("button", name="Preview transfer").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            expect(page.locator("#rows tr").filter(has=page.get_by_role("cell", name="71", exact=True))).to_contain_text(
                "Foundation archive / Gallery edits",
            )
            page.get_by_text("3 source folders", exact=True).click()
            expect(page.get_by_text("Foundation archive / Empty vault", exact=True)).to_be_visible()
            self.assertEqual({local_id}, set(folders()), "Preview must leave the Library as it was.")

            transfer = page.get_by_role("button", name="Transfer privately")
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=15000)
            tree, assigned = folders(), placements()
            gallery, archive = assigned[gallery_work], assigned[archive_work]
            self.assertEqual(4, len(tree))
            self.assertNotEqual(local_id, archive)
            self.assertEqual(("Foundation archive", None), (tree[archive]["name"], tree[archive]["parent"]))
            self.assertEqual(("Gallery edits", "#445566", archive),
                             (tree[gallery]["name"], tree[gallery]["color"], tree[gallery]["parent"]))
            empty = next(row for row in tree.values() if row["name"] == "Empty vault")
            self.assertEqual(archive, empty["parent"])
            self.assertNotIn(empty["id"], assigned.values())
            self.assertIsNone(assigned[unfiled_work])

            viewer = browser.new_page()
            self.assertEqual(404, viewer.request.get(self.base + f"/watch/work/{gallery_work}").status)
            review = self.client.post(f"/api/library/work/{gallery_work}/review-links", headers=self.auth,
                                      json={"name": "Migrated gallery cut"})
            self.assertEqual(201, review.status_code, review.get_data(as_text=True))
            viewer.goto(self.base + review.get_json()["path"])
            video = viewer.locator("#reviewTourPlayer")
            expect(video).to_be_visible()
            video.evaluate("async video => { video.muted = true; await video.play(); }")
            viewer.wait_for_function("""() => {
                const video = document.querySelector('#reviewTourPlayer');
                return video.videoWidth === 320 && video.currentTime > .2;
            }""")

            moved = self.client.put("/api/library/placement", headers=self.auth,
                                    json={"works": [gallery_work], "folder": None})
            self.assertEqual(204, moved.status_code, moved.get_data(as_text=True))
            renamed = self.client.put(f"/api/library/folders/{archive}", headers=self.auth,
                                      json={"name": "Local archive", "color": "#123456", "parent": None})
            self.assertEqual(200, renamed.status_code, renamed.get_data(as_text=True))
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=15000)
            after = folders()
            self.assertEqual(set(tree), set(after))
            self.assertEqual(("Local archive", "#123456"), (after[archive]["name"], after[archive]["color"]))
            self.assertEqual("Foundation archive", after[local_id]["name"])
            self.assertEqual(assigned | {gallery_work: None}, placements())
