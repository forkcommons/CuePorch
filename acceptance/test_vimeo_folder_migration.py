"""Verify the Vimeo folder transfer through the operator and Library screens."""

import json
import secrets
import unittest
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
from vimeo_transfer import SourceFailure, event_schedule_draft, folder_inventory, resolve_archive_recordings


class VimeoFolderInventory(unittest.TestCase):
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
            source.get_by_role("link", name="Set up in Live Events").click()
            form = page.locator("#newEventForm")
            for name, value in dict(title="Council replay archive", description="Recurring council meeting",
                                    timezone="America/Denver", start="2030-01-07T10:00", end="2030-01-07T11:00",
                                    repeat="weeks", interval="2", count="4", visibility="private", embed_mode="disabled",
                                    latency="low", method="encoder").items():
                expect(form.locator(f'[name="{name}"]')).to_have_value(value)
            self.assertEqual([], self.client.get("/api/live-events", headers=self.auth).get_json()["events"])
            with page.expect_response(lambda response: response.request.method == "POST"
                                      and response.url.endswith("/api/live-events")) as saved:
                form.get_by_role("button", name="Save event", exact=True).click()
            self.assertEqual(201, saved.value.status)
            calendar = self.client.get("/api/live-events?from=2030-01-01&to=2030-01-31", headers=self.auth).get_json()
            event, = calendar["events"]
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
