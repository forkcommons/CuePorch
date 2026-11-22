"""Exercise preserved Vimeo cuts in migration, review, and restore."""

import json
import secrets
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from cueporch import work_id
from demo_source import build_server
from installed_audio_tracks import SAMPLE_AUDIO
from institution import create_app
from test_drive_import import QuietHandler, service_worker


class VimeoVersionMigration(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(TemporaryDirectory()))
        home = self.temp / "collection"
        (home / "objects").mkdir(parents=True)
        (home / "manifest.json").write_text(
            json.dumps(dict(institution="station", schema=1, works=[])), encoding="utf8",
        )
        source = self.enterContext(build_server(self.temp / "vimeo", version_fixture=True, audio_fixture=True))
        self.source = source
        self.requests = Counter()
        self.denied = set()
        handler = source.RequestHandlerClass
        original_get = handler.do_GET

        def counters(request):
            path = urlsplit(request.path).path
            self.requests[path] += 1
            if path in self.denied:
                return request.send_error(403)
            return original_get(request)

        self.enterContext(patch.object(handler, "do_GET", counters))
        self.enterContext(service_worker(source))
        self.operator = secrets.token_urlsafe(32)
        self.auth = {"Authorization": "Bearer " + self.operator}
        self.enterContext(patch.dict("os.environ", {"CUEPORCH_PUBLIC_URL": "", "CUEPORCH_PEERTUBE_ORIGIN": ""}))
        with patch("institution.library_worker.start", return_value=lambda: None):
            app = create_app(home, self.temp / "workspace.sqlite", self.operator, "station",
                             simulation=f"http://127.0.0.1:{source.server_port}")
        for name, stop in app.extensions.items():
            if name.startswith("stop_") and callable(stop):
                self.addCleanup(stop)
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        self.enterContext(service_worker(server))
        self.client = app.test_client()
        self.base = f"http://127.0.0.1:{server.server_port}"

    def test_vimeo_embed_appearance_import(self):
        self.source.public_viewer_fixture = True
        self.source.video_embed_fixture = dict(
            colors=dict(color_one="112233", color_four="223344"),
            playbar=False, title=dict(name="show"), muted=True, speed=False,
            end_screen=dict(type="loop"),
            html="fake-html-secret", logos=dict(custom=dict(url="https://example.test/?fake-logo-secret")),
        )
        uid = work_id("station", "vimeo", "71")
        root = f"/api/library/work/{uid}"
        path = root + "/appearance/embed"
        expected = dict(primary="#112233", background="#223344", progress=False, show_title=True, muted=True, speed=False)

        def settings():
            response = self.client.get(path, headers=self.auth)
            self.assertEqual(200, response.status_code, response.text)
            return response.json["settings"]

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")

            def transfer():
                page.get_by_role("button", name="Preview transfer", exact=True).click()
                expect(page.locator("#status")).to_have_text("inventoried", timeout=20000)
                page.get_by_role("button", name="Transfer privately", exact=True).click()
                expect(page.locator("#status")).to_have_text("transferred", timeout=20000)
            transfer()
            source = settings()
            self.assertEqual(expected, {key: source[key] for key in expected})
            watch = self.client.get(root + "/appearance/watch", headers=self.auth).json["settings"]
            self.assertTrue(watch["speed"])
            self.assertNotEqual(watch["background"], source["background"])
            self.assertEqual(403, self.client.get(path).status_code)
            self.assertEqual(404, self.client.get(f"/embed/work/{uid}").status_code)
            self.assertEqual(self.client.put(root + "/source-privacy", headers=self.auth, json={}).status_code, 200)
            guest = browser.new_page()
            guest.goto(self.base + "/watch")
            guest.evaluate("""url => {
                const player = document.createElement('iframe');
                player.id = 'migration-test-player';
                player.src = url;
                document.body.append(player);
            }""", self.base + f"/embed/work/{uid}")
            frame = guest.frame_locator("#migration-test-player")
            expect(frame.get_by_text("Station meeting", exact=True)).to_be_visible()
            expect(frame.get_by_role("combobox", name="Speed", exact=True)).to_have_count(0)
            played = frame.locator("video").evaluate("""async video => {
                await video.play();
                return video.muted && video.videoWidth > 0 && video.loop;
            }""")
            self.assertTrue(played)
            loop_page = browser.new_page()
            loop_page.goto(self.base + f"/watch/work/{uid}")
            self.assertFalse(loop_page.locator("video").evaluate("video => video.loop"))

            local = source | {"speed": True}
            self.assertEqual(200, self.client.put(path, headers=self.auth, json=local).status_code)
            self.source.video_embed_fixture["colors"]["color_four"] = "99AABB"
            self.source.video_embed_fixture["end_screen"] = {"type":"empty"}
            transfer()
            self.assertEqual(local, settings())
            self.assertEqual(200, self.client.delete(path, headers=self.auth).status_code)
            self.assertEqual(source | {"background": "#99aabb"}, settings())
            self.source.video_embed_fixture = None
            transfer()
            self.assertEqual(source | {"background": "#99aabb"}, settings())
            migration_text = self.client.get("/api/migration", headers=self.auth).text
            self.assertNotIn("fake-html-secret", migration_text)
            self.assertNotIn("fake-logo-secret", migration_text)
            guest.locator("#migration-test-player").evaluate("(element) => { element.src = element.src; }")
            expect(frame.locator('[data-ending="empty"]')).to_have_count(1)
            self.assertFalse(frame.locator("video").evaluate("(video) => video.loop"))

            path = root + "/presentation"
            current = self.client.get(path, headers=self.auth).json
            saved = self.client.put(
                path,
                headers=self.auth,
                json=dict(
                    version=current["version"],
                    previous=current["settings"],
                    cards=[],
                    ending={"mode": "loop"},
                ),
            )
            self.assertEqual(200, saved.status_code, saved.text)
            guest.locator("#migration-test-player").evaluate(
                "el => el.contentWindow.location.reload()"
            )
            expect(frame.locator("video")).to_have_attribute("loop", "")

    def test_vimeo_page_appearance_import(self):
        uid = work_id("station", "vimeo", "71")
        root = f"/api/library/work/{uid}"
        watch_path = root + "/appearance/watch"
        embed_path = root + "/appearance/embed"
        self.source.public_viewer_fixture = True
        self.source.video_page_fixture = {
            "title": True,
            "description": False,
            "customization": {
                "background_color": "#123456",
                "ambient_backlight": True,
                "logo": {"custom_logo_url": "https://example.test/secret"},
            },
            "player": {"speed": False, "muted": True, "colors": {"color_one": "339988", "color_four": "ffffff"}},
        }
        expected_watch = {"primary": "#339988", "background": "#123456", "ambient": True, "speed": False, "description": False}

        def transfer(page):
            page.get_by_role("button", name="Preview transfer", exact=True).click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=20000)
            page.get_by_role("button", name="Transfer privately", exact=True).click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=20000)

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            transfer(page)

            watch = self.client.get(watch_path, headers=self.auth).json["settings"]
            self.assertEqual(expected_watch, {key: watch[key] for key in expected_watch})
            self.assertEqual(404, self.client.get(f"/watch/work/{uid}").status_code)
            self.assertEqual(200, self.client.put(root + "/source-privacy", headers=self.auth, json={}).status_code)

            guest = browser.new_page()
            guest.goto(self.base + f"/watch/work/{uid}")
            guest.wait_for_selector("video", timeout=20000)
            self.assertEqual("#123456", guest.evaluate("getComputedStyle(document.body).getPropertyValue('--viewer-background').trim()"))
            self.assertTrue(guest.locator("video").evaluate("async video => { await video.play(); return video.videoWidth > 0; }"))

            self.source.video_page_fixture = None
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            operator.goto(self.base + "/operator/migration")
            transfer(operator)

            watch2 = self.client.get(watch_path, headers=self.auth).json["settings"]
            self.assertEqual(expected_watch, {key: watch2[key] for key in expected_watch})
            embed = self.client.get(embed_path, headers=self.auth).json["settings"]
            self.assertNotEqual("#123456", embed["background"])
            self.assertTrue(embed["speed"])

    def test_vimeo_chapter_picture_import(self):
        self.source.chapter_picture_fixture = True
        self.source.public_viewer_fixture = True

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")

            page.get_by_role("button", name="Preview transfer", exact=True).click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=20000)
            self.assertEqual(0, self.requests["/media/chapter.png"])

            page.get_by_role("button", name="Transfer privately", exact=True).click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=20000)
            self.assertEqual(1, self.requests["/media/chapter.png"])

            uid = work_id("station", "vimeo", "71")
            self.assertEqual(404, self.client.get(f"/watch/work/{uid}").status_code)
            response = self.client.put(
                f"/api/library/work/{uid}/source-privacy",
                headers=self.auth,
                json={},
            )
            self.assertEqual(200, response.status_code)

            guest_context = browser.new_context()
            guest_page = guest_context.new_page()
            guest_page.goto(f"{self.base.rstrip('/')}/watch/work/{uid}")

            picture = guest_page.locator("[data-chapter-picture]")
            expect(picture).to_be_visible()
            self.assertGreater(picture.evaluate("img => img.naturalWidth"), 0)

            picture_src = picture.get_attribute("src")
            picture_url = (
                picture_src
                if picture_src.startswith("http")
                else f"{self.base.rstrip('/')}{picture_src}"
            )
            picture_response = guest_context.request.get(picture_url)
            self.assertEqual("image/png", picture_response.headers["content-type"])

            video = guest_page.locator("video")
            expect(video).to_be_visible()
            self.assertTrue(
                video.evaluate(
                    "async v => { v.muted = true; await v.play(); return v.videoWidth > 0; }"
                )
            )

            digest_url = self.base + picture_src.rsplit('/', 1)[0] + '/' + '0' * 64
            self.assertEqual(404, guest_page.request.get(digest_url).status)
            self.denied.add('/videos/71/chapters/18/pictures')
            page.get_by_role('button', name='Preview transfer', exact=True).click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=20000)
            page.get_by_role('button', name='Transfer privately', exact=True).click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=20000)
            self.assertEqual(1, self.requests['/media/chapter.png'])
            guest_page.reload()
            expect(picture).to_be_visible()
            self.assertGreater(picture.evaluate('el => el.naturalWidth'), 0)

    def test_vimeo_registration_history_csv(self):
        import csv
        import io

        uid = work_id("station", "vimeo", "71")
        registration_path = f"/api/library/work/{uid}/registration"
        history_path = registration_path + "/vimeo-history"
        header = b"First name,Last name,Email,Registration Date (UTC),Views,% Watched,Source Type,Source\n"
        valid_row_bytes = b"=1+1,Row,archive@example.test,2026-11-05T10:00:00Z,0,,embed,https://example.test/\n"
        payload = header + valid_row_bytes
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.get_by_role("button", name="Preview transfer", exact=True).click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=20000)
            page.get_by_role("button", name="Transfer privately", exact=True).click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=20000)
            page.goto(self.base + f"/operator/videos/{uid}/interactivity")
            expect(page.locator("#registrationVimeoHistory")).to_be_visible()
            page.set_input_files("#vimeoHistoryFile", {"name": "vimeo-history.csv", "mimeType": "text/csv", "buffer": payload})
            page.get_by_role("button", name="Upload Vimeo registrants").click()
            expect(page.locator("#registrationStatus")).to_have_text("Archived 1 Vimeo registration.")
            with page.expect_download() as download_info:
                page.get_by_role("button", name="Download Vimeo registration history").click()
            download = download_info.value
            self.assertEqual("vimeo-registration-history.csv", download.suggested_filename)

        def upload(payload):
            return self.client.post(
                history_path,
                data={"registrants": (io.BytesIO(payload), "vimeo.csv")},
                headers=self.auth,
            )

        r1 = upload(payload)
        assert r1.status_code == 200
        assert r1.json["archive"] == 1
        r2 = upload(payload)
        assert r2.status_code == 200
        assert r2.json["archive"] == 1

        assert self.client.get(history_path).status_code == 403

        resp = self.client.get(history_path, headers=self.auth)
        assert resp.status_code == 200
        assert "no-store" in resp.headers.get("Cache-Control", "")

        rows = list(csv.DictReader(io.StringIO(resp.data.decode())))
        assert len(rows) == 1
        parsed = rows[0]
        assert parsed["First name"] == "'=1+1"
        assert parsed["Email"] == "archive@example.test"
        assert parsed["Registration Date (UTC)"] == "2026-11-05T10:00:00+00:00"
        assert parsed["Views"] == "0"
        assert parsed["% Watched"] == ""

        reg = self.client.get(registration_path, headers=self.auth)
        assert reg.status_code == 200
        assert reg.json["registrants"] == []

        invalid_row = b"=1+1,Row,archive@example.test,invalid-date,0,,embed,https://example.test/\n"
        bad_payload = header + valid_row_bytes.replace(b"archive@", b"second@") + invalid_row
        bad = upload(bad_payload)
        assert bad.status_code == 422

        hist2 = self.client.get(history_path, headers=self.auth)
        assert hist2.status_code == 200
        assert len(list(csv.DictReader(io.StringIO(hist2.data.decode())))) == 1

    def test_vimeo_chapters_survive_denied_repeat_transfer(self):
        uid = work_id("station", "vimeo", "71")
        endpoint = f"/api/library/work/{uid}/chapters"
        source_path = "/videos/71/chapters"
        HandlerClass = self.source.RequestHandlerClass
        original = HandlerClass.do_GET

        def empty(handler):
            if urlsplit(handler.path).path == source_path:
                body = b'{"data":[],"total":0,"paging":{"next":null}}'
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
            else:
                return original(handler)
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")

            def transfer():
                page.get_by_role("button", name="Preview transfer", exact=True).click()
                expect(page.locator("#status")).to_have_text("inventoried", timeout=20000)
                page.get_by_role("button", name="Transfer privately", exact=True).click()
                expect(page.locator("#status")).to_have_text("transferred", timeout=20000)

            transfer()
            assert self.client.get(endpoint, headers=self.auth).json["chapters"] == [dict(title="Opening", timecode=0)]
            self.denied.add(source_path)
            transfer()
            assert self.client.get(endpoint, headers=self.auth).json["chapters"] == [dict(title="Opening", timecode=0)]
            self.denied.remove(source_path)
            with patch.object(self.source.RequestHandlerClass, "do_GET", empty):
                transfer()
                staff = self.client.get(endpoint, headers=self.auth)
                assert staff.json["chapters"] == []

    def test_vimeo_history_import_source_fixture_contract(self):
        from urllib.parse import parse_qs

        endpoint = "/api/analytics/vimeo-history/import-source"
        history = "/api/analytics/vimeo-history"
        payload = {"first": "2024-01-01", "last": "2024-01-02", "token": "fixture-supplied-token"}
        queries, source_denied = [], set()
        original = self.source.RequestHandlerClass.do_GET

        def provider_row(video, **metrics):
            return {"metadata": {"connections": {"video": {
                "uri": f"/videos/{video}", "title": f"Fixture {video}",
                "manage_link": "https://example.test/private?key=fixture-provider-secret",
            }}}, **metrics}

        first_page = [provider_row(71, views=10, unique_viewers=4),
                      *(provider_row(video, views=1) for video in range(1000, 1499))]

        def reply(handler, data, status=200):
            content = json.dumps(data).encode()
            handler.send_response(status)
            handler.send_header("Content-Type", "application/vnd.vimeo.analytics+json")
            handler.send_header("Content-Length", str(len(content)))
            handler.end_headers()
            handler.wfile.write(content)

        def analytics_provider(handler):
            parts = urlsplit(handler.path)
            if parts.path == "/me":
                return reply(handler, {"uri": "/users/555"})
            if parts.path != "/users/555/analytics":
                return original(handler)
            query = {name: values[0] for name, values in parse_qs(parts.query).items()}
            queries.append((query, handler.headers.get("Authorization")))
            if 403 in source_denied:
                return reply(handler, {"error": "denied"}, 403)
            if query["dimension"] == "total":
                return reply(handler, [{"views": 516, "unique_viewers": 5}])
            if query["page"] == "1" or 502 in source_denied:
                return reply(handler, first_page)
            return reply(handler, [provider_row(999, views=6)])

        self.enterContext(patch.object(self.source.RequestHandlerClass, "do_GET", analytics_provider))
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/analytics")
            page.get_by_text("Vimeo analytics history", exact=True).click()
            page.get_by_label("API report from (UTC)").fill(payload["first"])
            page.get_by_label("API report to (UTC)").fill(payload["last"])
            token = page.get_by_label("Vimeo access token", exact=True)
            token.fill(payload["token"])
            with page.expect_response(lambda reply: reply.url.endswith(endpoint)) as receipt:
                page.get_by_role("button", name="Import from Vimeo API", exact=True).click()
            self.assertEqual(201, receipt.value.status, receipt.value.text())
            expect(token).to_have_value("")
            expect(page.locator("#vimeoHistorySummary")).to_contain_text("Unique viewers 5")
            expect(page.locator("#vimeoHistoryTable").get_by_role("cell", name="71", exact=True)).to_be_visible()
            body = receipt.value.json()

        self.assertEqual(("api", 501), (body["source"], body["count"]))
        self.assertEqual({"views": 516, "unique_viewers": 5}, body["total"])
        self.assertEqual([("video", "1"), ("video", "2"), ("total", "1")],
                         [(params["dimension"], params["page"]) for params, _ in queries])
        for params, authorization in queries:
            self.assertEqual("Bearer cueporch-fixture", authorization)
            self.assertEqual("500", params["per_page"])
            self.assertEqual("2024-01-01T00:00:00Z", params["from"])
            self.assertEqual("2024-01-02T23:59:59Z", params["to"])
            self.assertEqual("none" if params["dimension"] == "video" else None, params.get("time_interval"))

        detail = self.client.get(f"{history}/{body['id']}", headers=self.auth)
        self.assertEqual(200, detail.status_code)
        self.assertNotIn("fixture-supplied-token", detail.text)
        self.assertNotIn("fixture-provider-secret", detail.text)
        self.assertEqual({"views": 10, "unique_viewers": 4}, detail.json["rows"][0]["metrics"])
        page6 = self.client.get(f"{history}/{body['id']}?page=6", headers=self.auth).json
        self.assertEqual(["999"], [row["source_id"] for row in page6["rows"]])

        self.assertEqual(200, self.client.post(endpoint, json=payload, headers=self.auth).status_code)
        self.assertEqual(403, self.client.post(endpoint, json=payload).status_code)
        for status in (403, 502):
            source_denied.add(status)
            self.assertEqual(status, self.client.post(endpoint, json=payload, headers=self.auth).status_code)
            source_denied.clear()
        self.assertEqual([body], self.client.get(history, headers=self.auth).json["reports"])

    def test_vimeo_history_transfer_import_and_preview(self):
        import io

        endpoint = "/api/analytics/vimeo-history"
        first, last = "2024-01-01", "2024-01-02"
        csv_bytes = (
            b"Video ID,Video,Date,Views,Unique Viewers,Impressions,Source URL,Viewer Email\n"
            b"71,Station meeting,2024-01-01,100,50,1000,"
            b"https://archive.example/screening?key=fixture-secret,viewer@example.test\n"
            b"/videos/71,Station meeting,2024-01-02,120,60,1100,,\n"
            b"999,Station meeting,2024-01-01,10,0,,,\n"
            b",Report Total,,230,70,2100,,\n"
        )

        def upload(payload, headers):
            return self.client.post(endpoint, data={
                "first": first, "last": last, "file": (io.BytesIO(payload), "vimeo-history.csv"),
            }, headers=headers)

        def live_report():
            response = self.client.post("/api/analytics/video-report", headers=self.auth,
                                        json={"from": first, "to": last})
            self.assertEqual(200, response.status_code, response.text)
            return response.json["totals"]

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.get_by_role("button", name="Preview transfer", exact=True).click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=20000)
            page.get_by_role("button", name="Transfer privately", exact=True).click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=20000)

            before = live_report()
            page.goto(self.base + "/operator/analytics")
            page.get_by_text("Vimeo analytics history", exact=True).click()
            page.get_by_label("From (UTC)", exact=True).last.fill(first)
            page.get_by_label("To (UTC)", exact=True).last.fill(last)
            page.get_by_label("Vimeo Video report CSV").set_input_files(
                {"name": "vimeo-history.csv", "mimeType": "text/csv", "buffer": csv_bytes},
            )
            with page.expect_response(lambda reply: reply.url.endswith(endpoint + "?preview=1")) as preview:
                page.get_by_role("button", name="Preview report", exact=True).click()
            self.assertEqual(200, preview.value.status)
            expect(page.locator("#vimeoHistoryStatus")).to_contain_text("Preview: 3 rows")
            self.assertEqual({"reports": []}, self.client.get(endpoint, headers=self.auth).json)

            with page.expect_response(lambda reply: reply.request.method == "POST" and reply.url.endswith(endpoint)) as imported:
                page.get_by_role("button", name="Import report", exact=True).click()
            self.assertEqual(201, imported.value.status)
            expect(page.locator("#vimeoHistoryTable").get_by_role("cell", name="71", exact=True)).to_have_count(2)
            expect(page.locator("#vimeoHistoryTable").get_by_role("cell", name="Unmatched", exact=True)).to_be_visible()
            expect(page.locator("#vimeoHistorySummary")).to_contain_text("Unique viewers 70")

        reports = self.client.get(endpoint, headers=self.auth).json["reports"]
        self.assertEqual(1, len(reports))
        report_path = endpoint + "/" + reports[0]["id"]
        response = self.client.get(report_path, headers=self.auth)
        self.assertIn("no-store", response.headers["Cache-Control"].split(", "))
        self.assertNotIn("fixture-secret", response.text)
        self.assertNotIn("viewer@example.test", response.text)
        data = response.json
        self.assertEqual((first, last, 2, 1), (data["first"], data["last"], data["matched"], data["unmatched"]))
        self.assertEqual(70, data["total"]["unique_viewers"])
        known = data["rows"][:2]
        self.assertEqual([50, 60], [row["metrics"]["unique_viewers"] for row in known])
        self.assertTrue(all(row["work"] == work_id("station", "vimeo", "71") for row in known))
        self.assertEqual("archive.example", known[0]["dimensions"]["source"])
        unknown = data["rows"][2]
        self.assertIsNone(unknown["work"])
        self.assertEqual(0, unknown["metrics"]["unique_viewers"])
        self.assertNotIn("impressions", unknown["metrics"])

        self.assertEqual(200, upload(csv_bytes, self.auth).status_code)
        invalid = upload(csv_bytes.replace(b"2024-01-02", b"2024-01-03"), self.auth)
        self.assertEqual(400, invalid.status_code)
        self.assertEqual(reports, self.client.get(endpoint, headers=self.auth).json["reports"])
        self.assertEqual(403, upload(csv_bytes, {}).status_code)
        self.assertEqual(403, self.client.get(report_path).status_code)
        self.assertEqual(before, live_report())

    def test_vimeo_review_csv_import_for_earlier_cut(self):
        import csv
        import io

        from installed_cards import select_action

        uid = work_id("station", "vimeo", "71")
        root_text = 'A comma, a "quote", and\na second line.'
        raw = io.StringIO(newline='')
        writer = csv.writer(raw)
        writer.writerow(('Video Version','#','Timecode','Username','Note','Reply','Date Added','Resolved'))
        writer.writerows([
            ('old-cut.mp4', '1', '00:00:00.500', 'Fixture Reviewer', root_text, '--', 'Tuesday, July 9, 2019 At 8:49 AM', 'No'),
            ('', '2', '00:00:00.500', 'Guest Reviewer', root_text, 'Reply note', '2019-07-09T08:49:00+00:00', 'No'),
            ('final-cut.mp4', '1', '00:00:01', 'Other Reviewer', 'Final note', '--', '2019-07-09T08:49:00Z', 'Yes'),
        ])
        payload = raw.getvalue().encode('utf-8-sig')

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.get_by_role("button", name="Preview transfer", exact=True).click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=20000)
            page.get_by_role("button", name="Transfer privately", exact=True).click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=20000)
            versions = self.client.get(f"/api/library/work/{uid}/versions", headers=self.auth).get_json()
            earlier = next(version for version in versions if version.get("source_version") == "/videos/71/versions/202")
            current = next(version for version in versions if version["current"])
            response = self.client.post(f"/api/library/work/{uid}/review-links", headers=self.auth, json={"name": "CSV review", "password": "fixture-review-lock", "allow_versions": True, "allow_resolved": True})
            assert response.status_code == 201
            link = response.get_json()
            page.goto(self.base + "/operator/library")
            page.get_by_role("button", name="Load library", exact=True).click()
            select_action(page, "Station meeting", "Review link", uid)
            page.locator("#workDialog li").filter(has_text="CSV review").get_by_role("button", name="Comments", exact=True).click()
            page.get_by_label("Video version").select_option(earlier["key"])
            page.get_by_label("Vimeo review CSV").set_input_files({"name": "review.csv", "mimeType": "text/csv", "buffer": payload})
            page.get_by_role("button", name="Import Vimeo review notes", exact=True).click()
            expect(page.get_by_label("Source cut in CSV")).to_be_visible()
            page.get_by_label("Source cut in CSV").select_option(label="old-cut.mp4")
            page.get_by_role("button", name="Import Vimeo review notes", exact=True).click()
            expect(page.locator("#workDialogStatus")).to_have_text("Imported 2 notes · 0 already present.")
            page = browser.new_page()
            page.goto(self.base + link["path"] + "?version=" + earlier["key"])
            page.get_by_label("Review password", exact=True).fill("fixture-review-lock")
            page.get_by_role("button", name="Open review", exact=True).click()
            expect(page.locator("#reviewTourPlayer")).to_be_visible()
            expect(page.get_by_text("Reply note", exact=True)).to_be_visible()
            expect(page.get_by_text("Final note", exact=True)).to_have_count(0)
            page.get_by_role("button", name="Go to 0:00.50").click()
            page.wait_for_function(
                """() => {
                    const v = document.querySelector('video');
                    return !!v && Math.abs(v.currentTime - 0.5) <= 0.04 && (v.paused || !v.seeking);
                }"""
            )
            feedback = link['path'] + '/feedback/' + earlier['key']
            endpoint = feedback + '/vimeo-comments.csv'

            comments = self.client.get(feedback, headers=self.auth).get_json()['comments']
            assert len(comments) == 2
            root = next(c for c in comments if not c['parent'])
            assert root['body'] == root_text
            assert root['seconds'] == .5
            reply = next(c for c in comments if c['parent'] == root['id'])
            assert reply['body'] == 'Reply note'

            assert self.client.get(link['path'] + '/feedback/' + current['key'], headers=self.auth).get_json()['comments'] == []
            assert self.client.get(feedback).status_code == 403
            assert self.client.post(endpoint, data={'file': (io.BytesIO(payload), 'review.csv')}).status_code == 403

            response = self.client.post(endpoint, data={'file': (io.BytesIO(payload), 'review.csv'), 'source_cut': 'old-cut.mp4'}, headers=self.auth)
            assert response.status_code == 201
            assert response.get_json() == {'imported': 0, 'existing': 2}

            response = self.client.put(feedback + '/' + root['id'] + '/resolved', json={'resolved': True}, headers=self.auth)
            assert response.status_code == 204
            assert self.client.post(endpoint, data={'file': (io.BytesIO(payload), 'review.csv'), 'source_cut': 'old-cut.mp4'}, headers=self.auth).get_json() == {'imported': 0, 'existing': 2}
            comments = self.client.get(feedback, headers=self.auth).get_json()["comments"]
            self.assertTrue(next(comment for comment in comments if comment["id"] == root["id"])["resolved"])

    def test_vimeo_viewer_discussions_keep_threads_and_privacy(self):
        self.source.viewer_comments_fixture = True
        self.source.public_viewer_fixture = True
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + '/operator/migration')

            def transfer():
                page.get_by_role('button', name='Preview transfer').click()
                expect(page.locator('#status')).to_have_text('inventoried', timeout=20000)
                page.get_by_role('button', name='Transfer privately').click()
                expect(page.locator('#status')).to_have_text('transferred', timeout=20000)

            transfer()
            data = self.client.get('/api/migration', headers=self.auth).get_json()
            works = {w['source_id']: w['source_viewer_comments'] for w in data['works']}
            public_roots = works['71']['roots']
            self.assertEqual(2, len(public_roots))
            self.assertEqual(['Historic festival discussion', 'Matinee screening'], [r['body'] for r in public_roots])
            self.assertEqual('Historic festival reply', public_roots[0]['replies'][0]['body'])
            self.assertEqual('Guest Reviewer', public_roots[0]['replies'][0]['author'])
            self.assertEqual('Restricted archive message', works['72']['roots'][0]['body'])
            self.assertEqual(403, self.client.get('/api/migration').status_code)
            self.assertEqual(4, self.requests['/videos/71/comments'])
            page.get_by_text('2 viewer threads', exact=True).click()
            expect(page.locator('details[open] > ol > li').filter(has_text='Festival Viewer: Historic festival discussion')).to_be_visible()
            uid = work_id('station', 'vimeo', '71')
            assert self.client.get('/api/viewer/comments/work/' + uid).status_code == 404
            r = self.client.put(f'/api/library/work/{uid}/source-privacy', headers=self.auth, json={})
            assert r.status_code == 200
            viewer = browser.new_page()
            viewer.goto(self.base + '/watch/work/' + uid)
            expect(viewer.get_by_text('Historic festival discussion', exact=True)).to_be_visible()
            expect(viewer.get_by_text('Historic festival reply', exact=True)).to_be_visible()
            expect(viewer.get_by_text('Matinee screening', exact=True)).to_be_visible()
            expect(viewer.get_by_text('Guest Reviewer', exact=True)).to_be_visible()
            expect(viewer.get_by_text('Restricted archive message', exact=True)).to_have_count(0)
            response = self.client.put(
                '/api/policy/public_comments',
                json={'required': True},
                headers=self.auth,
            )
            assert response.status_code == 200

            self.source.new_viewer_comment = True
            transfer()
            viewer.reload()
            expect(viewer.get_by_text('Matinee screening', exact=True)).to_be_visible()
            expect(viewer.get_by_text('Approval test viewer note', exact=True)).to_have_count(0)

            response = self.client.get('/api/actions', headers=self.auth)
            actions = response.get_json()
            action = next(
                a for a in actions
                if a['comment_body'] == 'Approval test viewer note'
            )
            response = self.client.post(
                f"/api/actions/{action['id']}/decision",
                json={'decision': 'approve'},
                headers=self.auth,
            )
            assert response.status_code == 204

            viewer.reload()
            expect(viewer.get_by_text('Approval test viewer note', exact=True)).to_be_visible()

            response = self.client.put(
                '/api/policy/public_comments',
                json={'required': False},
                headers=self.auth,
            )
            assert response.status_code == 200
            self.source.viewer_comment_text = 'Updated historic festival discussion'
            self.denied.add('/videos/71/comments/501/replies')
            transfer()
            viewer.reload()
            expect(viewer.get_by_text('Updated historic festival discussion', exact=True)).to_be_visible()
            expect(viewer.get_by_text('Historic festival reply', exact=True)).to_be_visible()
            comments = self.client.get('/api/operator/public-comments', headers=self.auth).get_json()['comments']
            primary = next(c for c in comments if c['body'] == self.source.viewer_comment_text)
            self.assertEqual(('Festival Viewer', 'vimeo', False), (primary['author'], primary['origin'], primary['owned']))
            self.assertNotIn('Restricted archive message', json.dumps(comments))
            response = self.client.delete(f"/api/operator/public-comments/{primary['id']}", headers=self.auth)
            self.assertEqual(204, response.status_code, response.text)
            self.denied.clear()
            transfer()
            viewer.reload()
            expect(viewer.get_by_text('Matinee screening', exact=True)).to_be_visible()
            expect(viewer.get_by_text('Updated historic festival discussion', exact=True)).to_have_count(0)
            expect(viewer.get_by_text('Historic festival reply', exact=True)).to_have_count(0)

    def test_vimeo_editorial_status_follows_source_cut(self):
        work_key = work_id('station', 'vimeo', '71')
        work_url = f'/api/library/work/{work_key}'
        editorial_url = f'{work_url}/editorial-status'

        def transfer(page):
            page.get_by_role('button', name='Preview transfer').click()
            expect(page.locator('#status')).to_have_text('inventoried', timeout=20000)
            page.get_by_role('button', name='Transfer privately').click()
            expect(page.locator('#status')).to_have_text('transferred', timeout=20000)

        def editorial_status():
            response = self.client.get(editorial_url, headers=self.auth)
            self.assertEqual(200, response.status_code, response.text)
            return response.get_json()

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(
                f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})"
            )
            page.goto(self.base + '/operator/migration')

            transfer(page)
            self.assertEqual(editorial_status()['status'], 'Needs review')

            self.source.review_status = 'approved'
            with patch('review_feedback.folder_slack_queue.enqueue') as notice:
                transfer(page)
                notice.assert_not_called()
            self.assertEqual('Approved', editorial_status()['status'])

            details = browser.new_page()
            details.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            details.goto(self.base + '/operator/videos/' + work_key)
            expect(details.locator('#editorialState')).to_have_value('Approved')
            details.select_option('#editorialState', label='In progress')
            expect(details.locator('#detailsStatus')).to_contain_text('Editorial status saved.')
            self.assertEqual('In progress', editorial_status()['status'])

            self.source.review_status = 'needs_review'
            transfer(page)
            self.assertEqual(editorial_status()['status'], 'In progress')

            response = self.client.get(work_url + '/versions', headers=self.auth)
            versions = response.get_json()
            older_key = next(v['key'] for v in versions if v.get('source_version') == '/videos/71/versions/202')
            current_key = editorial_status()['version']
            response = self.client.post(work_url + '/versions/restore', json={'current': current_key, 'restore': older_key}, headers=self.auth)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual({'status': 'In progress', 'version': older_key}, editorial_status())
            self.source.review_status = 'approved'
            transfer(page)
            self.assertEqual({'status': 'In progress', 'version': older_key}, editorial_status())

    def test_migration_replaces_vimeo_review_links(self):
        from installed_cards import select_action

        uid = work_id("station", "vimeo", "71")
        work = f"/api/library/work/{uid}"
        source_links = work + "/vimeo-review-links"
        original_password = "fixture-sensitive-review-password"
        local_password = "local-replacement-123"
        secrets_to_exclude = (
            original_password, "fixture-sensitive-vimeo-access",
            "7f4f9c2e-3b1a-4c8d-9e2f-6a5b4c3d2e1f",
        )

        def inventory():
            response = self.client.get("/api/migration", headers=self.auth)
            self.assertEqual(200, response.status_code)
            for value in secrets_to_exclude:
                self.assertNotIn(value, response.text)
            return {item["source_id"]: item["source_review"] for item in response.json["works"]}

        def presets():
            response = self.client.get(source_links, headers=self.auth)
            self.assertEqual(200, response.status_code, response.text)
            for value in secrets_to_exclude:
                self.assertNotIn(value, response.text)
            return {link["name"]: link for link in response.json["links"]}

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            preview = page.get_by_role("button", name="Preview transfer")
            transfer = page.get_by_role("button", name="Transfer privately")
            status = page.locator("#status")
            preview.click()
            expect(status).to_have_text("inventoried", timeout=20000)
            expect(page.get_by_text("Needs review · 2 Vimeo review links")).to_be_visible()
            records = inventory()
            self.assertEqual(("available", "needs_review"), (records["71"]["state"], records["71"]["status"]))
            self.assertEqual({"Panel feedback", "Expired panel feedback"},
                             {link["name"] for link in records["71"]["links"]})
            self.assertEqual(("available", []), (records["72"]["state"], records["72"]["links"]))
            self.assertEqual(0, self.requests["/media/71.mp4"])

            transfer.click()
            expect(status).to_have_text("transferred", timeout=20000)
            self.assertEqual(403, self.client.get(source_links).status_code)
            saved = presets()
            issue = source_links + "/" + saved["Panel feedback"]["id"]
            old = source_links + "/" + saved["Expired panel feedback"]["id"]
            other = work_id("station", "vimeo", "72")
            cross = f"/api/library/work/{other}/vimeo-review-links/" + saved["Panel feedback"]["id"]
            self.assertEqual(400, self.client.post(issue, headers=self.auth, json={"password": ""}).status_code)
            self.assertEqual(400, self.client.post(old, headers=self.auth, json={"password": local_password}).status_code)
            self.assertEqual(404, self.client.post(cross, headers=self.auth, json={"password": local_password}).status_code)
            self.assertEqual([], self.client.get(work + "/review-links", headers=self.auth).json)

            page.goto(self.base + "/operator/library")
            page.get_by_role("button", name="Load library").click()
            select_action(page, "Station meeting", "Review link", uid)
            expect(page.get_by_role("heading", name="Vimeo review settings")).to_be_visible()
            expect(page.get_by_role("button", name="Create replacement for Expired panel feedback")).to_have_count(0)
            page.get_by_label("Password for replacement of Panel feedback").fill(local_password)
            page.get_by_role("button", name="Create replacement for Panel feedback", exact=True).click()
            expect(page.locator("#workDialogStatus")).to_have_text("Replacement created for Panel feedback.")
            replacement_path = presets()["Panel feedback"]["replacement"]

            repeat = self.client.post(issue, headers=self.auth, json={})
            self.assertEqual(200, repeat.status_code, repeat.text)
            link = repeat.json
            self.assertEqual((replacement_path, saved["Panel feedback"]["expires"]), (link["path"], link["expires"]))
            for flag in ("password", "allow_versions", "allow_comment", "allow_status", "allow_resolved", "dark"):
                self.assertTrue(link[flag], flag)
            for flag in ("allow_download", "show_other_videos"):
                self.assertFalse(link[flag], flag)
            self.assertIsNone(link["logo"])
            self.assertEqual(1, len(self.client.get(work + "/review-links", headers=self.auth).json))

            viewer = browser.new_page()
            viewer.goto(self.base + replacement_path)
            secret = viewer.get_by_label("Review password")
            secret.fill(original_password)
            viewer.get_by_role("button", name="Open review").click()
            expect(viewer.get_by_role("alert")).to_have_text("That password did not open the review.")
            secret.fill(local_password)
            viewer.get_by_role("button", name="Open review").click()
            player = viewer.locator("#reviewTourPlayer")
            expect(player).to_be_visible()
            player.evaluate("async video => { video.muted = true; await video.play(); }")
            viewer.wait_for_function("document.querySelector('#reviewTourPlayer').currentTime > .2")

            response = self.client.delete(work + "/review-links/" + link["id"], headers=self.auth)
            self.assertEqual(204, response.status_code)
            self.assertEqual(404, viewer.request.get(self.base + replacement_path).status)
            page.goto(self.base + "/operator/migration")
            self.denied.add("/videos/72")
            preview.click()
            expect(status).to_have_text("inventoried", timeout=20000)
            self.assertEqual("unavailable", inventory()["72"]["state"])
            transfer.click()
            expect(status).to_have_text("transferred", timeout=20000)
            self.assertIsNone(presets()["Panel feedback"]["replacement"])
            self.assertEqual(404, self.client.post(issue, headers=self.auth, json={"password": local_password}).status_code)
            for path in self.temp.rglob("*.json"):
                contents = path.read_text(encoding="utf8")
                for value in (*secrets_to_exclude, local_password):
                    self.assertNotIn(value, contents, str(path))

    def test_private_version_transfer_recovers_earlier_cuts(self):
        uid = work_id("station", "vimeo", "71")
        work = f"/api/library/work/{uid}"
        earlier_mp4, earlier_webm = "/videos/71/versions/202", "/videos/71/versions/200"
        external_media = ("/media/previous.mp4", "/media/previous.webm")

        def versions():
            response = self.client.get(work + "/versions", headers=self.auth)
            self.assertEqual(200, response.status_code, response.get_data(as_text=True))
            return response.get_json()

        def review(allow_versions):
            response = self.client.post(work + "/review-links", headers=self.auth, json={
                "name": "Migrated previous cuts", "allow_versions": allow_versions,
            })
            self.assertEqual(201, response.status_code, response.get_data(as_text=True))
            return self.base + response.get_json()["path"]

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.get_by_role("button", name="Preview transfer").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            previous = page.get_by_text("3 previous cuts", exact=False)
            previous.click()
            expect(page.get_by_text("Vimeo version 2 · Available to transfer")).to_be_visible()
            expect(page.get_by_text("Vimeo version 3 · Owner file needed", exact=False)).to_be_visible()
            self.assertEqual([0, 0], [self.requests[path] for path in external_media])

            transfer = page.get_by_role("button", name="Transfer privately")
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=18000)
            before = versions()
            self.assertEqual(3, len(before))
            current = before[0]
            self.assertEqual((True, "/videos/71/versions/203"), (current["current"], current["source_version"]))
            old = {item["source_version"]: item for item in before[1:]}
            self.assertEqual({earlier_mp4, earlier_webm}, set(old))
            self.assertEqual(["mp4", "webm"], [file["format"] for file in old[earlier_webm]["video_files"]])
            mp4_key = old[earlier_mp4]["key"]

            viewer = browser.new_page()
            self.assertEqual(404, viewer.request.get(self.base + f"/watch/work/{uid}").status)
            self.assertEqual(404, viewer.request.get(review(False) + "?version=" + mp4_key).status)
            allowed = review(True)
            for source_uri, dimensions in ((earlier_mp4, [256, 144]), (earlier_webm, [192, 108])):
                viewer.goto(allowed + "?version=" + old[source_uri]["key"])
                video = viewer.locator("#reviewTourPlayer")
                expect(video).to_be_visible()
                video.evaluate("async video => { video.muted = true; await video.play(); }")
                viewer.wait_for_function("""([width, height]) => {
                    const video = document.querySelector('#reviewTourPlayer');
                    return video.videoWidth === width && video.videoHeight === height && video.currentTime > .2;
                }""", arg=dimensions)

            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=15000)
            self.assertEqual(before, versions())
            self.assertEqual([1, 1], [self.requests[path] for path in external_media])

            restored = self.client.post(work + "/versions/restore", headers=self.auth, json={
                "current": current["key"], "restore": mp4_key,
            })
            self.assertEqual(200, restored.status_code, restored.get_data(as_text=True))
            self.denied.add(earlier_mp4)
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=15000)
            previous.click()
            expect(page.get_by_text("Vimeo version 2 · Already in Library", exact=False)).to_be_visible()
            after = versions()
            self.assertEqual(mp4_key, after[0]["key"])
            self.assertEqual({item["key"] for item in before}, {item["key"] for item in after})
            self.assertEqual([1, 1], [self.requests[path] for path in external_media])
            for manifest in self.temp.rglob("*.json"):
                self.assertNotIn("fixture-signature", manifest.read_text(encoding="utf8"))

    def test_migration_preserves_previous_subtitles(self):
        uid = work_id("station", "vimeo", "71")
        work = f"/api/library/work/{uid}"
        earlier_mp4, earlier_webm = "/videos/71/versions/202", "/videos/71/versions/200"
        texttracks = "/videos/71/versions/202/texttracks"
        subtitle_paths = ("/media/previous-fr.vtt", "/media/previous-en.srt")

        def versions():
            response = self.client.get(work + "/versions", headers=self.auth)
            self.assertEqual(200, response.status_code, response.get_data(as_text=True))
            return response.get_json()

        def review(allow_versions):
            response = self.client.post(work + "/review-links", headers=self.auth, json={
                "name": "Migrated previous cuts", "allow_versions": allow_versions,
            })
            self.assertEqual(201, response.status_code, response.get_data(as_text=True))
            return self.base + response.get_json()["path"]

        self.denied.add(texttracks)

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.get_by_role("button", name="Preview transfer").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            previous = page.get_by_text("3 previous cuts", exact=False)
            previous.click()
            expect(page.get_by_text("Source inventory unavailable · HTTP 403", exact=True)).to_be_visible()

            transfer = page.get_by_role("button", name="Transfer privately")
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=18000)
            before = versions()
            self.assertEqual(3, len(before))
            current = before[0]
            self.assertEqual((True, "/videos/71/versions/203"), (current["current"], current["source_version"]))
            old = {item["source_version"]: item for item in before[1:]}
            self.assertEqual({earlier_mp4, earlier_webm}, set(old))
            earlier = old[earlier_mp4]["key"]

            old_review = review(True) + "?version=" + earlier
            denied_review = review(False) + "?version=" + earlier
            viewer = browser.new_page()
            self.assertEqual(404, viewer.request.get(denied_review).status)
            viewer.goto(old_review)
            expect(viewer.locator('#reviewTourPlayer track[srclang="fr"]')).to_have_count(0)

            self.denied.discard(texttracks)
            page.get_by_role("button", name="Preview transfer").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            previous.click()
            expect(page.get_by_text("fr · subtitles · Available to transfer")).to_be_visible()
            expect(page.get_by_text("de · subtitles · Disabled on Vimeo")).to_be_visible()
            expect(page.get_by_text("es · captions · Owner file needed")).to_be_visible()
            self.assertEqual([0, 0], [self.requests[path] for path in subtitle_paths])

            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=18000)
            after = versions()
            self.assertEqual(current["key"], after[0]["key"])
            after_old = {item["source_version"]: item for item in after[1:]}
            self.assertEqual(earlier, after_old[earlier_mp4]["key"])
            self.assertEqual([1, 1], [self.requests[path] for path in subtitle_paths])
            self.assertEqual(0, self.requests["/media/disabled-de.vtt"])
            self.assertEqual(404, viewer.request.get(denied_review).status)

            viewer.goto(old_review)
            video = viewer.locator("#reviewTourPlayer")
            kind = video.evaluate(
                """async (video) => {
                    const track = video.querySelector('track[srclang="fr"]');
                    track.track.mode = "showing";
                    video.muted = true;
                    await video.play();
                    return track.kind;
                }"""
            )
            self.assertEqual("subtitles", kind)
            viewer.wait_for_function(
                """() => {
                    const track = document.querySelector('#reviewTourPlayer track[srclang="fr"]');
                    const cues = (track && track.track.activeCues) || [];
                    return Array.from(cues).some(cue => cue.text.includes("Earlier cut caption"));
                }"""
            )

            resp = self.client.post(work + "/versions/restore", json={"current": current["key"], "restore": earlier}, headers=self.auth)
            self.assertEqual(resp.status_code, 200)
            resp = self.client.get(work + "/languages/fr", headers=self.auth)
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["cues"][0]["text"], "Earlier cut caption")
            english = self.client.get(work + "/languages/en", headers=self.auth)
            self.assertEqual(200, english.status_code)
            self.assertEqual("Earlier cut English", english.get_json()["cues"][0]["text"])
            data["cues"][0]["text"] = "Local French edit"
            resp = self.client.put(work + "/languages/fr", json={"version": data["version"], "revision": data["revision"], "cues": data["cues"]}, headers=self.auth)
            self.assertEqual(resp.status_code, 200)
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=18000)
            resp = self.client.get(work + "/languages/fr", headers=self.auth)
            self.assertEqual(resp.get_json()["cues"][0]["text"], "Local French edit")
            resp = self.client.get(work + "/languages/fr", headers=self.auth)
            data = resp.get_json()
            resp = self.client.delete(work + "/languages/fr", json={"version": data["version"], "revision": data["revision"]}, headers=self.auth)
            self.assertIn(resp.status_code, (200, 204))
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=18000)
            resp = self.client.get(work + "/languages/fr", headers=self.auth)
            self.assertEqual(resp.status_code, 404)
            for manifest in self.temp.rglob("*.json"):
                self.assertNotIn("fixture-signature", manifest.read_text(encoding="utf8"))

    def test_migration_preserves_version_alternate_audio(self):
        uid = work_id("station", "vimeo", "71")
        work = f"/api/library/work/{uid}"
        audio = work + "/audio-tracks"
        source_paths = ("version-203-es.wav", "version-202-en.mp3")
        password = "fixture-screening-audio"

        def get_tracks():
            response = self.client.get(audio, headers=self.auth)
            self.assertEqual(200, response.status_code)
            return response.get_json()

        def confirm_source(kind, language, filename):
            path = f"{audio}/{kind}/{language}"
            with self.client.get(path, headers=self.auth) as response:
                self.assertEqual(200, response.status_code)
                self.assertEqual((self.temp / "vimeo" / filename).read_bytes(), response.data)
            self.assertEqual(403, self.client.get(path).status_code)

        def play_track(browser, value, tone, width):
            viewer = browser.new_page()
            viewer.goto(self.base + f"/watch/work/{uid}")
            viewer.get_by_label("Video password").fill(password)
            viewer.get_by_role("button", name="Watch video").click()
            player = viewer.locator("video")
            player.click(position={"x": 12, "y": 12})
            selector = viewer.get_by_role("combobox", name="Audio track", exact=True)
            selector.select_option(value)
            expect(player).to_have_attribute("data-audio-track", value)
            self.assertAlmostEqual(tone, player.evaluate(SAMPLE_AUDIO), delta=24)
            self.assertEqual(width, player.evaluate("video => video.videoWidth"))
            viewer.close()

        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(self.base + "/operator/migration")
            page.get_by_role("button", name="Preview transfer").click()
            expect(page.locator("#status")).to_have_text("inventoried", timeout=15000)
            results = page.get_by_text("4 audio results", exact=True)
            results.click()
            expect(page.get_by_text("Vimeo version 203 · es · dubbed · Available to transfer")).to_be_visible()
            expect(page.get_by_text("Vimeo version 203 · en · description · Owner file needed")).to_be_visible()
            expect(page.get_by_text("Vimeo version 201 · Source inventory unavailable · HTTP 403")).to_be_visible()
            self.assertEqual([0, 0], [self.requests[f"/media/{name}"] for name in source_paths])

            transfer = page.get_by_role("button", name="Transfer privately")
            transfer.click()
            expect(page.locator("#status")).to_have_text("transferred", timeout=20000)
            results.click()
            expect(page.get_by_text("Vimeo version 203 · es · dubbed · Recovered")).to_be_visible()
            self.assertEqual(404, self.client.get(f"/watch/work/{uid}").status_code)
            current = get_tracks()
            self.assertEqual([("es", "dubbed")], [(item["language"], item["kind"]) for item in current["tracks"]])
            confirm_source("dubbed", "es", source_paths[0])
            published = self.client.put(work + "/source-privacy", headers=self.auth, json={"password": password})
            self.assertEqual(200, published.status_code, published.get_data(as_text=True))
            play_track(browser, "dubbed:es", 880, 320)

            versions = self.client.get(work + "/versions", headers=self.auth).get_json()
            old = next(version for version in versions if version["source_version"] == "/videos/71/versions/202")
            restored = self.client.post(work + "/versions/restore", headers=self.auth, json={
                "current": current["version"], "restore": old["key"],
            })
            self.assertEqual(200, restored.status_code, restored.get_data(as_text=True))
            summary = get_tracks()
            self.assertEqual([("en", "commentary")], [(item["language"], item["kind"]) for item in summary["tracks"]])
            confirm_source("commentary", "en", source_paths[1])
            play_track(browser, "commentary:en", 660, 256)

            removed = self.client.delete(audio + "/commentary/en", headers=self.auth, json={
                "version": old["key"], "revision": summary["tracks"][0]["revision"],
            })
            self.assertEqual(200, removed.status_code, removed.get_data(as_text=True))
            with patch("library_audio.render_audio", side_effect=AssertionError("Cached rendition was rendered again")):
                transfer.click()
                expect(page.locator("#status")).to_have_text("transferred", timeout=20000)
            self.assertEqual({"version": old["key"], "tracks": [], "available": True}, get_tracks())
            for manifest in self.temp.rglob("*.json"):
                self.assertNotIn("fixture-signature", manifest.read_text(encoding="utf8"))
