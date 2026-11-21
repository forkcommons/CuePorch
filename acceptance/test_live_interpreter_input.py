import json
import os
import secrets
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
from installed_events import play_event
from installed_rtmp_relay import free_port, marked_feed, relay_fixture
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
        with patch("institution.library_worker.start", return_value=lambda: None):
            app = create_app(root, root / "workspace.sqlite", operator, "station")
        for name, stop in app.extensions.items():
            if name.startswith("stop_") and callable(stop):
                cleanup.callback(stop)
        server = make_server("127.0.0.1", port, app, threaded=True, request_handler=QuietHandler)
        cleanup.enter_context(service_worker(server))
        yield app, f"http://127.0.0.1:{port}", {"Authorization": "Bearer " + operator}, certificate


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


def decoded_tone(path):
    sample = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-t", "0.5",
         "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
        check=True, capture_output=True, timeout=8,
    ).stdout
    samples = array("h")
    samples.frombytes(sample)
    assert len(samples) >= 1600
    crossings = sum((a < 0) != (b < 0) for a, b in zip(samples, samples[1:]))
    return crossings * 8000 / len(samples)


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
        assert prepared.value.status == 200
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
        with interpreter_fixture(os.environ["CUEPORCH_TEST_MEDIAMTX"]) as (app, base, auth, certificate):
            now = datetime.now(timezone.utc)
            event_response = requests.post(base + "/api/live-events", headers=auth, timeout=8, json={
                "title": "Interpreter rehearsal", "slug": "interpretation", "visibility": "private",
                "type": "live", "latency": "low", "dvr_seconds": 30,
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
                primary = requests.post(path + "/rtmp", headers=auth, timeout=8)
            self.assertEqual(primary.status_code, 200, primary.text if not primary.ok else None)
            denied = requests.get(route, timeout=8)
            self.assertIn(denied.status_code, (401, 403, 404))
            source = prepare_with_browser(base, auth, route)
            self.assertEqual(source["state"], "waiting")
            self.assertNotEqual(source["key"], primary.json()["key"])
            self.assertEqual(requests.post(route, headers=auth, json=selected, timeout=8).json(), source)

            manager = app.extensions["event_interpreter_inputs"]
            receiver = manager.get(uid, opens, "es")
            encoder = primary.json()
            invited = requests.post(base + "/api/team/invitations", headers=auth | TEAM, timeout=8,
                                    json={"email": "listener@example.com", "role": "viewer"})
            self.assertEqual(invited.status_code, 201, invited.text)
            invitation = invited.json()["path"].split("#", 1)[1]
            stable = base + event["watch"]
            with (
                marked_feed(encoder["server"], encoder["key"], "ffmpeg", certificate, "red", "320x180", 30, 440),
                patch("event_media.Gateway.metadata", return_value={
                    "isLive": True, "privacy": {"id": 3}, "state": {"id": 4},
                }),
                sync_playwright() as pw,
                chromium(pw, args=["--autoplay-policy=no-user-gesture-required"]) as browser,
            ):
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
            self.assertEqual(requests.get(route, headers=auth, timeout=8).json(), {"inputs": []})
            with publish_tone(source["server"], source["key"], certificate, 440) as revoked:
                self.assertNotEqual(revoked.wait(timeout=8), 0)

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
