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
from vimeo_transfer import SourceFailure, folder_inventory


class VimeoFolderInventory(unittest.TestCase):
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
