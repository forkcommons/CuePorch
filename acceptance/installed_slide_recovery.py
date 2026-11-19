"""Retain private presentation data from the installed service and reopen a restored copy."""

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import uuid

import requests
from playwright.sync_api import expect, sync_playwright
from browser import chromium


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def desktop(launcher, home):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = dict(os.environ, CUEPORCH_DESKTOP_PORT=str(port))
    environment.pop("CUEPORCH_PEERTUBE_ORIGIN", None)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with subprocess.Popen([str(launcher), "--data", str(home), "--headless"], env=environment,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags) as service:
        marker = home / "desktop-session.json"
        session = None
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if service.poll() is not None:
                    raise RuntimeError(f"The installed service exited with {service.returncode}")
                if marker.is_file():
                    session = json.loads(marker.read_text())["url"].split("#", 1)[1]
                    break
                time.sleep(.2)
            if not session:
                raise TimeoutError("The installed service did not open")
            yield f"http://127.0.0.1:{port}", {"Authorization": "Bearer " + session}
        finally:
            if service.poll() is None:
                try:
                    requests.post(f"http://127.0.0.1:{port}/desktop/api/quit",
                                  headers={"Authorization": "Bearer " + (session or "")}, timeout=10).raise_for_status()
                    assert service.wait(timeout=20) == 0
                finally:
                    if service.poll() is None:
                        service.terminate()
                        service.wait(timeout=10)


def digest(file):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def create_presentations(base, keys):
    now = datetime.now(timezone.utc)
    schedule = {key: (now + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M")
                for key, offset in (("start", -1), ("end", 90))}
    response = requests.post(base + "/api/live-events", headers=keys, timeout=15, json=dict(
        title="Presentation recovery rehearsal", visibility="public", type="webinar", latency="standard",
        schedule=dict(schedule, timezone="UTC", repeat="none"), save_replay=False))
    response.raise_for_status()
    event = response.json()
    (slot,) = event["occurrences"]
    suffix = f"{event['id']}/{slot['id']}"
    path = f"/api/live-events/{suffix}/scenes"
    slides = {}
    for extension in ("pdf", "pptx"):
        fixture = Path(__file__).with_name("fixtures") / f"studio-slides.{extension}"
        with fixture.open("rb") as source:
            result = requests.post(base + path + "/slides", headers=keys, files={"deck": (fixture.name, source)}, timeout=140)
        assert result.status_code == 201, result.text
        slides[result.json()["id"]] = fixture
    settings = requests.get(base + path, headers=keys, timeout=10).json()
    scenes = [dict(settings["templates"][0], id=uuid.uuid4().hex, title="Recovered " + file.suffix[1:].upper(),
                   presentation=identity) for identity, file in slides.items()]
    save = requests.put(base + path, headers=keys, timeout=10,
                        json=dict(revision=settings["revision"], scenes=scenes, brand=settings["brand"]))
    save.raise_for_status()
    stage = requests.post(base + path + "/slides/program", headers=keys, timeout=10, json={"deck": next(iter(slides))})
    assert stage.status_code == 201
    return suffix, scenes, slides


def verify_restored(base, suffix, scenes, digest_by_file, keys):
    path = f"/api/live-events/{suffix}/scenes"
    restored = requests.get(base + path, headers=keys, timeout=10)
    restored.raise_for_status()
    assert {row["id"]: row["presentation"] for row in restored.json()["scenes"]} == {
        scene["id"]: scene["presentation"] for scene in scenes}
    for relative, expected in digest_by_file.items():
        if relative.suffix == ".png":
            asset = f"{base}{path}/slides/{relative.parent.name}/{relative.stem}"
            assert requests.get(asset, timeout=10).status_code == 403
            response = requests.get(asset, headers=keys, timeout=10)
            response.raise_for_status()
            assert hashlib.sha256(response.content).hexdigest() == expected
    with sync_playwright() as playwright:
        browser = chromium(playwright)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['Authorization'][7:])})")
            operator.goto(f"{base}/operator/live-events/{suffix}/studio")
            panel = operator.locator("#studioScenes")
            panel.get_by_text("Scenes and graphics", exact=True).click()
            panel.get_by_role("button", name="Recovered PDF", exact=True).click()
            panel.get_by_role("button", name="Take scene").click()
            expect(panel.locator("#studioSlideNumber")).to_have_text("Slide 1 of 2")
            operator.wait_for_function("""() => {
              const canvas = document.querySelector('#recordCanvas');
              const data = canvas.getContext('2d').getImageData(210, 230, 1, 1).data;
              return data[0] > 190 && data[2] < 80;
            }""")
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launcher", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--snapshot-runner", type=Path, required=True,
                        help="Local program that runs the real deployment backup and restore using input and output directories")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="cue-slide-test-", dir=args.report.parent.resolve()) as temp:
        root = Path(temp)
        original, recovered, portable = (root / name for name in ("original", "restored", "portable"))
        with desktop(args.launcher.resolve(), original) as (base, session):
            requests.post(base + "/desktop/api/setup", headers=session, timeout=15,
                          json={"institution": "Presentation recovery rehearsal"}).raise_for_status()
            keys = {"Authorization": "Bearer " + json.loads((original / "operator.json").read_text())["operator"]}
            suffix, scenes, decks = create_presentations(base, keys)
        print("Installed source service saved and shut down with two studio presentations.", flush=True)
        files = {file.relative_to(original): digest(file) for identity in decks
                 for file in (original / "live-slide-decks" / identity).iterdir()}
        assert len(files) == 6
        for identity, fixture in decks.items():
            assert files[Path("live-slide-decks") / identity / ("source" + fixture.suffix)] == digest(fixture)
        subprocess.run([sys.executable, str(ROOT / "export_institution.py"), str(original / "collection"),
                        str(original / "institution.sqlite"), str(portable)], check=True)
        record = json.loads((portable / "rights-and-routes.json").read_text())
        assert {row["id"] for row in record["live_slide_decks"]} == decks.keys()
        recorded_hashes = json.loads((portable / "SHA256.json").read_text())
        assert all(digest(portable / file) == recorded_hashes[file.as_posix()] == expected for file, expected in files.items())
        subprocess.run([sys.executable, str(args.snapshot_runner.resolve()), str(original), str(recovered)], check=True)
        assert all(digest(recovered / file) == expected for file, expected in files.items())
        with closing(sqlite3.connect(recovered / "institution.sqlite")) as database:
            assert database.execute("SELECT count(*) FROM live_slide_program").fetchone()[0] == 0
        with desktop(args.launcher.resolve(), recovered) as (base, _):
            verify_restored(base, suffix, scenes, files, keys)
        args.report.write_text(json.dumps(dict(
            original_decks=2, portable_checksums_verified=len(files), restored_asset_checksums_verified=len(files),
            restored_scene_references=True, restored_studio_decoded_frame=True, restored_media_private=True,
            ephemeral_program_cleared=True), indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
