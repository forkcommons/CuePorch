"""Source acceptance for concurrent simulcast and destination limits."""

import io
import json
import secrets
import shutil
import subprocess
import sys
import unittest
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import installed_simulcast as sim
from event_simulcast import Simulcast
from institution import create_app


class FixtureMedia:
    origin = "https://origin.local"

    def __init__(self, ffmpeg, root, widths):
        self.root = root
        self.remotes = {f"event-{index}": width for index, width in enumerate(widths)}
        for remote, width in self.remotes.items():
            directory = root / remote
            directory.mkdir()
            subprocess.run([
                ffmpeg, "-y", "-nostdin", "-v", "error",
                "-f", "lavfi", "-i", f"testsrc2=size={width}x180:rate=24",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
                "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
                "-c:a", "aac", "-t", "20", "-g", "24", "-sc_threshold", "0",
                "-hls_time", "1", "-hls_playlist_type", "vod",
                "-hls_segment_filename", str(directory / f"{width}-%d.ts"), str(directory / f"{width}.m3u8"),
            ], check=True, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            (directory / "master.m3u8").write_text(
                f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION={width}x180\n{width}.m3u8\n",
                encoding="ascii",
            )

    def metadata(self, origin, remote):
        assert origin == self.origin and remote in self.remotes
        return {
            "privacy": {"id": 1}, "state": {"id": 1}, "isLive": True,
            "streamingPlaylists": [{
                "type": 1, "playlistUrl": f"{origin}/static/streaming-playlists/hls/{remote}/master.m3u8",
            }],
        }

    @contextmanager
    def read(self, origin, path, stream=False):
        prefix = "/static/streaming-playlists/hls/"
        assert origin == self.origin and path.startswith(prefix)
        remote, _, name = path.removeprefix(prefix).partition("/")
        assert remote in self.remotes and name and "/" not in name
        yield SimpleNamespace(raw=io.BytesIO((self.root / remote / name).read_bytes()))


class ConcurrentSimulcast(unittest.TestCase):
    def test_three_live_events_with_twenty_destinations_on_one(self):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        self.assertTrue(ffmpeg and ffprobe, "This source check needs FFmpeg and FFprobe on PATH.")
        root = Path(self.enterContext(TemporaryDirectory()))
        media = FixtureMedia(ffmpeg, root, (320, 352, 384))
        programs = {}
        recordings = []
        with ExitStack() as receivers:
            for (remote, width), count in zip(media.remotes.items(), (20, 1, 1), strict=True):
                ports = []
                for index in range(count):
                    key = f"{remote}-{index}"
                    path = root / f"{key}.flv"
                    port = sim.free_port()
                    receivers.enter_context(sim.receive(ffmpeg, port, key, path, timeout=60))
                    ports.append((port, key))
                    recordings.append((path, width))
                programs[remote] = ports

            candidates = {}
            scheduler = Simulcast(Flask(__name__), nullcontext, media, root / "fixture.sqlite",
                                  "fixture-signing-key", lambda _: candidates)
            try:
                candidates = {
                    (remote, 1): (media.origin, remote, [
                        {"id": key, "server": f"rtmp://127.0.0.1:{port}/live",
                         "secret": scheduler.cipher.encrypt(key.encode())}
                        for port, key in ports
                    ], None)
                    for remote, ports in programs.items()
                }
                sim.wait_until(lambda: all(sim.size(path) > 12000 for path, _ in recordings), 40,
                               lambda: str([path.name for path, _ in recordings if sim.size(path) <= 12000]))
                with scheduler.lock:
                    self.assertEqual(set(candidates), set(scheduler.jobs))
            finally:
                scheduler.close()
        for path, width in recordings:
            with self.subTest(receiver=path.name):
                sim.decode(ffmpeg, path)
                dimensions = subprocess.run([
                    ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                    "-of", "csv=p=0", str(path),
                ], check=True, capture_output=True, text=True, timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(f"{width},180", dimensions.stdout.strip())


class SimulcastCapacity(unittest.TestCase):
    def setUp(self):
        cleanup = self.enterContext(ExitStack())
        root = Path(cleanup.enter_context(TemporaryDirectory()))
        (root / "objects").mkdir()
        (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
        operator = secrets.token_urlsafe(32)
        cleanup.enter_context(patch("event_peer.host_available", return_value=True))
        with patch("institution.library_worker.start", return_value=lambda: None):
            app = create_app(root, root / "workspace.sqlite", operator, "station")
        for name, stop in app.extensions.items():
            if name.startswith("stop_"):
                cleanup.callback(stop)
        self.client = app.test_client()
        self.auth = {"Authorization": f"Bearer {operator}"}

    def targets(self, slug):
        starts = datetime.now(timezone.utc) + timedelta(minutes=10)
        schedule = {
            "timezone": "UTC", "repeat": "days", "count": 2,
            "start": starts.strftime("%Y-%m-%dT%H:%M"),
            "end": (starts + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M"),
        }
        result = self.client.post("/api/live-events", headers=self.auth, json=dict(
            title=slug, slug=slug, visibility="public", type="live", latency="standard",
            save_replay=False, schedule=schedule,
        ))
        self.assertLess(result.status_code, 300, result.get_data(as_text=True))
        event = result.get_json()
        first, future = event["occurrences"]
        prefix = f"/api/live-events/{event['id']}"
        return tuple(f"{prefix}/{date['id']}/destinations" for date in (first, future))

    def test_custom_limit_applies_independently_to_recurring_events(self):
        event_ids = []
        for slug in ("capacity-a", "capacity-b"):
            first, future = self.targets(slug)
            for number in range(1, 22):
                result = self.client.post(first, headers=self.auth, json=dict(
                    name=f"Custom {number:02}", server="rtmp://127.0.0.1:1935/live",
                    key=f"fixture-private-{number:02}",
                ))
                self.assertEqual(409 if number == 21 else 200, result.status_code, result.get_data(as_text=True))
                if number == 21:
                    self.assertIn("up to 20 custom destinations", result.get_data(as_text=True))
            rosters = []
            for url in (first, future):
                result = self.client.get(url, headers=self.auth)
                self.assertEqual(200, result.status_code)
                self.assertNotIn("fixture-private-", result.get_data(as_text=True))
                roster = result.get_json()["destinations"]
                self.assertEqual([f"Custom {number:02}" for number in range(1, 21)],
                                 [item["name"] for item in roster])
                self.assertTrue(all(item["enabled"] for item in roster))
                rosters.append([item["id"] for item in roster])
            self.assertEqual(*rosters)
            event_ids.append(set(rosters[0]))
        self.assertTrue(event_ids[0].isdisjoint(event_ids[1]))

    def test_connected_limit_is_separate_from_custom(self):
        first_url, next_url = self.targets("connected-capacity")
        for i in range(20):
            response = self.client.post(first_url, headers=self.auth, json=dict(
                name=f"custom-{i}", server="rtmp://localhost/live", key=f"custom-secret-{i}",
            ))
            self.assertEqual(response.status_code, 200)

        def bound(_connection, _revision, broadcast_id, _channel_id):
            return dict(title=broadcast_id, stream=broadcast_id + "-stream",
                        server="rtmps://a.rtmps.youtube.com:443/live2", key="fixture-secret-" + broadcast_id)

        connected_ids = []
        revision = "fixture-live-revision"
        with (
            patch("youtube_live.WorkspaceConnection.connection", return_value={"revision": revision}),
            patch("youtube_live.WorkspaceConnection.revision", return_value=revision),
            patch("youtube_live.bound_target", side_effect=bound),
        ):
            for i in range(10):
                response = self.client.post(first_url + "/youtube", headers=self.auth,
                                            json=dict(broadcast=f"broadcast-{i}", channel=f"channel-{i}"))
                self.assertEqual(201, response.status_code, response.get_data(as_text=True))
                connected_ids.append(response.get_json()["id"])
            response = self.client.post(first_url + "/youtube", headers=self.auth,
                                        json=dict(broadcast="broadcast-10", channel="channel-10"))
            self.assertEqual(response.status_code, 409)
            self.assertIn("up to 10 connected destinations", response.get_data(as_text=True))
            first = self.client.get(first_url, headers=self.auth)
            future = self.client.get(next_url, headers=self.auth)
        first_destinations = first.get_json()["destinations"]
        next_destinations = future.get_json()["destinations"]
        custom_ids = [d["id"] for d in first_destinations if d["id"] not in connected_ids]
        self.assertEqual(len(first_destinations), 30)
        self.assertEqual(len(next_destinations), 20)
        self.assertEqual({d["id"] for d in first_destinations}, set(custom_ids + connected_ids))
        self.assertEqual({d["id"] for d in next_destinations}, set(custom_ids))
        self.assertEqual(len({d["id"] for d in first_destinations}), 30)
        self.assertNotIn("fixture-secret", first.get_data(as_text=True))
        self.assertNotIn("fixture-secret", future.get_data(as_text=True))
