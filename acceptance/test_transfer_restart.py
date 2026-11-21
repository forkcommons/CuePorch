"""Interrupt an actual source transfer and verify its saved progress and media reuse."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

import requests
from playwright.sync_api import expect, sync_playwright

from browser import chromium
from demo_source import build_server


ROOT = Path(__file__).resolve().parents[1]


def wait_for(read, description):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if result := read():
            return result
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}")


def test_restart_reuses_verified_vimeo_files(tmp_path):
    home = tmp_path / "institution"
    marker = home / "desktop-session.json"
    progress_file = home / "institution-vimeo-progress.json"
    requests_by_path = Counter()
    reached = threading.Event()
    release = threading.Event()
    fixture_token = "fixture-token-must-not-be-persisted"
    total = 121
    limited = threading.Event()
    limited.set()
    unauthorized = threading.Event()
    unauthorized.set()
    interrupted_media = threading.Event()

    with ExitStack() as cleanup:
        source = cleanup.enter_context(build_server(tmp_path / "source"))
        source.extra_inventory_count = total - 3
        original = source.RequestHandlerClass.do_GET

        def pause_first_second_video(handler):
            requests_by_path[handler.path] += 1
            if handler.path.startswith("/me/videos?") and unauthorized.is_set():
                return handler.send_error(401)
            if handler.path.startswith("/me/videos?") and limited.is_set():
                handler.send_response(429)
                handler.send_header("Retry-After", "120")
                handler.end_headers()
                return
            if handler.path.startswith("/videos/1000/versions?"):
                return handler.send_error(403)
            if handler.path == "/media/72.mp4" and requests_by_path[handler.path] == 1:
                reached.set()
                release.wait(45)
                return
            if handler.path == "/media/72.mp4" and requests_by_path[handler.path] == 2:
                return handler.send_error(410)
            if handler.path == "/media/72.mp4" and interrupted_media.is_set():
                handler.send_response(200)
                handler.send_header("Content-Length", "100")
                handler.end_headers()
                handler.wfile.write(b"partial")
                return
            original(handler)

        source.RequestHandlerClass.do_GET = pause_first_second_video
        threading.Thread(target=source.serve_forever, daemon=True).start()
        cleanup.callback(source.shutdown)
        cleanup.callback(release.set)
        source_url = f"http://127.0.0.1:{source.server_port}"
        last_marker = None

        def launch():
            nonlocal last_marker
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = dict(os.environ, CUEPORCH_DESKTOP_PORT=str(port), CUEPORCH_SIMULATOR=source_url)
            env.pop("CUEPORCH_PEERTUBE_ORIGIN", None)
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "desktop_service.py"), "--data", str(home)],
                env=env, stdout=subprocess.DEVNULL,
            )

            def stop_if_running():
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)

            cleanup.callback(stop_if_running)

            def ready():
                assert process.poll() is None, "Desktop service exited during startup"
                try:
                    value = marker.read_text(encoding="utf8")
                except (FileNotFoundError, PermissionError):
                    return None
                return value if value != last_marker else None

            last_marker = wait_for(ready, "new desktop session")
            desktop_url = json.loads(last_marker)["url"]
            shell_auth = {"Authorization": "Bearer " + desktop_url.split("#", 1)[1]}
            return process, f"http://127.0.0.1:{port}", shell_auth

        def status(base):
            response = requests.get(base + "/api/migration", headers=auth, timeout=10)
            response.raise_for_status()
            return response.json()

        def transfer(base, dry_run):
            response = requests.post(base + "/api/migration", headers=auth, timeout=10,
                                     json={"dry_run": dry_run, "token": fixture_token})
            assert response.status_code == 202, response.text

        def close(process, base, shell_auth):
            requests.post(base + "/desktop/api/quit", headers=shell_auth, timeout=10).raise_for_status()
            assert process.wait(timeout=20) == 0

        process, base, shell_auth = launch()
        requests.post(base + "/desktop/api/setup", headers=shell_auth, timeout=15,
                      json={"institution": "Restart rehearsal"}).raise_for_status()
        credentials = json.loads((home / "operator.json").read_text(encoding="utf8"))
        auth = {"Authorization": "Bearer " + credentials["operator"]}
        transfer(base, dry_run=True)
        wait_for(lambda: status(base)["state"] == "failed", "expired source credential")
        assert status(base)["detail"].startswith("Source HTTP 401.")
        unauthorized.clear()
        transfer(base, dry_run=True)
        wait_for(lambda: status(base)["state"] == "failed", "source rate limit")
        assert status(base)["detail"].startswith("Source HTTP 429.")
        limited.clear()
        transfer(base, dry_run=True)
        wait_for(lambda: status(base)["state"] == "inventoried", "Vimeo preview")
        close(process, base, shell_auth)

        process, base, shell_auth = launch()
        preview = status(base)
        assert preview["state"] == "inventoried" and len(preview["works"]) == total
        assert preview["metadata_incomplete"] == 1
        with sync_playwright() as playwright:
            browser = chromium(playwright)
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(credentials['operator'])})")
            page.goto(base + "/operator/migration")
            expect(page.locator("#rows tr")).to_have_count(100)
            expect(page.locator("#migrationSummary")).to_contain_text("119 owner file needed")
            expect(page.locator("#rows tr").filter(has_text="Archive record 1000")).to_contain_text("versions (403)")
            page.get_by_role("button", name="Next page").click()
            expect(page.locator("#rows tr")).to_have_count(21)
            expect(page.get_by_text("Page 2 of 2")).to_be_visible()
            page.get_by_role("button", name="Previous page").click()
            expect(page.locator("#rows tr")).to_have_count(100)
            browser.close()
        assert requests.get(base + "/api/migration?page=0", headers=auth, timeout=10).status_code == 400
        transfer(base, dry_run=False)
        assert reached.wait(25), "Second video request never reached the rehearsal source"
        progress = status(base)
        assert (progress["state"], progress["processed"], progress["total"]) == ("running", 1, total)
        assert (home / "institution-vimeo/checkpoints/71.json").is_file()
        process.kill()
        process.wait(timeout=10)
        release.set()

        process, base, shell_auth = launch()
        interrupted = status(base)
        assert interrupted["state"] == "interrupted" and len(interrupted["works"]) == total
        assert requests.get(base + "/api/migration", timeout=10).status_code == 403
        transfer(base, dry_run=False)
        wait_for(lambda: status(base)["state"] == "transferred", "resumed transfer")
        assert requests_by_path["/media/71.mp4"] == 1
        assert requests_by_path["/media/72.mp4"] == 3
        assert fixture_token not in progress_file.read_text(encoding="utf8")
        close(process, base, shell_auth)

        process, base, shell_auth = launch()
        completed = status(base)
        assert completed["state"] == "transferred" and len(completed["works"]) == total
        assert completed["showcases"][0]["target"]
        library = requests.get(base + "/api/library", headers=auth, timeout=10).json()
        assert {work["id"] for work in completed["works"]} == {work["id"] for work in library}
        second = next(work for work in completed["works"] if work["source_id"] == "72")
        source.second_video_revision = "2026-11-19"
        interrupted_media.set()
        transfer(base, dry_run=False)
        wait_for(lambda: status(base)["state"] == "transferred", "account transfer with one partial download")
        partial = status(base)
        assert partial["transfer_incomplete"] == 1 and len(partial["works"]) == total
        item = next(work for work in partial["works"] if work["id"] == second["id"])
        assert item["disposition"] == "owner file needed"
        assert item["transfer_issues"] == [{"role": "playback", "reason": "incomplete_media"}]
        library = requests.get(base + "/api/library", headers=auth, timeout=10).json()
        retained = next(work for work in library if work["id"] == second["id"])
        assert retained["quality"] == "playback copy only"
        with sync_playwright() as playwright:
            browser = chromium(playwright)
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(credentials['operator'])})")
            page.goto(base + "/operator/migration")
            expect(page.locator("#migrationSummary")).to_contain_text("1 with media errors")
            expect(page.locator("#rows tr").filter(has_text="Incomplete download")).to_have_count(1)
            browser.close()
        interrupted_media.clear()
        transfer(base, dry_run=False)
        wait_for(lambda: status(base)["state"] == "transferred", "retry of only the failed video")
        assert status(base)["transfer_incomplete"] == 0
        assert requests_by_path["/media/71.mp4"] == 1
        assert requests_by_path["/media/72.mp4"] == 5

        work_id = completed["works"][0]["id"]
        details_url = base + f"/api/library/work/{work_id}/details"
        details = requests.get(details_url, headers=auth, timeout=10).json()
        pages = ["https://station.example/archive/meeting"]
        requests.put(details_url, headers=auth, timeout=10,
                     json=dict(details, embed_pages=pages)).raise_for_status()
        close(process, base, shell_auth)
        export = tmp_path / "portable"
        subprocess.run([sys.executable, str(ROOT / "export_institution.py"), str(home / "collection"),
                        str(home / "institution.sqlite"), str(export)], check=True)
        exported = json.loads((export / "rights-and-routes.json").read_text(encoding="utf8"))
        assert exported["library_embed_pages"] == [{"work": work_id, "pages": pages}]
