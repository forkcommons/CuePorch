import csv
from datetime import datetime, timezone
import io
import json
import secrets
import sqlite3
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from flask import Response, request, send_file
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from installed_versions import generate_replacement
from institution import create_app
from library_analytics import record
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

    def test_date_report_utc_buckets_and_csv(self):
        wid = self.ids[0]
        sessions = [
            (9001, datetime(2025, 12, 28, 12, tzinfo=timezone.utc).timestamp(), 0),
            (9002, datetime(2025, 12, 29, 12, tzinfo=timezone.utc).timestamp(), 1),
            (9003, datetime(2025, 12, 29, 13, tzinfo=timezone.utc).timestamp(), 2),
            (9004, datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp(), 3),
        ]
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executemany(
                "INSERT INTO video_analytics_session VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(sid, wid, "fixture-cut", "watch", ts, seq, 1, 1, 1000, "[]") for sid, ts, seq in sessions],
            )
            conn.executemany(
                "INSERT INTO video_analytics_viewer VALUES(?,?)",
                [(sid, f"v-{sid}") for sid, _, _ in sessions],
            )
            conn.commit()
        expected = {
            "day": [("2025-12-28", 1), ("2025-12-29", 2), ("2026-01-01", 1)],
            "week": [("2025-12-22", 1), ("2025-12-29", 3)],
            "month": [("2025-12-01", 3), ("2026-01-01", 1)],
            "year": [("2025-01-01", 3), ("2026-01-01", 1)],
        }
        for period, buckets in expected.items():
            opts = {"from": "2025-12-28", "to": "2026-01-01", "period": period, "videos": [wid]}
            result = self.client.post("/api/analytics/date-report", headers=self.auth, json=opts).get_json()
            self.assertEqual(buckets, [(r["category"], r["views"]) for r in result["rows"]])
            totals = result["totals"]
            self.assertEqual((4, 4, 4, 4, 1), tuple(totals[k] for k in ("views", "impressions", "unique_viewers", "watched_seconds", "average_watched_seconds")))
            text = self.client.post("/api/analytics/date-report?format=csv", headers=self.auth, json=opts).get_data(as_text=True)
            csv_rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(buckets, [(r["date"], int(r["views"])) for r in csv_rows])

    def test_engagement_counts_replayed_passages_and_skips_seek_gaps(self):
        work = {"id": self.ids[0], "title": "Repeat", "duration_seconds": 8, "files": []}
        self.manifest.write_text(
            json.dumps({"institution": "station", "schema": 1, "works": [work]}), encoding="utf-8",
        )
        media = generate_replacement(self.root / "retention.mp4", width=320, seconds=8)

        @self.app.get("/fixture-media")
        def fixture_media():
            return send_file(media, conditional=True)

        @self.app.get("/fixture-player")
        def fixture_player():
            return Response(
                '<video data-analytics="/fixture-observe" src="/fixture-media" muted></video>'
                '<script src="/static/ui/playback_analytics.js" type="module"></script>',
                mimetype="text/html",
            )

        @self.app.post("/fixture-observe")
        def fixture_observe():
            with closing(sqlite3.connect(self.db)) as connection, connection:
                record(connection, work, "repeat-session", "watch", request.get_json())
            return "", 204

        server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=QuietHandler)
        self.enterContext(service_worker(server))
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()

            def observation(response, *, finished):
                return (
                    response.url.endswith("/fixture-observe")
                    and response.request.method == "POST"
                    and response.request.post_data_json["completed"] == finished
                    and response.request.post_data_json["watched"] > 0
                )

            page.goto(f"http://127.0.0.1:{server.server_port}/fixture-player")
            video = page.locator("video")
            page.wait_for_function("document.querySelector('video').videoWidth === 320", timeout=5000)

            def seek(seconds):
                video.evaluate("""(video, seconds) => new Promise(resolve => {
                    video.addEventListener("seeked", resolve, {once: true});
                    video.currentTime = seconds;
                })""", seconds)

            def play_and_pause():
                video.evaluate("video => video.play()")
                page.wait_for_function("document.querySelector('video').currentTime >= 2.5", timeout=5000)
                with page.expect_response(lambda response: observation(response, finished=False)) as response:
                    video.evaluate("video => video.pause()")
                self.assertEqual(204, response.value.status)

            play_and_pause()
            seek(1)
            play_and_pause()
            seek(5.5)
            with page.expect_response(lambda response: observation(response, finished=True)) as response:
                video.evaluate("video => video.play()")
            self.assertEqual(204, response.value.status)
            self.assertTrue(video.evaluate("video => video.ended"))

        response = self.client.post("/api/analytics/engagement-report", headers=self.auth, json={"videos": [work["id"]]})
        self.assertEqual(200, response.status_code)
        result = response.get_json()
        totals = result["totals"]
        self.assertEqual((1, 1, 1), (totals["views"], totals["measured"], totals["finishes"]))
        self.assertEqual((2, 0, 1), tuple(totals["points"][str(point)] for point in (25, 50, 100)))
        self.assertEqual((2, 0, 1), tuple(result["retention"][point] for point in (25, 50, 100)))

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
