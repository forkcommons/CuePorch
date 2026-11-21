import hashlib
import json
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from flask import Flask
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


class EditorExports(unittest.TestCase):
    def test_private_export_intake_and_recovery(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps(dict(institution="station", schema=1, works=[]))
            )

            export_dir = root / "exports"
            export_dir.mkdir()
            clip = root / "sample.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=320x180:rate=30",
                    "-t",
                    "3",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(clip),
                ],
                check=True,
            )
            shutil.copyfile(clip, export_dir / "Old export.mp4")

            database = root / "workspace.sqlite"
            sqlite3.connect(database).close()
            operator = secrets.token_urlsafe(32)

            remote = Flask("cueporch-service")
            service = make_server(
                "127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True
            )
            base = f"http://127.0.0.1:{service.server_port}"

            cleanup.enter_context(
                patch.dict(
                    "os.environ",
                    {
                        "CUEPORCH_PUBLIC_URL": base,
                        "CUEPORCH_EDITOR_EXPORT_FOLDER": str(export_dir),
                    },
                )
            )
            cleanup.enter_context(patch("event_peer.host_available", return_value=True))
            cleanup.enter_context(patch("editor_exports.POLL_SECONDS", 0.15))
            cleanup.enter_context(patch("editor_exports.SETTLE_NS", 300_000_000))

            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, database, operator, "station")
            service.app = app

            def restart():
                nonlocal app
                for name, stop in list(app.extensions.items()):
                    if name.startswith("stop_"):
                        stop()
                        app.extensions.pop(name, None)
                with patch(
                    "institution.library_worker.start", return_value=lambda: None
                ):
                    app = create_app(root, database, operator, "station")
                service.app = app
                return app

            cleanup.callback(
                lambda: [
                    stop()
                    for name, stop in list(app.extensions.items())
                    if name.startswith("stop_")
                ]
            )
            cleanup.enter_context(service_worker(service))

            with sync_playwright() as playwright, chromium(playwright) as browser:
                self.exercise(browser, base, operator, clip, export_dir, restart)

    def exercise(self, browser, base, operator, clip, export_dir, restart):
        page = browser.new_page()
        page.add_init_script(
            f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator)})"
        )
        api = page.context.request
        token = operator
        auth = {"Authorization": f"Bearer {token}"}
        watch_url = base + "/api/integrations/editor-exports/watch"

        anon = api.get(watch_url)
        assert anon.status == 403, anon.text()

        folder_resp = api.post(
            base + "/api/library/folders",
            headers=auth,
            data={"name": "Edits", "parent": None, "color": "#224466"},
        )
        assert folder_resp.status == 201, folder_resp.text()
        folder_id = folder_resp.json()["id"]

        page.goto(base + "/operator/integrations")
        page.get_by_text("Manage editor exports", exact=True).click()
        page.get_by_label("Library destination", exact=True).select_option(
            value=folder_id
        )
        page.get_by_role("button", name="Watch for exports").click()
        expect(page.locator("#dropboxWatchStatus")).to_contain_text(
            "Watching the configured export folder."
        )

        initial = api.get(watch_url, headers=auth).json()
        assert initial["jobs"] == []
        assert initial["destination"] == folder_id

        out_dir = Path(export_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        part_path = out_dir / "New cut.part"
        final_path = out_dir / "New cut.mp4"
        assert not final_path.exists(), "refusing to overwrite existing export"
        shutil.copyfile(clip, part_path)
        part_path.rename(final_path)

        def wait_for_jobs(count):
            deadline = time.monotonic() + 12
            last = None
            while time.monotonic() < deadline:
                resp = api.get(watch_url, headers=auth)
                assert resp.status == 200, resp.text()
                last = resp.json()
                jobs = last.get("jobs", [])
                if len(jobs) == count and all(
                    job.get("state") == "imported" for job in jobs
                ):
                    return jobs
                time.sleep(0.1)
            raise AssertionError(
                f"Timed out waiting for {count} imported editor export jobs; "
                f"last={last!r}"
            )

        first = wait_for_jobs(1)[0]
        assert first["name"] == "New cut.mp4"
        assert first["work"]
        assert final_path.read_bytes() == clip.read_bytes()

        work = first["work"]
        from contextlib import closing

        with closing(sqlite3.connect(clip.parent / "workspace.sqlite")) as db:
            assert (
                db.execute(
                    "SELECT folder FROM library_placement WHERE work=?", (work,)
                ).fetchone()[0]
                == folder_id
            )
        works = json.loads((clip.parent / "manifest.json").read_text(encoding="utf8"))[
            "works"
        ]
        row = next(row for row in works if row["id"] == work)
        assert (
            row["source_upload_sha256"] == hashlib.sha256(clip.read_bytes()).hexdigest()
        )

        assert api.get(base + "/watch/work/" + work).status == 404
        page.get_by_role("button", name="Refresh watch status").click()
        page.locator("#dropboxWatch").get_by_role("link", name="New cut.mp4").click()
        video = page.locator("#workspacePreview")
        video.wait_for(state="visible")
        assert (
            video.evaluate(
                "async v => {v.muted=true; await v.play(); return v.videoWidth}"
            )
            == 320
        )

        restart()
        shutil.copyfile(clip, export_dir / "Second cut.mp4")
        jobs = wait_for_jobs(2)
        jobs_by_name = {job["name"]: job["work"] for job in jobs}
        assert set(jobs_by_name) == {"New cut.mp4", "Second cut.mp4"}
        assert jobs_by_name["New cut.mp4"] == first["work"]
        assert (export_dir / "Old export.mp4").read_bytes() == clip.read_bytes()
        page.goto(base + "/operator/integrations")
        page.get_by_role("button", name="Manage editor exports").click()
        page.get_by_role("button", name="Stop watching").click()
        expect(page.locator("#dropboxWatchStatus")).to_have_text(
            "Folder watching is off."
        )
        shutil.copyfile(clip, export_dir / "After stop.mp4")
        time.sleep(0.65)
        assert api.get(watch_url, headers=auth).json() == {"enabled": False, "jobs": []}
        assert (
            len(json.loads((clip.parent / "manifest.json").read_text())["works"]) == 2
        )
        print(
            "Editor output intake preserved source and exact bytes, played privately, resumed without duplicate imports, and stopped on request."
        )


if __name__ == "__main__":
    unittest.main()
