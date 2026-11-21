import secrets
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from time import monotonic
from unittest.mock import Mock, patch

import requests
from browser import chromium
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

import library_events
from event_media import Gateway, local_hls
from institution import create_app
from installed_audio_tracks import SAMPLE_AUDIO
from installed_events import play_event
from test_drive_import import QuietHandler, service_worker


PEER = "https://peer.example"
REMOTE = "private-fixture"
TEAM = {"X-CuePorch-Team": "1"}
WATCH = "/events/private-rehearsal"


def audio_fixture(directory):
    def run(cmd):
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *cmd],
            check=True, timeout=60,
        )

    hls = ["-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
           "-hls_segment_type", "mpegts"]

    run([
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-t", "14",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-sc_threshold", "0",
        "-an", *hls, "-hls_segment_filename", str(directory / "0-%d.ts"), str(directory / "0.m3u8"),
    ])
    for idx, freq in ((1, 440), (2, 880)):
        run([
            "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=44100",
            "-t", "14", "-c:a", "aac", "-ac", "1", "-b:a", "64k", "-vn",
            *hls, "-hls_segment_filename", str(directory / f"{idx}-%d.ts"),
            str(directory / f"{idx}.m3u8"),
        ])

    (directory / "master.m3u8").write_text("\n".join([
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",'
        'DEFAULT=YES,AUTOSELECT=YES,URI="1.m3u8"',
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Spanish",LANGUAGE="es",'
        'DEFAULT=NO,AUTOSELECT=YES,URI="2.m3u8"',
        '#EXT-X-STREAM-INF:BANDWIDTH=800000,CODECS="avc1.42c01e,mp4a.40.2",AUDIO="aud"',
        "0.m3u8",
        "",
    ]), encoding="ascii")


def hear_frequency(page, frequency):
    video = page.locator("#eventPlayer video")
    video.evaluate("video => { video.muted = false; }")
    deadline = monotonic() + 5
    measured = video.evaluate(SAMPLE_AUDIO)
    while abs(measured - frequency) > 24 and monotonic() < deadline:
        measured = video.evaluate(SAMPLE_AUDIO)
    assert abs(measured - frequency) <= 24, (measured, frequency)


class PrivateLiveEventTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "objects").mkdir()
        (self.root / "manifest.json").write_text(
            '{"institution":"station","schema":1,"works":[]}', encoding="utf-8"
        )
        self.db = self.root / "workspace.sqlite"
        operator = secrets.token_urlsafe(32)
        self.enterContext(patch.dict("os.environ", {"CUEPORCH_PUBLIC_URL": ""}))
        with patch("institution.library_worker.start", return_value=lambda: None):
            self.app = create_app(self.root, self.db, operator, "station")
        for name, stop in self.app.extensions.items():
            if name.startswith("stop_") and callable(stop):
                self.addCleanup(stop)
        self.client = self.app.test_client()
        self.auth = {"Authorization": "Bearer " + operator}

    @contextmanager
    def tx(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.row_factory = sqlite3.Row
            with con:
                yield con

    def create_event(self, slug, visibility):
        now = datetime.now(timezone.utc)
        start = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M")
        end = (now + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            "/api/live-events", headers=self.auth, json={
                "title": "Private rehearsal", "slug": slug, "visibility": visibility,
                "type": "live", "latency": "standard", "save_replay": True,
                "schedule": {"timezone": "UTC", "repeat": "none", "start": start, "end": end},
            }
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()

    def invite(self):
        response = self.client.post(
            "/api/team/invitations", headers={**self.auth, **TEAM},
            json={"email": "viewer@example.com", "role": "viewer"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()["path"].split("#", 1)[1]

    def make_media(self):
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30", "-t", "4",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30", "-sc_threshold", "0",
             "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod",
             "-hls_segment_filename", str(self.root / "0-%d.ts"), str(self.root / "0.m3u8")],
            check=True, timeout=20,
        )

    def release_replay(self, event, opens):
        host = Mock()
        approval = Mock(side_effect=("waiting", "eligible"))
        with patch("event_peer.observe", return_value={"state": "replay"}):
            for expected in ("ready", "replay"):
                library_events.publish_replays(self.tx, self.db, PEER, Mock(return_value=host), approval)
                with self.tx() as con:
                    self.assertEqual(library_events.occurrence(con, event, opens)["delivery"], expected)
        self.assertEqual(approval.call_count, 2)
        self.assertEqual(approval.call_args.args[2], "replay")
        host.set_public.assert_not_called()

    def test_private_live_and_replay_require_active_team(self):
        created = self.create_event("private-rehearsal", "private")
        missing = self.create_event("missing-publication", "public")
        event = created["id"]
        opens = created["occurrences"][0]["id"]
        with self.tx() as con:
            con.execute("UPDATE live_event SET publication=NULL WHERE id=?", (missing["id"],))
            con.execute("INSERT INTO live_occurrence VALUES (?,?,?,?,?)", (event, opens, PEER, REMOTE, "ready"))
        invitation = self.invite()
        server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=QuietHandler)
        base = f"http://127.0.0.1:{server.server_port}"
        with service_worker(server), sync_playwright() as pw, chromium(pw) as browser:
            anon = browser.new_context()
            team = browser.new_context()
            stable = base + WATCH
            self.assertEqual(anon.request.get(stable).status, 404)
            self.assertEqual(anon.request.get(stable + "/embed").status, 404)
            joined = team.request.post(base + "/api/team/join", headers=TEAM, data={
                "invitation": invitation, "name": "Viewer", "password": "viewer-password",
            })
            self.assertEqual(joined.status, 201, joined.text())
            authenticated = team.request.get(stable)
            self.assertEqual(authenticated.status, 200, "The active team member must be able to open the event")
            self.assertIn("noindex", authenticated.headers["x-robots-tag"])
            self.assertIn("no-store", authenticated.headers["cache-control"])
            self.assertEqual(team.request.get(base + "/events/missing-publication").status, 404)
            self.assertEqual(created["watch"], WATCH)
            self.assertEqual(created["embed"], WATCH + "/embed")
            self.make_media()
            metadata = {
                "isLive": True, "privacy": {"id": 3}, "state": {"id": 1},
                "streamingPlaylists": [{
                    "type": 1, "playlistUrl": f"{PEER}/static/streaming-playlists/hls/private/{REMOTE}/0.m3u8",
                }],
            }
            with patch.object(Gateway, "metadata", return_value=metadata), patch.object(
                Gateway, "media",
                side_effect=lambda origin, remote, gateway, filename, byte_range: local_hls(self.root, filename),
            ):
                page = team.new_page()
                playlist, width, _ = play_event(page, stable, "Live")
                self.assertEqual(width, 320)
                expect(page.get_by_role("combobox", name="Audio track", exact=True)).to_have_count(0)
                segment = playlist.rsplit("/", 1)[0] + "/0-0.ts"
                self.assertEqual(team.request.get(segment).status, 200)
                for address in (playlist, segment):
                    self.assertEqual(anon.request.get(address).status, 404)
                page.route(base + "/embed-fixture", lambda route: route.fulfill(
                    content_type="text/html", body=f'<iframe src="{WATCH}/embed"></iframe>'
                ))
                page.goto(base + "/embed-fixture")
                frame = page.frame_locator("iframe")
                expect(frame.locator("#eventStatus")).to_have_text("Live")
                frame.locator("video").evaluate("""video => {
                    if (video.readyState >= 2) return;
                    return new Promise(resolve => video.addEventListener("loadeddata", resolve, {once: true}));
                }""", timeout=15000)
                self.assertEqual(frame.locator("video").evaluate("video => video.videoWidth"), 320)

                self.release_replay(event, opens)
                metadata["isLive"] = False
                replay, width, _ = play_event(page, stable, "Replay")
                self.assertEqual(width, 320)
                self.assertEqual(anon.request.get(replay).status, 404)
                logout = team.request.delete(base + "/api/team/session", headers=TEAM, data={})
                self.assertEqual(logout.status, 204)
                for address in (stable, stable + "/embed", segment, replay):
                    self.assertEqual(team.request.get(address).status, 404)


    def test_private_live_audio_switching_and_replay_are_team_only(self):
        created = self.create_event("private-audio", "private")
        event, opens = created["id"], created["occurrences"][0]["id"]
        invitation = self.invite()
        audio_fixture(self.root)
        with self.tx() as con:
            con.execute("INSERT INTO live_occurrence VALUES(?,?,?,?,?)", (event, opens, PEER, REMOTE, "ready"))

        metadata = {
            "isLive": True, "privacy": {"id": 3}, "state": {"id": 1},
            "streamingPlaylists": [{
                "type": 1,
                "playlistUrl": f"{PEER}/static/streaming-playlists/hls/private/{REMOTE}/master.m3u8",
            }],
        }

        def local_read(origin, path, byte_range=None, stream=False):
            name = path.rsplit("/", 1)[-1]
            data = (self.root / name).read_bytes()
            response = requests.Response()
            response.status_code = 200
            response.headers.update({"Content-Length": str(len(data)), "Accept-Ranges": "bytes"})
            response.raw = BytesIO(data)
            response._content = data
            return response

        server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=QuietHandler)
        base = f"http://127.0.0.1:{server.server_port}"
        stable = base + "/events/private-audio"
        with (
            service_worker(server), sync_playwright() as pw,
            chromium(pw, args=["--autoplay-policy=no-user-gesture-required"]) as browser,
        ):
            team = browser.new_context()
            anon = browser.new_context()
            joined = team.request.post(base + "/api/team/join", headers=TEAM, data={
                "invitation": invitation, "name": "Audio Viewer", "password": "viewer-password",
            })
            self.assertEqual(joined.status, 201, joined.text())
            with patch.object(Gateway, "metadata", return_value=metadata), patch.object(
                Gateway, "read", side_effect=local_read,
            ):
                page = team.new_page()
                playlist, width, _ = play_event(page, stable, "Live")
                self.assertEqual(width, 320)
                page.evaluate("() => { window.__eventVideo = document.querySelector('#eventPlayer video'); }")
                track = page.get_by_role("combobox", name="Audio track")
                expect(track).to_be_visible(timeout=2500)
                expect(track.locator("option:checked")).to_have_text("English")
                hear_frequency(page, 440)
                before = page.evaluate("() => document.querySelector('#eventPlayer video').currentTime")
                track.select_option(label="Spanish")
                hear_frequency(page, 880)
                after = page.evaluate("() => document.querySelector('#eventPlayer video').currentTime")
                self.assertGreater(after, before)
                self.assertTrue(page.evaluate("() => window.__eventVideo === document.querySelector('#eventPlayer video')"))
                alt = playlist.replace("master.m3u8", "1.m3u8")
                self.assertEqual(team.request.get(alt).status, 200)
                self.assertEqual(anon.request.get(alt).status, 404)
                self.release_replay(event, opens)
                metadata["isLive"] = False
                replay, width, _ = play_event(page, stable, "Replay")
                self.assertEqual(width, 320)
                track = page.get_by_role("combobox", name="Audio track")
                expect(track).to_be_visible(timeout=2500)
                self.assertEqual(track.locator("option").all_text_contents(), ["English", "Spanish"])
                track.select_option(label="Spanish")
                hear_frequency(page, 880)
                self.assertEqual(anon.request.get(replay).status, 404)


if __name__ == "__main__":
    unittest.main()
