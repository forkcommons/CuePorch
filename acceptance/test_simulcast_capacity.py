"""Source acceptance for concurrent simulcast and destination limits."""

import io
import json
import secrets
import shutil
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
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
        self.database = root / "workspace.sqlite"
        with patch("institution.library_worker.start", return_value=lambda: None):
            app = create_app(root, self.database, operator, "station")
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

    def test_last_connected_slot_is_held_during_provider_confirmation(self):
        first_url, next_url = self.targets("connected-slot-race")

        def bound(_connection, _revision, broadcast, _channel):
            return dict(title=broadcast, stream=broadcast + "-stream",
                        server="rtmps://a.rtmps.youtube.com:443/live2", key="fixture-private-" + broadcast)

        revision = "fixture-live-revision"
        held, release = Event(), Event()

        def fake_create(_revision, _page, _title, _opens, before_send, **_options):
            before_send()
            held.set()
            if not release.wait(10):
                raise AssertionError("The fixture did not release provider confirmation.")
            return {"id": "123456789", "server": "rtmps://live-api-s.facebook.com:443/rtmp",
                    "key": "fixture-private-fb-key"}

        with (
            patch("youtube_live.WorkspaceConnection.connection", return_value={"revision": revision}),
            patch("youtube_live.WorkspaceConnection.revision", return_value=revision),
            patch("youtube_live.bound_target", side_effect=bound),
            patch("facebook_live.PageLiveConnection.connection", return_value={"revision": "fb-rev"}),
            patch("facebook_live.PageLiveConnection.tokens",
                  return_value={"pages": {"123456": {"name": "Page One"}}}),
            patch("facebook_live.PageLiveConnection.create_page_live", side_effect=fake_create),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            for i in range(9):
                response = self.client.post(first_url + "/youtube", headers=self.auth,
                                            json=dict(broadcast=f"broadcast-{i}", channel=f"channel-{i}"))
                self.assertEqual(201, response.status_code, response.get_data(as_text=True))

            provider_client = self.client.application.test_client()
            future = executor.submit(
                provider_client.post, first_url + "/facebook", headers=self.auth, json={"page": "123456"}
            )
            try:
                self.assertTrue(held.wait(timeout=10))
                competing = self.client.post(first_url + "/youtube", headers=self.auth,
                                             json=dict(broadcast="broadcast-9", channel="channel-9"))
                separate = self.client.post(next_url + "/youtube", headers=self.auth,
                                            json=dict(broadcast="broadcast-10", channel="channel-10"))
            finally:
                release.set()
            result = future.result(timeout=10)
            self.assertEqual(
                (409, 201, 201), (competing.status_code, separate.status_code, result.status_code),
                [response.get_data(as_text=True) for response in (competing, separate, result)],
            )
            roster = self.client.get(first_url, headers=self.auth).get_json()["destinations"]
            self.assertEqual(10, len(roster))
            retry = self.client.post(first_url + "/youtube", headers=self.auth,
                                     json=dict(broadcast="broadcast-9", channel="channel-9"))
            self.assertEqual(409, retry.status_code, retry.get_data(as_text=True))
            self.assertIn("up to 10 connected destinations", retry.get_data(as_text=True))

    def test_facebook_profile_confirms_after_stopping_future_starts(self):
        first, current = self.targets("facebook-profile-ack")
        _, _, _, event_id, opening, _ = current.split("/")
        opens = int(opening)
        with closing(sqlite3.connect(self.database)) as con:
            con.execute("INSERT INTO live_occurrence VALUES (?,?,'fixture','fixture','ready')", (event_id, opens))
            con.execute("INSERT INTO workspace_integration_connection VALUES ('facebook-profile-live','fb-fixture',X'')")
            con.commit()

        profile = {"page": "707", "series": True}
        stopped = []

        def provider_create(_revision, _owner, _title, _opens, before_send, **options):
            before_send()
            response = self.client.application.test_client().delete(
                first + "/facebook-profile/auto", headers=self.auth, json=profile,
            )
            stopped.append((response.status_code, options["privacy"]))
            return {"id": "123456789", "server": "rtmps://live-api-s.facebook.com:443/rtmp", "key": "fixture-key"}

        with (
            patch("facebook_live.ProfileLiveConnection.connection", return_value={"revision": "fb-fixture"}),
            patch("facebook_live.ProfileLiveConnection.tokens",
                  return_value={"profile": {"id": "707", "name": "Profile fixture"}}),
            patch("facebook_live.ProfileLiveConnection.create_profile_live", side_effect=provider_create),
            patch("facebook_live_job.time", return_value=opens + 1),
        ):
            response = self.client.put(first + "/facebook-profile/auto", headers=self.auth,
                                       json=profile | {"privacy": "SELF"})
            self.assertEqual(202, response.status_code, response.get_data(as_text=True))
            deadline = monotonic() + 7
            while monotonic() < deadline:
                response = self.client.get(current + "/facebook-profile/auto?page=707", headers=self.auth)
                self.assertEqual(200, response.status_code, response.get_data(as_text=True))
                status = response.get_json()
                if status["state"] == "prepared":
                    break
                sleep(.05)
            self.assertEqual([(200, "SELF")], stopped)
            self.assertEqual({"state": "prepared", "series": False, "privacy": "SELF"}, status)
            roster = self.client.get(current, headers=self.auth).get_json()["destinations"]
            self.assertEqual([("facebook-profile-live", True)],
                             [(row["provider"], row["enabled"]) for row in roster])
            self.assertNotIn("fixture-key", json.dumps(roster))

    def test_facebook_profile_retries_after_rate_limit(self):
        from requests import Response

        from facebook_live_job import due
        from facebook_upload import graph_object

        first, current = self.targets("facebook-profile-throttle")
        _, _, _, event_id, opening, _ = current.split("/")
        opens = int(opening)
        with closing(sqlite3.connect(self.database)) as con:
            con.execute("INSERT INTO live_occurrence VALUES (?,?,'fixture','fixture','ready')", (event_id, opens))
            con.execute("INSERT INTO workspace_integration_connection VALUES ('facebook-profile-live','fb-fixture',X'')")
            con.commit()

        profile = {"page": "707", "series": True}
        calls = []
        clock = [opens + 1]

        def provider_create(_revision, _owner, _title, _opens, before_send, **options):
            before_send()
            calls.append((clock[0], options["privacy"]))
            if len(calls) == 1:
                rejection = Response()
                rejection.status_code = 429
                rejection.headers["Retry-After"] = "30"
                graph_object(rejection)
            return {"id": "123456789", "server": "rtmps://live-api-s.facebook.com:443/rtmp", "key": "fixture-key"}

        with (
            patch("facebook_live.ProfileLiveConnection.connection", return_value={"revision": "fb-fixture"}),
            patch("facebook_live.ProfileLiveConnection.tokens",
                  return_value={"profile": {"id": "707", "name": "Profile fixture"}}),
            patch("facebook_live.ProfileLiveConnection.create_profile_live", side_effect=provider_create),
            patch("facebook_live_job.time", side_effect=lambda: clock[0]),
        ):
            response = self.client.put(first + "/facebook-profile/auto", headers=self.auth,
                                       json=profile | {"privacy": "SELF"})
            self.assertEqual(202, response.status_code, response.get_data(as_text=True))
            deadline = monotonic() + 7
            while monotonic() < deadline:
                with closing(sqlite3.connect(self.database)) as con:
                    sending = con.execute("SELECT 1 FROM facebook_live_creation WHERE event=? AND opens=?",
                                          (event_id, opens)).fetchone()
                if calls and not sending:
                    break
                sleep(.05)
            self.assertEqual([(opens + 1, "SELF")], calls)
            self.assertIsNone(sending, "The rejected create must release its reservation")

            status_url = current + "/facebook-profile/auto?page=707"
            status = self.client.get(status_url, headers=self.auth).get_json()
            self.assertEqual({"state": "queued", "series": True, "privacy": "SELF"}, status)
            with closing(sqlite3.connect(self.database)) as con:
                self.assertEqual([], due(con, opens + 30))

            # A skipped date stays off even after Meta permits a new attempt.
            skipped = self.client.delete(current + "/facebook-profile/auto", headers=self.auth, json={"page": "707"})
            self.assertEqual(200, skipped.status_code, skipped.get_data(as_text=True))
            clock[0] = opens + 31
            with closing(sqlite3.connect(self.database)) as con:
                self.assertEqual([], due(con, clock[0]))
            restored = self.client.put(current + "/facebook-profile/auto", headers=self.auth,
                                       json={"page": "707", "restore": True})
            self.assertEqual(202, restored.status_code, restored.get_data(as_text=True))

            deadline = monotonic() + 7
            while monotonic() < deadline:
                status = self.client.get(status_url, headers=self.auth).get_json()
                if status["state"] == "prepared":
                    break
                sleep(.05)
            self.assertEqual("prepared", status["state"])
            self.assertEqual([(opens + 1, "SELF"), (opens + 31, "SELF")], calls)
            roster = self.client.get(current, headers=self.auth).get_json()["destinations"]
            self.assertEqual([("facebook-profile-live", True)],
                             [(row["provider"], row["enabled"]) for row in roster])
            self.assertNotIn("fixture-key", json.dumps(roster))

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
