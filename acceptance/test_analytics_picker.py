from datetime import datetime, timezone
import json
import secrets
import sqlite3
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from institution import create_app
from test_drive_import import QuietHandler, service_worker


class AnalyticsPicker(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        (self.root / "objects").mkdir()

        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps({"institution": "station", "schema": 1, "works": []}),
            encoding="utf-8",
        )

        self.db = self.root / "workspace.sqlite"
        self.operator = secrets.token_urlsafe(32)
        self.auth = {"Authorization": f"Bearer {self.operator}"}

        self.enterContext(
            patch.dict(
                "os.environ",
                {
                    "CUEPORCH_PUBLIC_URL": "",
                    "CUEPORCH_PEERTUBE_ORIGIN": "",
                },
            )
        )
        with patch("institution.library_worker.start", return_value=lambda: None):
            self.app = create_app(self.root, self.db, self.operator, "station")
        for name, stop in self.app.extensions.items():
            if name.startswith("stop_") and callable(stop):
                self.addCleanup(stop)

        self.client = self.app.test_client()

        self.ids = [str(uuid.UUID(int=i + 1)) for i in range(10002)]

        # Catalog metadata is enough to exercise the picker at scale.
        works = [
            {"id": wid, "title": f"Fixture {i:05d}"}
            for i, wid in enumerate(self.ids)
        ]
        self.manifest.write_text(
            json.dumps(
                {"institution": "station", "schema": 1, "works": works}
            ),
            encoding="utf-8",
        )
        now = datetime.now(timezone.utc).timestamp()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute(
                "INSERT INTO library_folder (id, parent, name, color) VALUES (?, ?, ?, ?)",
                ("fixture-parent", None, "Festival archive", "#3266aa"),
            )
            con.executemany(
                "INSERT INTO library_folder (id, parent, name, color) VALUES (?, ?, ?, ?)",
                [
                    ("fixture-child", "fixture-parent", "Screenings", "#3266aa"),
                    ("fixture-overflow", "fixture-parent", "Extras", "#3266aa"),
                ],
            )
            con.executemany(
                "INSERT INTO library_placement (work, folder) VALUES (?, ?)",
                [(work, "fixture-child") for work in self.ids[:10000]]
                + [(work, "fixture-overflow") for work in self.ids[10000:]],
            )
            con.executemany(
                "INSERT INTO video_analytics_session "
                "(id, work, version, surface, created, sequence, played, completed, watched, intervals) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "fixture-session-1",
                        self.ids[9999],
                        "fixture-cut",
                        "watch",
                        now,
                        1,
                        1,
                        0,
                        1500,
                        "[[0,1500]]",
                    ),
                    (
                        "fixture-session-2",
                        self.ids[10000],
                        "fixture-cut",
                        "watch",
                        now,
                        1,
                        1,
                        0,
                        1500,
                        "[[0,1500]]",
                    ),
                ],
            )

    def test_operator_analytics_report_folder_limit(self):
        server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=QuietHandler)
        self.enterContext(service_worker(server))
        base = f"http://127.0.0.1:{server.server_port}"
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(base + "/operator/analytics")
            page.locator("#reportSelection").click()
            page.select_option("#reportFolder", "fixture-parent")
            expect(page.locator("#reportFolderCount")).to_have_text("10002 videos")
            page.locator("#reportFolderAdd").click()
            expect(page.locator("#reportStatus")).to_have_text("Choose up to 10,000 items.")
            expect(page.locator("#reportSelection")).to_have_text("All videos and events")
            page.select_option("#reportFolder", "fixture-child")
            expect(page.locator("#reportFolderCount")).to_have_text("10000 videos")
            page.click("#reportFolderAdd")
            expect(page.locator("#reportSelection")).to_have_text("10,000 selected")

            page.fill("#reportSearch", "Fixture 09999")
            expect(page.locator("#reportSelection")).to_have_text("10,000 selected")

            with page.expect_response(
                lambda r: r.request.method == "POST"
                and r.url.endswith("/api/analytics/video-report")
            ) as info:
                page.get_by_role("button", name="Apply filters", exact=True).click()

            assert info.value.status == 200
            result = info.value.json()
            filters = result["filters"]
            videos = filters["videos"]
            assert len(videos) == 10000
            assert self.ids[9999] in videos
            assert self.ids[10000] not in videos
            assert result["totals"]["views"] == 1

            page.select_option("#reportFolder", "fixture-overflow")
            page.click("#reportFolderAdd")
            expect(page.locator("#reportSelection")).to_have_text("10,000 selected")
            expect(page.locator("#reportStatus")).to_have_text("Choose up to 10,000 items.")
            page.select_option("#reportFolder", "fixture-child")
            page.click("#reportFolderRemove")
            expect(page.locator("#reportSelection")).to_have_text("All videos and events")
            assert self.client.get("/api/analytics/videos?folders=1").status_code == 403
            assert isinstance(self.client.get("/api/analytics/videos", headers=self.auth).get_json(), list)
