import sys
from contextlib import ExitStack
import json
from pathlib import Path
import secrets
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from flask import Flask
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


class PremiereReviewMarkers(unittest.TestCase):
    def test_panel_review_markers(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps(dict(institution="station", schema=1, works=[]))
            )
            clip = root / "Review clip.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=24",
                    "-t",
                    "3",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(clip),
                ],
                check=True,
                capture_output=True,
            )
            compiled_dir = root / "panel"
            repo_root = Path(__file__).resolve().parents[1]
            subprocess.run(
                [
                    "node",
                    str(
                        repo_root / "ui" / "node_modules" / "typescript" / "bin" / "tsc"
                    ),
                    "--strict",
                    "--target",
                    "ES2022",
                    "--module",
                    "commonjs",
                    "--outDir",
                    str(compiled_dir),
                    "integrations/premiere/panel.ts",
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
            )
            remote = Flask("cueporch-service")
            service = make_server(
                "127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True
            )
            base = f"http://127.0.0.1:{service.server_port}"
            database = root / "workspace.sqlite"
            operator = secrets.token_urlsafe(32)

            def start_app():
                with patch(
                    "institution.library_worker.start", return_value=lambda: None
                ):
                    app = create_app(root, database, operator, "station")
                stops = [
                    stop
                    for name, stop in app.extensions.items()
                    if name.startswith("stop_")
                ]

                def stop_app():
                    while stops:
                        stops.pop()()

                cleanup.callback(stop_app)
                service.app = app
                return stop_app

            start_app()
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                self.exercise(browser, base, operator, clip, compiled_dir)

    def exercise(self, browser, base, operator, clip, compiled_dir):
        from uuid import uuid4
        import json
        from installed_cards import select_action

        page = browser.new_page(accept_downloads=True)
        page.add_init_script(
            "sessionStorage.setItem('cueporchOperator', %s);" % json.dumps(operator)
        )
        api = page.context.request
        auth = {"Authorization": "Bearer " + operator}

        upload_id = str(uuid4())
        resp = api.post(
            base + "/api/library/uploads",
            headers=auth,
            multipart={
                "upload_id": upload_id,
                "file": {
                    "name": clip.name,
                    "mimeType": "video/mp4",
                    "buffer": clip.read_bytes(),
                },
            },
        )
        self.assertEqual(201, resp.status)
        work_id = resp.json()["id"]

        resp = api.get(
            base + "/api/library/work/" + work_id + "/versions", headers=auth
        )
        self.assertEqual(200, resp.status)
        versions = resp.json()
        self.assertIsInstance(versions, list)
        key = versions[0]["key"]

        resp = api.post(
            base + "/api/library/work/" + work_id + "/review-links",
            headers=auth,
            data={"name": "Editor review"},
        )
        self.assertEqual(201, resp.status)
        path = resp.json()["path"]
        feedback = base + path + "/feedback/" + key

        resp = api.post(
            feedback,
            data={
                "author": "Test reviewer",
                "seconds": 1.25,
                "body": "=preserve, this\nnext line",
            },
        )
        self.assertEqual(201, resp.status)
        first_id = resp.json()["id"]

        resp = api.post(
            feedback,
            data={
                "author": "Test reviewer",
                "body": "Reply body",
                "parent": first_id,
            },
        )
        self.assertEqual(201, resp.status)
        reply_id = resp.json()["id"]

        resp = api.post(
            feedback,
            data={
                "author": "Test reviewer",
                "seconds": 1.5,
                "body": "Second top-level",
            },
        )
        self.assertEqual(201, resp.status)
        second_id = resp.json()["id"]

        resp = api.put(
            feedback + "/" + second_id + "/resolved",
            headers=auth,
            data={"resolved": True},
        )
        self.assertEqual(204, resp.status)

        resp = api.get(feedback + "/markers.json")
        self.assertEqual(403, resp.status)

        page.goto(base + "/operator/library")
        page.get_by_role("button", name="Load library", exact=True).click()
        select_action(page, clip.stem, "Review link", work_id)

        page.locator("#workDialog li").filter(has_text="Editor review").get_by_role(
            "button", name="Comments"
        ).click()

        unknown_markers_url = feedback.rsplit("/", 1)[0] + "/unknown/markers.json"
        expired = api.get(unknown_markers_url, headers=auth)
        assert expired.status == 404
        markers = api.get(feedback + "/markers.json", headers=auth)
        assert markers.status == 200
        assert "no-store" in markers.headers.get("cache-control", "")
        with page.expect_download() as marker_download_info:
            page.locator(
                "#workDialog button", has_text="Download Premiere markers"
            ).click()
        marker_download = marker_download_info.value
        assert marker_download.suggested_filename == "cueporch-review-markers.json"
        download_path = Path(clip.parent) / marker_download.suggested_filename
        marker_download.save_as(download_path)
        marker_text = download_path.read_text()
        marker_payload = json.loads(marker_text)
        assert marker_payload == {
            "schema": 1,
            "version": key,
            "markers": [
                {
                    "id": first_id,
                    "author": "Test reviewer",
                    "body": "=preserve, this\nnext line",
                    "seconds": 1.25,
                }
            ],
        }
        assert str(path) not in marker_text
        with page.expect_download() as csv_download_info:
            page.locator("#workDialog button", has_text="Download comments CSV").click()
        csv_download = csv_download_info.value
        csv_path = Path(clip.parent) / csv_download.suggested_filename
        csv_download.save_as(csv_path)
        assert csv_path.exists()
        csv_text = csv_path.read_text()
        assert "'=preserve, this" in csv_text
        assert "\n=preserve" not in csv_text
        subprocess.run(
            [
                "node",
                str(Path(__file__).with_name("premiere_marker_host.cjs")),
                str(compiled_dir),
                str(download_path),
            ],
            check=True,
        )
        comments = api.get(feedback, headers=auth)
        assert comments.status == 200
        assert reply_id in comments.text()
        print("evidence: private export; real browser CSV; mock marker timing; dedup")
