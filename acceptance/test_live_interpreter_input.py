import json
import os
import secrets
import socket
import subprocess
import unittest
from array import array
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urljoin

import requests
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
import event_replay_spool
from installed_events import play_event
from installed_rtmp_relay import CHECK_COLOUR, free_port, marked_feed, relay_fixture
from institution import create_app
from test_private_live_event import TEAM, hear_frequency
from test_drive_import import QuietHandler, service_worker


@contextmanager
def interpreter_fixture(binary):
    with ExitStack() as cleanup:
        root = Path(cleanup.enter_context(TemporaryDirectory()))
        (root / "objects").mkdir()
        (root / "manifest.json").write_text(
            '{"institution":"station","schema":1,"works":[]}', encoding="utf-8"
        )
        port = free_port()
        relay_env, certificate = cleanup.enter_context(relay_fixture(root, port, binary))
        cleanup.enter_context(patch.dict("os.environ", relay_env | {"CUEPORCH_PUBLIC_URL": ""}))
        operator = secrets.token_urlsafe(32)
        db = root / "workspace.sqlite"
        with patch("institution.library_worker.start", return_value=lambda: None):
            app = create_app(root, db, operator, "station")
        for name, stop in app.extensions.items():
            if name.startswith("stop_") and callable(stop):
                cleanup.callback(stop)
        server = make_server("127.0.0.1", port, app, threaded=True, request_handler=QuietHandler)
        cleanup.enter_context(service_worker(server))
        yield app, f"http://127.0.0.1:{port}", {"Authorization": "Bearer " + operator}, certificate, db


@contextmanager
def publish_tone(server, key, certificate, frequency):
    with subprocess.Popen(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi",
         "-i", f"sine=frequency={frequency}:sample_rate=48000", "-t", "40",
         "-vn", "-c:a", "aac", "-tls_verify", "1", "-ca_file", str(certificate),
         "-rw_timeout", "5000000", "-f", "flv", server + "/" + key],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as publisher:
        try:
            yield publisher
        finally:
            if publisher.poll() is None:
                publisher.terminate()
                publisher.wait(timeout=8)


def decoded_tone(path, stream=0, at=None):
    seek = ["-ss", str(at)] if at is not None else []
    sample = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", *seek, "-i", str(path),
         "-map", f"0:a:{stream}", "-t", "0.5",
         "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
        check=True, capture_output=True, timeout=8,
    ).stdout
    samples = array("h")
    samples.frombytes(sample)
    assert len(samples) >= 1600
    crossings = sum((a < 0) != (b < 0) for a, b in zip(samples, samples[1:]))
    return crossings * 8000 / len(samples)


def replay_switch_times(text):
    positions, elapsed, first = [], Fraction(0), True
    for line in text.splitlines():
        if line == "#EXT-X-DISCONTINUITY":
            first = True
        elif line.startswith("#EXTINF:"):
            duration = Fraction(line.partition(":")[2].partition(",")[0])
            if first:
                positions.append(float(elapsed + duration / 2))
                first = False
            elapsed += duration
    return positions


def segment_pts(directory, name, payload, stream):
    path = directory / name
    path.write_bytes(payload)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
         "stream=start_pts,time_base", "-of", "json", str(path)],
        check=True, capture_output=True, timeout=8,
    )
    info = json.loads(probe.stdout)["streams"][0]
    return Fraction(info["start_pts"]) * Fraction(info["time_base"])


def prepare_program(path, auth, redundant, srt):
    if not srt:
        return requests.post(path + "/rtmp", headers=auth, timeout=8, json={"backup": redundant})
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as primary,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as backup,
    ):
        primary.bind(("127.0.0.1", 0))
        backup.bind(("127.0.0.1", 0))
        payload = {"interface": "127.0.0.1", "port": primary.getsockname()[1]}
        if redundant:
            payload["backup_port"] = backup.getsockname()[1]
    return requests.post(path + "/srt", headers=auth, timeout=8, json=payload)


