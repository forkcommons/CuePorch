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
from installed_events import check_browser_delivery, check_live_delivery, check_program_delivery, check_registered_live, check_srt_delivery
from installed_live_captions import check_live_captions
from installed_library_recording import check_library_recording
from installed_event_playlist import check_event_playlist
from installed_event_waiting import check_event_waiting
from installed_event_controls import check_event_controls
from installed_encoder_preview import check_encoder_preview
from installed_waiting_replay import check_waiting_replay
from installed_program_graphics import check_program_graphics
from installed_replay_captions import check_replay_captions
from installed_simulcast import check_simulcast, tls_certificate
from installed_speakers import check_speaker_delivery
from installed_studio_slides import check_studio_slides
import psutil

p = argparse.ArgumentParser()
p.add_argument("launcher", type=Path)
p.add_argument("origin")
p.add_argument("report", type=Path)
p.add_argument("--setup-timeout", type=int, default=45000, help="Browser setup response timeout in milliseconds")
p.add_argument("--ffmpeg-report-directory", type=Path, help="Capture FFmpeg diagnostics for local synthetic fixtures")
p.add_argument("--station-package", type=Path, help="Use an actual CivicCast cable package for review defaults acceptance")
modes = p.add_mutually_exclusive_group()
modes.add_argument("--station-only", action="store_true", help="Exercise station review intake on the installed application")
modes.add_argument("--live-rtmp-port", type=int, help="Exercise event production on an isolated local RTMP listener")
live_modes = p.add_mutually_exclusive_group()
live_modes.add_argument("--registered-event", action="store_true", help="Test an audience-registration event instead")
live_modes.add_argument("--browser-event", action="store_true", help="Test browser capture through the installed Live Studio")
live_modes.add_argument("--scenes-event", action="store_true", help="Verify live scene changes and graphics from the installed studio")
live_modes.add_argument("--event-diagnostics", action="store_true", help="Verify live DVR and actual browser relay diagnostics")
live_modes.add_argument("--speakers-event", action="store_true", help="Verify browser guests and private speaker chat")
live_modes.add_argument("--program-graphics", action="store_true", help="Verify audience graphics in real hosted video")
live_modes.add_argument("--studio-slides", action="store_true", help="Verify PDF and PowerPoint slides in real hosted video")
live_modes.add_argument("--captions-event", action="store_true", help="Verify live captions from real encoder audio")
live_modes.add_argument("--replay-captions", action="store_true", help="Verify local captions on a hosted recording")
live_modes.add_argument("--library-replay", action="store_true", help="Verify a private Library copy of the hosted replay")
live_modes.add_argument("--replay-playlist", action="store_true", help="Verify a recurring event's watch and iframe playlist")
live_modes.add_argument("--event-waiting", action="store_true", help="Verify event poster and schedule through transition to live playback")
live_modes.add_argument("--event-controls", action="store_true", help="Verify customized live playback controls on watch and embed")
live_modes.add_argument("--encoder-preview", action="store_true", help="Verify private external RTMP and SRT previews and release")
live_modes.add_argument("--waiting-replay", action="store_true", help="Verify registered replay and live transition on the same event player")
live_modes.add_argument("--simulcast-event", action="store_true", help="Verify real RTMP and RTMPS event receivers")
p.add_argument("--caption-speech", type=Path, help="Use a spoken audio fixture for live caption acceptance")
live_modes.add_argument("--srt-event", action="store_true", help="Test encrypted SRT through the installed production controls")
live_modes.add_argument("--program-event", action="store_true", help="Test an unattended prerecorded event from installed CuePorch")
a = p.parse_args()
if a.station_only and not a.station_package:
    p.error("--station-only requires --station-package")
if (a.registered_event or a.browser_event or a.scenes_event or a.event_diagnostics or a.speakers_event or a.program_graphics or a.studio_slides or
        a.captions_event or a.replay_captions or a.library_replay or a.replay_playlist or a.event_waiting or a.event_controls or
        a.waiting_replay or a.simulcast_event or a.encoder_preview or
        a.srt_event or a.program_event) and not a.live_rtmp_port:
    p.error("Live acceptance modes require --live-rtmp-port")
