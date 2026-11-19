"""Use the installed desktop to connect a real host and publish an eligible Vimeo import."""

import argparse, json, os, socket, subprocess, tempfile, threading, time
from pathlib import Path
import requests
from playwright.sync_api import expect, sync_playwright
from browser import chromium
from demo_source import build_server
from operator_walkthrough import walkthrough
from installed_trash import check_hosted
from installed_cards import check_hosted_cards
from installed_versions import check_hosted_versions, generate_replacement
from installed_migrated_privacy import check_hosted_handoff
from installed_languages import check_hosted_languages
from installed_interactivity import check_hosted_chapters
from installed_events import check_live_delivery
import psutil

p = argparse.ArgumentParser()
p.add_argument("launcher", type=Path)
p.add_argument("origin")
p.add_argument("report", type=Path)
p.add_argument("--station-package", type=Path, help="Use an actual CivicCast cable package for review defaults acceptance")
modes = p.add_mutually_exclusive_group()
modes.add_argument("--station-only", action="store_true", help="Exercise station review intake on the installed application")
modes.add_argument("--live-rtmp-port", type=int, help="Exercise event production on an isolated local RTMP listener")
a = p.parse_args()
if a.station_only and not a.station_package:
    p.error("--station-only requires --station-package")


def check_station_review(base, keys, package):
    operator = {"Authorization": "Bearer " + keys["operator"]}
    station = {
        "Authorization": "Bearer " + keys["station"],
        "Content-Type": "application/zip",
    }
    defaults = requests.put(
        base + "/api/review-defaults", headers=operator, timeout=10,
        json={"auto_create": True, "dark": True, "password": ""},
    )
    defaults.raise_for_status()

    def send():
        with package.open("rb") as content:
            response = requests.post(
                base + "/api/station-packages", headers=station, data=content,
                params={"kind": "meeting", "public": "false"}, timeout=20,
            )
        response.raise_for_status()
        return response.json()

    received = send()
    assert send()["work"] == received["work"]
    links = requests.get(
        f"{base}/api/library/work/{received['work']}/review-links", headers=operator, timeout=10,
    )
    links.raise_for_status()
    (review,) = links.json()
    assert review["dark"] and not review["password"]
    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            guest = browser.new_page()
            guest.goto(base + review["path"])
            expect(guest.locator("body")).to_have_class("review-dark")
            width = guest.locator("video").evaluate("async v => {v.muted = true; await v.play(); return v.videoWidth;}")
            assert width == 256
            expect(guest.locator("video track[kind=captions]")).to_have_count(1)
        finally:
            browser.close()
    return {"civiccast_intake_uses_saved_review_defaults": True, "repeat_does_not_duplicate_link": True, "guest_playback_width": width}


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    home = root / "data"
    with build_server(root / "fixture") as src:
        threading.Thread(target=src.serve_forever, daemon=True).start()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(
            os.environ,
            CUEPORCH_DESKTOP_PORT=str(port),
            CUEPORCH_SIMULATOR=f"http://127.0.0.1:{src.server_port}",
        )
        user = env.pop("PEERTUBE_USERNAME")
        password = env.pop("PEERTUBE_PASSWORD")
        env.pop("CUEPORCH_PEERTUBE_ORIGIN", None)
        started = time.monotonic()
        proc = subprocess.Popen(
            [str(a.launcher.resolve()), "--data", str(home), "--headless"], env=env
        )
        try:
            for _ in range(600):
                if (home / "desktop-session.json").exists():
                    break
                if proc.poll() is not None:
                    raise RuntimeError("Desktop startup failed")
                time.sleep(0.1)
            url = json.loads((home / "desktop-session.json").read_text())["url"]
            base = url.split("/desktop")[0]
            with sync_playwright() as pw:
                browser = chromium(pw)
                pg = browser.new_page()
                pg.goto(url)
                pg.get_by_label("Institution name").fill("Hosted rehearsal")
                pg.locator("summary").click()
                pg.get_by_label("Video host address").fill(a.origin)
                pg.get_by_label("Publishing account").fill(user)
                pg.get_by_label("Password", exact=True).fill(password)
                with pg.expect_response(
                    lambda response: response.url == base + "/desktop/api/setup" and response.request.method == "POST",
                    timeout=45000,
                ) as setup:
                    pg.get_by_role("button", name="Save setup").click()
                assert setup.value.ok, f"Installed host setup returned HTTP {setup.value.status}"
                pg.get_by_role("link", name="Open operator console").wait_for(
                    timeout=45000
                )
                browser.close()
            keys = json.loads((home / "operator.json").read_text())
            evidence = {"real_peertube": True}
            if a.live_rtmp_port:
                ffmpeg = a.launcher.resolve().parent / "service/_internal/media-tools/ffmpeg.exe"
                evidence["event_delivery"] = check_live_delivery(base, a.origin, keys, ffmpeg, a.live_rtmp_port)
            elif not a.station_only:
                transfer, first = walkthrough(base, keys, started)
                evidence.update(
                    source="three-item HTTP simulator",
                    inventory=3,
                    private_deliveries=2,
                    public_eligible=1,
                    first_embed_seconds=round(first, 2),
                    transfer_seconds=round(transfer, 2),
                    repeat_preserved_job_and_video_ids=True,
                    recently_deleted=check_hosted(base, a.origin, keys),
                    card_actions=check_hosted_cards(base, a.origin, keys),
                    video_versions=check_hosted_versions(
                        base, a.origin, keys, generate_replacement(root / "replacement.mp4"),
                    ),
                    hosted_languages=check_hosted_languages(base, a.origin, keys),
                    hosted_chapters=check_hosted_chapters(base, a.origin, keys),
                    hosted_privacy_handoff=check_hosted_handoff(base, a.origin, keys),
                )
            if a.station_package:
                evidence["station_review"] = check_station_review(base, keys, a.station_package)
            response = requests.post(
                base + "/desktop/api/quit",
                headers={"Authorization": "Bearer " + url.split("#")[1]},
            )
            response.raise_for_status()
            assert proc.wait(timeout=20) == 0, "The installed desktop did not exit cleanly"
            a.report.write_text(json.dumps(evidence, indent=2), encoding="utf8")
        finally:
            if proc.poll() is None:
                for child in psutil.Process(proc.pid).children(recursive=True):
                    child.terminate()
                proc.terminate()
                proc.wait()
            src.shutdown()