def prepare_with_browser(base, auth, route):
    token = auth["Authorization"].removeprefix("Bearer ")
    with sync_playwright() as pw, chromium(pw) as browser:
        page = browser.new_page()
        page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(token)});")
        page.goto(base + "/operator/live-events")
        page.get_by_role("button", name="Open production for Interpreter rehearsal", exact=False).click()
        page.locator("#interpreterPanel summary").click()
        page.locator("#interpreterLanguage").fill("es")
        page.locator("#interpreterName").fill("Spanish")
        with page.expect_response(lambda r: r.url == route and r.request.method == "POST") as prepared:
            page.get_by_role("button", name="Prepare interpreter input").click()
        assert prepared.value.status == 200, prepared.value.text()
        assert "no-store" in prepared.value.headers["cache-control"]
        source = prepared.value.json()
        key = page.get_by_label("Interpreter stream key")
        expect(key).to_have_attribute("type", "password")
        assert key.input_value() == source["key"], "The browser did not show the issued key"
        expect(page.get_by_text("Spanish — Waiting for encoder")).to_be_visible()
        return source


@unittest.skipUnless(os.environ.get("CUEPORCH_TEST_MEDIAMTX"), "Local MediaMTX fixture binary required")
class LiveInterpreterInputTest(unittest.TestCase):
    def test_rtmps_interpreter_reconnect_and_revoke(self):
        self.exercise_interpreter(manual=True)

    def test_interpreter_replay_releases_automatically_while_host_is_offline(self):
        self.exercise_interpreter(manual=False)

    def test_interpreted_replay_keeps_both_encoder_switches(self):
        self.exercise_interpreter(manual=True, redundant=True)

    def test_interpreter_audio_on_encrypted_srt_program(self):
        self.exercise_interpreter(manual=False, srt=True)

    def test_interpreter_audio_on_encrypted_srt_failover(self):
        self.exercise_interpreter(manual=False, redundant=True, srt=True)

    def exercise_interpreter(self, manual, redundant=False, *, srt=False):
        with interpreter_fixture(os.environ["CUEPORCH_TEST_MEDIAMTX"]) as (app, base, auth, certificate, db):
            now = datetime.now(timezone.utc)
            event_response = requests.post(base + "/api/live-events", headers=auth, timeout=8, json={
                "title": "Interpreter rehearsal", "slug": "interpretation", "visibility": "private",
                "type": "live", "latency": "low", "dvr_seconds": 30, "save_replay": not srt,
                "archive_replay": False,
                "schedule": {
                    "timezone": "UTC", "repeat": "none",
                    "start": (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M"),
                    "end": (now + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M"),
                },
            })
            self.assertEqual(event_response.status_code, 201, event_response.text)
            event = event_response.json()
            uid, opens = event["id"], event["occurrences"][0]["id"]
            path = f"{base}/api/live-events/{uid}/{opens}"
            route = path + "/interpreters"
            selected = {"language": "es", "name": "Spanish"}
            self.assertEqual(requests.post(route, headers=auth, json=selected, timeout=8).status_code, 409)
            peer = SimpleNamespace(origin="https://peer.example")
            with (
                patch("event_peer.connected_host", side_effect=lambda *_: nullcontext(peer)),
                patch("event_peer.stage_remote", return_value="fixture-delivery"),
                patch("event_peer.release_remote"),
                patch("event_peer.observe", return_value={"state": "waiting_for_encoder"}),
                patch("event_peer.encoder", return_value={
                    "server": "rtmp://127.0.0.1:1/live", "key": "synthetic-primary",
                }),
            ):
                primary = prepare_program(path, auth, redundant, srt)
            self.assertEqual(primary.status_code, 200, primary.text if not primary.ok else None)
            denied = requests.get(route, timeout=8)
            self.assertIn(denied.status_code, (401, 403, 404))
            source = prepare_with_browser(base, auth, route)
            self.assertEqual(source["state"], "waiting")
            encoder = primary.json()
            program_key = encoder["passphrase"] if srt else encoder["key"]
            self.assertNotEqual(source["key"], program_key)
            self.assertEqual(requests.post(route, headers=auth, json=selected, timeout=8).json(), source)

            manager = app.extensions["event_interpreter_inputs"]
            receiver = manager.get(uid, opens, "es")
            invited = requests.post(base + "/api/team/invitations", headers=auth | TEAM, timeout=8,
                                    json={"email": "listener@example.com", "role": "viewer"})
            self.assertEqual(invited.status_code, 201, invited.text)
            invitation = invited.json()["path"].split("#", 1)[1]
            stable = base + event["watch"]
            with (
                ExitStack() as feeds,
                patch("event_media.Gateway.metadata", return_value={
                    "isLive": True, "privacy": {"id": 3}, "state": {"id": 4},
                }),
                sync_playwright() as pw,
                chromium(pw, args=["--autoplay-policy=no-user-gesture-required"]) as browser,
            ):
                primary_feed = feeds.enter_context(
                    marked_feed(encoder["server"], program_key, "ffmpeg", certificate, "red", "320x180", 30, 440)
                )
                team = browser.new_context()
                joined = team.request.post(base + "/api/team/join", headers=TEAM, data={
                    "invitation": invitation, "name": "Listener", "password": "listener-password",
                })
                self.assertEqual(joined.status, 201, joined.text())
                deadline = monotonic() + 15
                while monotonic() < deadline and not receiver.mixer.primary.live():
                    sleep(.2)
                page = team.new_page()
                address, _, _ = play_event(page, stable, "Live")
                self.assertTrue(address.endswith("/0.m3u8"))
                hear_frequency(page, 440)
                track = page.get_by_role("combobox", name="Audio track")
                with publish_tone(source["server"], source["key"], certificate, 880):
                    first_run = self.assert_received(route, auth, receiver, 880)
                    expect(track).to_be_visible(timeout=20000)
                    self.assertEqual(track.locator("option:checked").text_content(), "Original")
                    hear_frequency(page, 440)
                    track.select_option(label="Spanish")
                    hear_frequency(page, 880)
                    if redundant:
                        self.assert_failover(feeds, page, track, primary_feed, encoder, certificate, path, auth, team)
                    if srt:
                        self.assert_srt_stopped(path, route, auth, receiver, team, page, source, certificate)
                        return
                    if redundant:
                        requests.put(base + "/api/policy/replay", headers=auth,
                                     json={"required": True}, timeout=8).raise_for_status()
                        offline = requests.ConnectionError("The fixture's connected host is offline.")
                        with patch("event_media.Gateway.metadata", side_effect=offline):
                            requests.delete(path + "/rtmp", headers=auth, timeout=8).raise_for_status()
                            self.assert_replay(db, base, auth, stable, path, uid, opens, team, page, manual,
                                               redundant=True)
                        return
                    player = page.locator("#eventPlayer")
                    master = urljoin(stable, player.get_attribute("data-address"))
                    alternate = urljoin(master, "1.m3u8")
                    playlist = team.request.get(alternate)
                    self.assertEqual(playlist.status, 200)
                    name = [line for line in playlist.text().splitlines() if line.endswith(".ts")][-1]
                    segment = urljoin(alternate, name)
                    audio = team.request.get(segment)
                    video_name = "0-" + name[2:]
                    video = team.request.get(urljoin(alternate, video_name))
                    self.assertEqual(audio.status, 200)
                    self.assertEqual(video.status, 200)
                    with TemporaryDirectory() as directory:
                        audio_pts = segment_pts(Path(directory), name, audio.body(), "a:0")
                        video_pts = segment_pts(Path(directory), video_name, video.body(), "v:0")
                        self.assertAlmostEqual(float(audio_pts), float(video_pts), delta=.002)
                    self.assertEqual(requests.get(alternate, timeout=8).status_code, 404)
                    self.assertEqual(requests.get(segment, timeout=8).status_code, 404)
                with publish_tone(source["server"], source["key"], certificate, 660) as publishing:
                    self.assert_received(route, auth, receiver, 660, previous=first_run)
                    hear_frequency(page, 660)
                    self.assertEqual(requests.delete(route + "/es", headers=auth, timeout=8).status_code, 204)
                    self.assertNotEqual(publishing.wait(timeout=11), 0)
                self.assertEqual(team.request.get(alternate).status, 404)
                expect(track).to_be_hidden(timeout=12000)
                hear_frequency(page, 440)
                self.assertFalse(requests.get(base + "/api/policy", headers=auth, timeout=8).json()["replay"])
                if manual:
                    requests.put(base + "/api/policy/replay", headers=auth,
                                 json={"required": True}, timeout=8).raise_for_status()
                offline = requests.ConnectionError("The fixture's connected host is offline.")
                with patch("event_media.Gateway.metadata", side_effect=offline):
                    self.assertEqual(requests.delete(path + "/rtmp", headers=auth, timeout=8).status_code, 204)
                    self.assert_replay(db, base, auth, stable, path, uid, opens, team, page, manual)
            self.assertEqual(requests.get(route, headers=auth, timeout=8).json(), {"inputs": []})
            with publish_tone(source["server"], source["key"], certificate, 440) as revoked:
                self.assertNotEqual(revoked.wait(timeout=8), 0)

    def assert_srt_stopped(self, path, route, auth, receiver, team, page, source, certificate):
        master = urljoin(page.url, page.locator("#eventPlayer").get_attribute("data-address"))
        signed = urljoin(master, "1.m3u8")
        self.assertEqual(team.request.get(signed).status, 200)
        self.assertEqual(requests.delete(path + "/srt", headers=auth, timeout=8).status_code, 204)
        self.assertTrue(receiver.done.wait(9))
        self.assertEqual(requests.get(route, headers=auth, timeout=8).json(), {"inputs": []})
        self.assertEqual(team.request.get(signed).status, 404)
        with publish_tone(source["server"], source["key"], certificate, 440) as revoked:
            self.assertNotEqual(revoked.wait(timeout=8), 0)

    def assert_failover(self, feeds, page, track, primary, encoder, certificate, path, auth, team):
        page.wait_for_function(CHECK_COLOUR, arg=0, timeout=25000)
        video = page.locator("#eventPlayer video")
        video.evaluate("el => { window.originalVideo = el; }")
        backup_server = encoder.get("backup_server", encoder["server"])
        backup_key = encoder.get("backup_passphrase") or encoder["backup_key"]
        backup = feeds.enter_context(marked_feed(backup_server, backup_key, "ffmpeg", certificate,
                                                 "blue", "640x360", 25, 620))
        deadline = monotonic() + 15
        while monotonic() < deadline:
            health = requests.get(path + "/health?source=relay", headers=auth, timeout=8).json()
            if health["encoder"]["inputs"]["backup"]:
                break
            sleep(.25)
        else:
            self.fail("The independent backup encoder did not reach the program.")
        primary.terminate()
        primary.wait(timeout=6)
        page.wait_for_function(CHECK_COLOUR, arg=2, timeout=25000)
        hear_frequency(page, 880)
        self.assertTrue(video.evaluate("el => el === window.originalVideo"))
        self.assertEqual(track.locator("option:checked").text_content(), "Spanish")
        program_key = encoder.get("passphrase") or encoder["key"]
        feeds.enter_context(marked_feed(encoder["server"], program_key, "ffmpeg", certificate,
                                        "green", "480x270", 20, 520))
        deadline = monotonic() + 15
        while monotonic() < deadline:
            health = requests.get(path + "/health?source=relay", headers=auth, timeout=8).json()
            inputs = health["encoder"]["inputs"]
            if inputs["primary"] and inputs["selected"] == "backup":
                break
            sleep(.25)
        else:
            self.fail("The returning primary encoder did not warm up behind the active backup.")
        backup.terminate()
        backup.wait(timeout=6)
        page.wait_for_function(CHECK_COLOUR, arg=1, timeout=25000)
        hear_frequency(page, 880)
        self.assertTrue(video.evaluate("el => el === window.originalVideo"))
        self.assertEqual(track.locator("option:checked").text_content(), "Spanish")
        address = urljoin(page.url, page.locator("#eventPlayer").get_attribute("data-address"))
        paired = [team.request.get(urljoin(address, name)) for name in ("1.m3u8", "2.m3u8")]
        for manifest in paired:
            self.assertEqual(manifest.status, 200)
            self.assertEqual(manifest.text().splitlines().count("#EXT-X-DISCONTINUITY"), 2)
        self.assertEqual([line for line in paired[0].text().splitlines() if line.startswith("#EXT-X-DISCONTINUITY")],
                         [line for line in paired[1].text().splitlines() if line.startswith("#EXT-X-DISCONTINUITY")])

    def assert_replay(self, db, base, auth, stable, path, uid, opens, team, page, manual, redundant=False):
        archive = None
        deadline = monotonic() + 25
        while archive is None and monotonic() < deadline:
            archive = event_replay_spool.available(db.parent / "interpreted-replays", uid, opens, "fixture-delivery")
            if archive is None:
                sleep(.25)
        self.assertIsNotNone(archive)
        segments = sorted(archive.directory.glob("1-*.ts"))
        self.assertGreater(len(segments), 5)
        self.assertAlmostEqual(decoded_tone(segments[0], at=.15), 440, delta=28)
        interpretation = next(index for index, segment in enumerate(segments)
                              if abs(decoded_tone(segment) - 880) < 28)
        gate = f"{base}/api/actions/live-replay-{uid}-{opens}"
        deadline = monotonic() + 12
        while monotonic() < deadline:
            response = requests.get(gate, headers=auth, timeout=8)
            if response.status_code == 200:
                approval = response.json()
                self.assertEqual(approval["status"], "waiting" if manual else "eligible")
                self.assertEqual(approval["decision"], None if manual else "automatic")
                break
            self.assertEqual(response.status_code, 404)
            sleep(.25)
        else:
            self.fail("The local replay did not reach the operator approval queue while the host was offline.")
        captions = f"{stable}/{opens}/replay-captions"
        page.goto(stable)
        if manual:
            expect(page.locator("#eventStatus")).to_have_text("The replay is awaiting release")
            expect(page.locator("#eventPlayer video")).to_have_count(0)
            self.assertEqual(requests.get(path + "/health", headers=auth, timeout=8).json()["state"], "preparing_replay")
            media = self.retained_copy(db, base, auth, path)
            if not redundant:
                self.assertAlmostEqual(decoded_tone(media, 0, interpretation + .15), 440, delta=28)
                self.assertAlmostEqual(decoded_tone(media, 1, interpretation + .15), 880, delta=28)
            self.assertEqual(team.request.get(captions).status, 404)
            if not redundant:
                self.assert_caption_input(auth, path, interpretation + .15)
            requests.post(gate + "/decision", headers=auth, json={"decision": "approve"}, timeout=8).raise_for_status()
        expect(page.locator("#eventStatus")).to_have_text("Replay", timeout=12000)
        replay, width, duration = play_event(page, stable, "Replay", navigate=False)
        self.assertEqual(requests.get(path + "/health", headers=auth, timeout=8).json()["state"], "replay")
        if manual and not redundant:
            self.assertEqual(team.request.get(captions).json()["cues"][0]["text"], "Original program caption")
        self.assertEqual(width, 1280 if redundant else 320)
        self.assertAlmostEqual(duration, archive.duration, delta=.5)
        hear_frequency(page, 440)
        track = page.get_by_role("combobox", name="Audio track")
        track.select_option(label="Spanish")
        if redundant:
            self.assert_replay_switches(page, team, replay, media, archive.duration)
        else:
            page.locator("#eventPlayer video").evaluate("(video, at) => { video.currentTime = at; }", interpretation + .15)
            hear_frequency(page, 880)
        alternate = urljoin(replay, "1.m3u8")
        self.assertEqual(requests.get(alternate, timeout=8).status_code, 404)
        with patch("event_peer.connected_host", side_effect=requests.ConnectionError("The host remains offline.")):
            self.assertEqual(requests.delete(path + "/delivery", headers=auth, timeout=8).status_code, 502)
        self.assertEqual(team.request.get(alternate).status, 404)

    def assert_replay_switches(self, page, team, replay, media, duration):
        switches = []
        for name in ("1.m3u8", "2.m3u8"):
            response = team.request.get(urljoin(replay, name))
            self.assertEqual(response.status, 200)
            switches.append(replay_switch_times(response.text()))
        self.assertEqual(switches[0], switches[1])
        self.assertEqual(len(switches[0]), 3)
        actual = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(media),
        ], timeout=8)
        self.assertAlmostEqual(float(actual), duration, delta=.3)
        video = page.locator("#eventPlayer video")
        for at, colour, program, interpretation in zip(switches[0], (0, 2, 1),
                                                        (440, 620, 520), (440, 880, 880), strict=True):
            video.evaluate("(video, at) => { video.currentTime = at; }", at)
            page.wait_for_function(CHECK_COLOUR, arg=colour, timeout=10000)
            hear_frequency(page, interpretation)
            rgb = subprocess.check_output([
                "ffmpeg", "-nostdin", "-v", "error", "-ss", str(at), "-i", str(media),
                "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
            ], timeout=8)
            self.assertEqual(len(rgb), 3, (at, rgb))
            self.assertEqual(max(range(3), key=rgb.__getitem__), colour, (at, rgb))
            self.assertAlmostEqual(decoded_tone(media, 0, at), program, delta=28)
            self.assertAlmostEqual(decoded_tone(media, 1, at), interpretation, delta=28)

    def assert_caption_input(self, auth, path, at):
        def transcribe(source, output, language, stopping):
            self.assertEqual(language, "en")
            self.assertAlmostEqual(decoded_tone(source, 0, at), 440, delta=28)
            return b"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nOriginal program caption\n", b"{}"

        with patch("caption_adapter.transcribe_path", side_effect=transcribe) as adapter:
            requests.put(path + "/captions", headers=auth, json={"enabled": True, "language": "en"},
                         timeout=8).raise_for_status()
            deadline = monotonic() + 12
            while monotonic() < deadline:
                state = requests.get(path + "/replay-captions", headers=auth, timeout=8).json()
                self.assertNotEqual(state["state"], "failed", state)
                if state["has_track"]:
                    break
                sleep(.25)
            else:
                self.fail("The archive did not supply local program audio for replay captions.")
            adapter.assert_called_once()

    def retained_copy(self, db, base, auth, path):
        endpoint = path + "/library-recording"
        requests.post(endpoint, headers=auth, timeout=8).raise_for_status()
        deadline = monotonic() + 20
        while monotonic() < deadline:
            state = requests.get(endpoint, headers=auth, timeout=8).json()
            self.assertNotEqual(state["state"], "failed", state)
            if state["state"] == "ready":
                break
            sleep(.25)
        else:
            self.fail("The interpreted replay did not reach the Library.")
        work = requests.get(base + "/api/library/work/" + state["work"], headers=auth, timeout=8).json()
        self.assertEqual(work["source_privacy"], "private")
        digest = next(item["sha256"] for item in work["files"] if item["role"] == "playback")
        return db.parent / "objects" / digest

    def assert_received(self, route, auth, receiver, frequency, previous=None):
        deadline = monotonic() + 15
        while monotonic() < deadline:
            current = requests.get(route, headers=auth, timeout=8).json()["inputs"]
            path = receiver.path
            if current and current[0]["state"] == "receiving" and path and path != previous:
                segments = sorted(path.glob("1-*.ts"))
                if segments:
                    self.assertAlmostEqual(decoded_tone(segments[-1]), frequency, delta=28)
                    return path
            sleep(.25)
        self.fail("Interpreter audio did not arrive through RTMPS")