if (a.captions_event or a.replay_captions) and (not a.caption_speech or not a.caption_speech.is_file()):
    p.error("Caption acceptance requires the --caption-speech audio fixture")


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
        material = tls_certificate(root) if a.simulcast_event else None
        if material:
            env["SSL_CERT_FILE"] = str(material[0])
        if a.ffmpeg_report_directory:
            env["FFREPORT"] = "file=ffmpeg-%t.log:level=48"
        started = time.monotonic()
        proc = subprocess.Popen(
            [str(a.launcher.resolve()), "--data", str(home), "--headless"], env=env,
            cwd=a.ffmpeg_report_directory,
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
                    timeout=a.setup_timeout,
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
                tool = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
                ffmpeg = a.launcher.resolve().parent / "service/_internal/media-tools" / tool
                if a.browser_event:
                    evidence["event_delivery"] = check_browser_delivery(base, keys)
                elif a.scenes_event:
                    evidence["event_delivery"] = check_browser_delivery(base, keys, (root, ffmpeg))
                elif a.event_diagnostics:
                    evidence["event_delivery"] = check_browser_delivery(base, keys, diagnostics=True)
                elif a.speakers_event:
                    evidence["event_delivery"] = check_speaker_delivery(base, keys, ffmpeg)
                elif a.program_graphics:
                    evidence["event_delivery"] = check_program_graphics(base, keys)
                elif a.studio_slides:
                    evidence["event_delivery"] = check_studio_slides(base, keys)
                elif a.replay_captions:
                    evidence["event_delivery"] = check_replay_captions(base, keys, ffmpeg, a.caption_speech.resolve())
                elif a.library_replay:
                    evidence["event_delivery"] = check_library_recording(base, keys, home, ffmpeg)
                elif a.replay_playlist:
                    evidence["event_delivery"] = check_event_playlist(base, keys, ffmpeg)
                elif a.event_waiting:
                    evidence["event_delivery"] = check_event_waiting(base, keys, ffmpeg)
                elif a.event_controls:
                    evidence["event_delivery"] = check_event_controls(base, keys, ffmpeg)
                elif a.encoder_preview:
                    evidence["event_delivery"] = check_encoder_preview(base, keys, ffmpeg, root, a.report)
                elif a.waiting_replay:
                    evidence["event_delivery"] = check_waiting_replay(base, keys, ffmpeg)
                elif a.simulcast_event:
                    evidence["event_delivery"] = check_simulcast(base, keys, ffmpeg, root, material)
                elif a.captions_event:
                    speech = a.caption_speech.resolve()
                    evidence["event_delivery"] = {
                        "public": check_live_captions(base, keys, ffmpeg, speech),
                        "registered": check_live_captions(base, keys, ffmpeg, speech, registered=True),
                    }
                elif a.srt_event:
                    evidence["event_delivery"] = check_srt_delivery(base, keys, ffmpeg)
                elif a.program_event:
                    evidence["event_delivery"] = check_program_delivery(base, keys, ffmpeg, root)
                elif a.registered_event:
                    evidence["event_delivery"] = check_registered_live(base, a.origin, keys, home, ffmpeg, a.live_rtmp_port)
                else:
                    evidence["event_delivery"] = check_live_delivery(base, a.origin, keys, home, ffmpeg, a.live_rtmp_port)
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
        except Exception:
            log = home / "service.log"
            if a.replay_captions and log.is_file():
                for detail in log.read_text(encoding="utf8", errors="replace").splitlines():
                    if "Replay caption generation failed" in detail:
                        print(detail, flush=True)
            raise
        finally:
            if proc.poll() is None:
                for child in psutil.Process(proc.pid).children(recursive=True):
                    child.terminate()
                proc.terminate()
                proc.wait()
            src.shutdown()
