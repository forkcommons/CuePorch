import csv
from datetime import datetime, timedelta, timezone
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

    def test_team_analytics_reports(self):
        now = datetime.now(timezone.utc).timestamp()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany(
                "INSERT INTO team_member (id,email,name,role,password_hash,joined,removed) VALUES (?,?,?,?,?,?,?)",
                [
                    ("alice", "a@example.test", "Alice", "viewer", "test", now, None),
                    ("bob", "b@example.test", "Bob", "viewer", "test", now, now),
                ],
            )
            conn.execute('INSERT INTO team_analytics_setting VALUES(1, "fixture-cycle")')
            for sid, watched, interval in (
                ("a1", 6000, [0, 6000]),
                ("a2", 7000, [3000, 10000]),
                ("b1", 10000, [0, 10000]),
            ):
                conn.execute(
                    "INSERT INTO video_analytics_session (id,work,version,surface,created,sequence,played,completed,watched,intervals) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, self.ids[0], "cut", "watch", now, 1, 1, 0, watched, json.dumps([interval])),
                )
            conn.executemany(
                "INSERT INTO team_analytics_view (session,member,duration_ms) VALUES (?,?,?)",
                [("a1", "alice", 10000), ("a2", "alice", 10000), ("b1", "bob", 10000)],
            )

        member_report = self.client.get("/api/team/analytics?group=member", headers=self.auth)
        self.assertEqual(200, member_report.status_code)
        members = {row["id"]: row for row in member_report.get_json()["summary"]}
        alice = members["alice"]
        assert alice["net_seconds"] == 10
        self.assertAlmostEqual(.65, alice["avg_watched_fraction"])
        assert alice["completions"] == 0
        bob = members["bob"]
        assert bob["removed"] == now
        assert bob["completions"] == 1

        videos = self.client.get("/api/team/analytics?group=video", headers=self.auth).get_json()["summary"]
        assert len(videos) == 1
        assert videos[0]["viewers"] == 2
        assert videos[0]["net_seconds"] == 20
        assert videos[0]["views"] == 3

        pair = self.client.get("/api/team/analytics?member=alice", headers=self.auth).get_json()["rows"]
        self.assertEqual(1, len(pair))
        self.assertEqual(13, pair[0]["watched_seconds"])

        server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=QuietHandler)
        self.enterContext(service_worker(server))
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.set_viewport_size({"width": 1280, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_port}/team/analytics")
            rows = page.locator("#teamAnalyticsRows tr")
            page.locator("#teamAnalyticsGroup").select_option("video")
            expect(rows).to_have_count(1)
            expect(rows).to_contain_text("Fixture 00000")
            expect(page.locator("#teamAnalyticsTotals")).to_contain_text("3")
            page.locator("#teamAnalyticsGroup").select_option("member")
            expect(rows).to_have_count(2)
            expect(rows.get_by_role("button", name="Alice", exact=True)).to_be_visible()
            expect(rows.get_by_role("button", name="Bob (removed)", exact=True)).to_be_visible()
            expect(page.locator("#teamAnalyticsTotals")).to_contain_text("3")
            page.locator("#teamAnalyticsRows").get_by_role("button", name="Alice").click()
            expect(rows).to_have_count(1)
            expect(rows).to_contain_text("Fixture 00000")
            expect(rows).to_contain_text("100%")
            with page.expect_download() as dl:
                page.get_by_role("button", name="Download CSV").click()
            reader = csv.DictReader(io.StringIO(dl.value.path().read_text(encoding="utf-8")))
            assert reader.fieldnames and "Email" in reader.fieldnames
            detail = list(reader)
            self.assertEqual(1, len(detail))
            self.assertEqual("a@example.test", detail[0]["Email"])
            self.assertEqual("100.00%", detail[0]["Net percent completed"])

    def test_team_analytics_excludes_deleted_and_expired_views(self):
        current = datetime.now(timezone.utc)
        expired = current - timedelta(days=366 * 5 + 1)
        now, stale = current.timestamp(), expired.timestamp()
        member, ok, gone = "m1", self.ids[0], self.ids[1]
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO team_member VALUES(?,?,?,?,?,?,?)",
                         (member, "a@b.c", "A", "member", "x", now, None))
            conn.execute('INSERT INTO team_analytics_setting VALUES(1,"a")')
            conn.executemany(
                "INSERT INTO video_analytics_session VALUES(?,?,?,?,?,?,?,?,?,?)",
                [("s1", ok, 1, "watch", now - 100, 1, 1, 0, 95000, "[[0,95000]]"),
                 ("s2", ok, 2, "watch", now - 200, 1, 1, 1, 96000, "[[0,96000]]"),
                 ("s3", ok, 3, "watch", stale, 1, 1, 1, 100000, "[[0,100000]]"),
                 ("s4", gone, 1, "watch", now - 50, 1, 1, 1, 100000, "[[0,100000]]")])
            conn.executemany("INSERT INTO team_analytics_view VALUES(?,?,?)",
                             [("s1", member, 100000), ("s2", member, 100000),
                              ("s3", member, 100000), ("s4", member, 100000)])
            conn.execute("INSERT INTO library_deletion VALUES(?,?,?,?,?,?,?,?,?)",
                         ("d1", "work", gone, "T", "actor", now, "done", None, now))
            conn.execute("INSERT INTO library_deleted_item VALUES(?,?,?)",
                         ("work", gone, "d1"))
        r = self.client.get(f"/api/team/analytics?group=video&from={expired.date().isoformat()}", headers=self.auth)
        assert r.status_code == 200
        rows = r.get_json()["rows"]
        self.assertEqual([ok], [row["work"] for row in rows])
        row = rows[0]
        assert row["views"] == 2
        assert row["completions"] == 1
        self.assertEqual(191, row["net_seconds"])
        self.assertAlmostEqual(.955, row["net_fraction"])
        self.assertAlmostEqual(.955, row["avg_watched_fraction"])

    def test_bandwidth_counts_authorized_team_media_range_only(self):
        self.manifest.write_text(json.dumps({"institution": "station", "schema": 1, "works": []}), encoding="utf-8")
        clip = generate_replacement(self.root / "team-bandwidth.mp4", width=320)
        with clip.open("rb") as stream:
            upload = self.client.post("/api/library/uploads", headers=self.auth, data={
                "file": (stream, "fixture.mp4"), "upload_id": uuid.uuid4().hex,
            })
        self.assertEqual(201, upload.status_code, upload.get_data(as_text=True))
        work = upload.get_json()["id"]
        team = {"X-CuePorch-Team": "1"}
        manager = self.auth | team
        invite = self.client.post("/api/team/invitations", headers=manager, json={
            "email": "bandwidth@example.test", "role": "viewer",
        })
        self.assertEqual(201, invite.status_code)
        viewer = self.app.test_client()
        joined = viewer.post("/api/team/join", headers=team, json={
            "invitation": invite.get_json()["path"].split("#", 1)[1],
            "name": "Meter test", "password": "fixture-long-password",
        })
        self.assertEqual(201, joined.status_code)
        grant = f"/api/team/members/{joined.get_json()['id']}/works/{work}"
        media = f"/team/video/{work}/media"
        self.assertEqual(204, self.client.put(grant, headers=manager, json={}).status_code)
        with self.app.test_client().get(media) as response:
            self.assertEqual(401, response.status_code)
        with viewer.head(media) as response:
            self.assertEqual(200, response.status_code)
        with viewer.get(media, headers={"Range": "bytes=999999999-"}) as response:
            self.assertEqual(416, response.status_code)
        with viewer.get(media, headers={"Range": "bytes=20-99"}, buffered=True) as response:
            self.assertEqual(206, response.status_code)
            self.assertEqual(80, len(response.data))
        self.assertEqual(204, self.client.delete(grant, headers=manager, json={}).status_code)
        with viewer.get(media, headers={"Range": "bytes=20-99"}) as response:
            self.assertEqual(404, response.status_code)
        report = self.client.post("/api/analytics/bandwidth-report", headers=self.auth, json={
            "videos": [work], "bandwidth_group": "video",
        })
        self.assertEqual(200, report.status_code)
        self.assertEqual({"live_bytes": 0, "vod_bytes": 80, "total_bytes": 80}, report.get_json()["totals"])

    def test_report_comparison(self):
        first, second = self.ids[:2]
        title = "Shared fixture title"
        self.manifest.write_text(json.dumps({"institution": "station", "schema": 1, "works": [
            {"id": uid, "title": title} for uid in (first, second)
        ]}), encoding="utf-8")
        sessions = [
            ("cmp-first-jan1", first, 1, "Phone", "US-CO"),
            ("cmp-first-jan2", first, 2, "Desktop", "US-NY"),
            ("cmp-second-jan2", second, 2, "Phone", "US-CO"),
        ]
        with closing(sqlite3.connect(self.db)) as con, con:
            con.executemany(
                "INSERT INTO video_analytics_session VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(sid, uid, "fixture-cut", "watch", datetime(2026, 1, day, 12, tzinfo=timezone.utc).timestamp(),
                  1, 1, 0, 1000, "[]") for sid, uid, day, _, _ in sessions],
            )
            con.executemany("INSERT INTO video_analytics_context VALUES(?,?,?)", [
                (sid, "https://arts.example" if device == "Phone" else "https://station.example", device)
                for sid, _, _, device, _ in sessions
            ])
            con.executemany("INSERT INTO video_analytics_region VALUES(?,?)",
                            [(sid, region) for sid, _, _, _, region in sessions])

        dates = {"from": "2026-01-01", "to": "2026-01-02"}
        for kind, key, expected in (
            ("video", [first, ""], [("2026-01-01", 1), ("2026-01-02", 1)]),
            ("video", [second, ""], [("2026-01-02", 1)]),
            ("device", ["Phone", ""], [("2026-01-01", 1), ("2026-01-02", 1)]),
            ("region", ["US-NY", ""], [("2026-01-02", 1)]),
            ("source", ["https://arts.example", ""], [("2026-01-01", 1), ("2026-01-02", 1)]),
        ):
            with self.subTest(kind=kind, key=key):
                response = self.client.post(f"/api/analytics/{kind}-report", json=dates | {"compare": [key]},
                                            headers=self.auth)
                self.assertEqual(200, response.status_code, response.get_data(as_text=True))
                data = response.get_json()
                self.assertEqual(3, data["totals"]["views"])
                self.assertEqual(key, data["comparison"][0]["key"])
                self.assertEqual(expected, [(row["period"], row["views"]) for row in data["comparison"][0]["overview"]])
        path = "/api/analytics/video-report"
        self.assertEqual(403, self.client.post(path, json=dates | {"compare": [[first, ""]]}).status_code)
        for invalid in (None, [[first, ""]] * 2, [[first, ""]] * 11, [[self.ids[2], ""]]):
            self.assertEqual(422, self.client.post(path, json=dates | {"compare": invalid},
                                                   headers=self.auth).status_code)
        filtered = self.client.post(path, headers=self.auth,
                                    json=dates | {"videos": [first], "compare": [[second, ""]]})
        self.assertEqual(422, filtered.status_code)
        paired = self.client.post(path, headers=self.auth, json=dates | {
            "breakdown": "device", "compare": [[first, "Desktop"], [first, "Phone"]],
        }).get_json()
        self.assertEqual([["2026-01-02"], ["2026-01-01"]], [
            [bucket["period"] for bucket in item["overview"]] for item in paired["comparison"]
        ])

        server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=QuietHandler)
        self.enterContext(service_worker(server))
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(f"http://127.0.0.1:{server.server_port}/operator/analytics")
            page.fill("[name=from]", dates["from"])
            page.fill("[name=to]", dates["to"])
            page.get_by_role("button", name="Apply filters", exact=True).click()
            rows = page.locator("#reportRows input[type=checkbox]")
            rows.nth(0).check()
            rows.nth(1).check()

            expect(page.locator("#trendColumns th")).to_have_text(["UTC date", "Total", title, title])
            expect(page.locator("#reportTrend tr").nth(0).locator("td")).to_have_text(["2026-01-01", "1", "1", "—"])
            expect(page.locator("#reportTrend tr").nth(1).locator("td")).to_have_text(["2026-01-02", "2", "1", "1"])

            page.locator("#trendTotal").uncheck()
            expect(page.locator("#trendColumns th")).to_have_text(["UTC date", title, title])

            rows.nth(0).uncheck()
            rows.nth(1).uncheck()
            expect(page.locator("#reportTrend tr")).to_have_count(0)

            page.locator("#trendTotal").check()
            expect(page.locator("#reportTrend tr")).to_have_count(2)

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
