import csv
import json
import secrets
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path
from time import monotonic
from unittest.mock import Mock, patch

import requests
from browser import chromium
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

import library_events
from event_media import Gateway, local_hls
from institution import create_app
from installed_audio_tracks import SAMPLE_AUDIO
from installed_events import play_event
from installed_recording import DISPLAY_FIXTURE
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
    deadline = monotonic() + 8
    no_signal_error = "No decoded audio signal reached the browser analyser."
    measured = None
    last_no_signal = None
    while monotonic() < deadline:
        try:
            measured = video.evaluate(SAMPLE_AUDIO)
        except PlaywrightError as error:
            if no_signal_error not in str(error).partition("\n")[0]:
                raise
            last_no_signal = error
            continue
        last_no_signal = None
        if abs(measured - frequency) <= 24:
            return
    if last_no_signal is not None:
        raise last_no_signal
    raise AssertionError((measured, frequency))


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
            landing = f"/api/live-events/{event}/landing"
            landing_payload = {
                "revision": None,
                "settings": {
                    "enabled": True,
                    "theme": "dark",
                    "layout": "split",
                    "accent": "#3266aa",
                    "show_logo": True,
                    "show_poster": True,
                },
            }
            enabled = self.client.put(landing, headers=self.auth, json=landing_payload)
            self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
            self.assertEqual(self.client.get(landing).status_code, 403)
            stale = self.client.put(landing, headers=self.auth, json=landing_payload)
            self.assertEqual(stale.status_code, 409, stale.get_data(as_text=True))
            from PIL import Image

            poster = BytesIO()
            Image.new("RGB", (160, 90), "#3266aa").save(poster, "PNG")
            poster.seek(0)
            uploaded = self.client.post(
                "/api/review-logos", headers=self.auth, data={"logo": (poster, "poster.png")},
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
            digest = uploaded.get_json()["logo"]
            artwork = self.client.put(
                f"/api/live-events/{event}/player", headers=self.auth, json={"poster": digest, "logo": digest},
            )
            self.assertEqual(artwork.status_code, 200, artwork.get_data(as_text=True))

            assert self.client.post(
                "/api/live-event-speaker-profiles",
                headers=self.auth,
                json=dict(name="Owen Lin", role="Moderator", bio="Hosts the discussion."),
            ).status_code == 201
            operator = browser.new_page()
            operator_token = self.auth["Authorization"].removeprefix("Bearer ")
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator_token)})")
            operator.goto(base + "/operator/live-events")
            production = operator.get_by_role("row").filter(
                has=operator.get_by_role("cell", name="ready", exact=True)
            )
            production.get_by_role("button", name="Open production for Private rehearsal").click()
            panel = operator.locator("#eventLanding")
            panel.get_by_text("Event landing page", exact=True).click()
            expect(panel.get_by_role("combobox", name="Theme", exact=True)).to_have_value("dark")
            panel.get_by_role("combobox", name="Layout", exact=True).select_option("stacked")
            with operator.expect_response(
                lambda r: r.request.method == "POST"
                and r.url.endswith("/api/live-event-speaker-profiles")
            ) as speaker_profile_response:
                panel.get_by_role("button", name="New speaker profile").click()
                dialog = operator.get_by_role("dialog", name="Speaker profile", exact=True)
                dialog.get_by_label("Name").fill("Mina Reed")
                dialog.get_by_label("Role").fill("Keynote")
                dialog.get_by_label("Bio").fill("Founder and keynote speaker.")
                dialog.get_by_role("button", name="Save", exact=True).click()
            assert speaker_profile_response.value.status == 201
            speaker = speaker_profile_response.value.json()
            panel.get_by_role("combobox", name="Speaker", exact=True).select_option(
                label="Mina Reed"
            )
            panel.get_by_role("button", name="Add to lineup").click()
            panel.get_by_role("combobox", name="Speaker", exact=True).select_option(label="Owen Lin")
            panel.get_by_role("button", name="Add to lineup", exact=True).click()
            panel.get_by_role("button", name="Move Owen Lin up", exact=True).click()
            panel.get_by_role("button", name="Save landing page", exact=True).click()
            expect(panel.locator("#eventLandingStatus")).to_have_text("Saved")
            panel.locator("#eventLandingLanguage").select_option("es")
            panel.locator("#eventLandingTitle").fill("Ensayo privado")
            panel.locator("#eventLandingDescription").fill("Resumen privado")
            panel.get_by_role("button", name="Save landing page", exact=True).click()
            expect(panel.locator("#eventLandingStatus")).to_have_text("Saved")
            preview_url = panel.locator("#eventLandingPreviewLink").get_attribute("href")
            r = team.request.get(preview_url)
            assert r.status == 200
            assert r.headers["content-language"] == "es"
            assert "noindex" in r.headers["x-robots-tag"]
            assert "Ensayo privado" in r.text()
            assert (anon.request.get(preview_url)).status == 404
            draft = team.request.get(stable + "?lang=es")
            assert "Private rehearsal" in draft.text()
            speaker_fields = panel.locator("#eventLandingTranslatedSpeakers").get_by_role("group", name="Mina Reed")
            speaker_fields.locator("input").first.fill("Ponente principal")
            speaker_fields.locator("textarea").fill("Presentadora del ensayo")
            panel.locator("#eventLandingTranslationPublished").check()
            panel.get_by_role("button", name="Save landing page", exact=True).click()
            expect(panel.locator("#eventLandingStatus")).to_have_text("Saved")
            pub = team.new_page()
            pub.goto(stable + "?lang=es")
            expect(pub.locator("h1")).to_have_text("Ensayo privado")
            expect(pub.get_by_role("region", name="Ponentes").get_by_text("Mina Reed", exact=True)).to_be_visible()
            expect(pub.get_by_role("region", name="Ponentes").get_by_text("Ponente principal", exact=True)).to_be_visible()
            expect(pub.get_by_role("region", name="Ponentes").get_by_text("Presentadora del ensayo", exact=True)).to_be_visible()
            pub.get_by_role("link", name="English").click()
            expect(pub.locator("h1")).to_have_text("Private rehearsal")
            stale = team.request.get(preview_url)
            assert "Private translation preview" not in stale.text()
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
                def played(response):
                    return (
                        response.url.endswith("/api/event-playback")
                        and response.request.method == "POST"
                        and response.request.post_data_json.get("played") is True
                    )

                page = team.new_page()
                with page.expect_response(played, timeout=15000) as live_sample:
                    playlist, width, _ = play_event(page, stable, "Live")
                self.assertEqual(204, live_sample.value.status)
                self.assertEqual(width, 320)
                duplicate = team.new_page()
                try:
                    with duplicate.expect_response(played, timeout=15000) as duplicate_sample:
                        _, duplicate_width, _ = play_event(duplicate, stable, "Live")
                    self.assertEqual((204, 320), (duplicate_sample.value.status, duplicate_width))
                finally:
                    duplicate.close()
                expect(page.locator("body")).to_have_attribute("data-landing-theme", "dark")
                expect(page.locator("body")).to_have_attribute("data-landing-layout", "stacked")
                hero_poster = page.locator(".event-landing-poster img")
                expect(hero_poster).to_be_visible()
                self.assertEqual(hero_poster.evaluate("async i => { await i.decode(); return i.naturalWidth }"), 160)
                self.assertEqual(anon.request.get(base + hero_poster.get_attribute("src")).status, 404)
                expect(page.locator(".event-landing-logo")).to_be_visible()
                speakers_region = page.get_by_role("region", name="Speakers")
                expect(speakers_region.get_by_text("Mina Reed")).to_be_visible()
                expect(speakers_region.get_by_text("Keynote", exact=True)).to_be_visible()
                expect(speakers_region.get_by_text("Founder and keynote speaker.", exact=True)).to_be_visible()
                expect(speakers_region.get_by_role("listitem").nth(0)).to_contain_text("Owen Lin")
                expect(speakers_region.get_by_role("listitem").nth(1)).to_contain_text("Mina Reed")
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
                expect(frame.locator(".event-custom-landing")).to_have_count(0)
                expect(frame.locator("#eventSpeakersHeading")).to_have_count(0)
                frame.locator("video").evaluate("""video => {
                    if (video.readyState >= 2) return;
                    return new Promise(resolve => video.addEventListener("loadeddata", resolve, {once: true}));
                }""", timeout=15000)
                self.assertEqual(frame.locator("video").evaluate("video => video.videoWidth"), 320)

                self.release_replay(event, opens)
                metadata.update(isLive=False, duration=4)
                with page.expect_response(
                    lambda response: played(response) and response.request.post_data_json.get("completed") is True,
                    timeout=15000,
                ) as replay_sample:
                    replay, width, _ = play_event(page, stable, "Replay")
                self.assertEqual(204, replay_sample.value.status)
                self.assertEqual(width, 320)
                endpoint = f"/api/live-events/{event}/{opens}/analytics"
                self.assertEqual(403, self.client.get(endpoint).status_code)
                counts = self.client.get(endpoint, headers=self.auth).get_json()
                self.assertEqual((2, 2, 1), tuple(counts["live"][key] for key in (
                    "plays", "identified_plays", "unique_viewers",
                )))
                self.assertEqual((1, 1, 1), tuple(counts["replay"][key] for key in (
                    "plays", "unique_viewers", "finishes",
                )))
                self.assertGreater(counts["replay"]["average_watched_fraction"], 0)
                self.assertIsNone(counts["live"]["average_watched_fraction"])
                operator.get_by_text("Audience analytics", exact=True).click()
                expect(operator.get_by_role("columnheader", name="Unique viewers")).to_be_visible()
                with operator.expect_download() as exported:
                    operator.get_by_role("button", name="Download audience analytics CSV", exact=True).click()
                contents = exported.value.path().read_text(encoding="utf-8")
                rows = list(csv.DictReader(StringIO(contents)))
                self.assertEqual([("live", "2", "1"), ("replay", "1", "1")], [
                    (row["phase"], row["plays"], row["unique_viewers"]) for row in rows
                ])
                self.assertNotIn("viewer@example.com", contents)
                expect(page.locator("body")).to_have_attribute("data-landing-theme", "dark")
                expect(page.locator("body")).to_have_attribute("data-landing-layout", "stacked")
                self.assertEqual(anon.request.get(replay).status, 404)
                public_event = self.create_event("shared-speaker-public", "public")
                saved = self.client.get(f"/api/live-events/{event}/landing", headers=self.auth).get_json()
                resp = self.client.put(
                    f"/api/live-events/{public_event['id']}/landing",
                    headers=self.auth,
                    json={"revision": None, "settings": saved["settings"]},
                )
                assert resp.status_code == 200
                resp = self.client.put(
                    f"/api/live-event-speaker-profiles/{speaker['id']}",
                    headers=self.auth,
                    json=dict(revision=speaker["revision"], name=speaker["name"], role="Curator", bio=speaker["bio"]),
                )
                assert resp.status_code == 200
                page.reload()
                expect(page.get_by_role("region", name="Speakers").get_by_text("Curator", exact=True)).to_be_visible()
                operator.reload()
                production = operator.get_by_role("row").filter(
                    has=operator.get_by_role("cell", name="replay", exact=True)
                )
                production.get_by_role("button", name="Open production for Private rehearsal").click()
                panel.get_by_text("Event landing page", exact=True).click()
                panel.locator("#eventLandingLanguage").select_option("es")
                speaker_fields = operator.locator("#eventLandingTranslatedSpeakers").get_by_role("group", name="Mina Reed")
                expect(speaker_fields.get_by_text("Translation needs updating", exact=True)).to_be_visible()
                expect(speaker_fields.locator("input").first).to_have_value("Ponente principal")
                speaker_fields.get_by_role("button", name="Mark current").click()
                expect(speaker_fields.get_by_text("Translation needs updating", exact=True)).not_to_be_visible()
                panel.get_by_role("button", name="Save landing page", exact=True).click()
                expect(panel.locator("#eventLandingStatus")).to_be_visible()
                public_watch = self.client.get(public_event["watch"])
                assert public_watch.status_code == 200 and b"Curator" in public_watch.data
                logout = team.request.delete(base + "/api/team/session", headers=TEAM, data={})
                self.assertEqual(logout.status, 204)
                for address in (stable, stable + "/embed", segment, replay):
                    self.assertEqual(team.request.get(address).status, 404)


    def test_branded_password_event_language_switch(self):
        password = secrets.token_urlsafe(18)
        now = datetime.now(timezone.utc)
        start = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M")
        end = (now + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            "/api/live-events", headers=self.auth, json={
                "title": "Private rehearsal", "slug": "branded-password",
                "visibility": "password",
                "type": "live", "latency": "standard", "save_replay": True,
                "view_password": password,
                "schedule": {
                    "timezone": "UTC", "repeat": "none",
                    "start": start, "end": end,
                },
            }
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        record = response.get_json()

        translated_title = "Repaso privado en español único"
        put_response = self.client.put(
            f"/api/live-events/{record['id']}/landing",
            headers=self.auth,
            json={
                "revision": None,
                "settings": {
                    "enabled": True, "theme": "light", "layout": "stacked",
                    "accent": "#3266aa", "show_logo": True, "show_poster": False,
                    "localization": {
                        "base": "en",
                        "translations": {
                            "es": {
                                "title": translated_title,
                                "description": "",
                                "published": True,
                            }
                        },
                    },
                },
            },
        )
        self.assertEqual(
            put_response.status_code, 200,
            put_response.get_data(as_text=True),
        )

        watch = record["watch"]
        if not watch.startswith(("http://", "https://")):
            if not watch.startswith("/"):
                watch = "/" + watch

        es_client = self.app.test_client()
        locked = es_client.get(f"{watch}?lang=es")
        self.assertEqual(locked.status_code, 200)
        locked_body = locked.get_data(as_text=True)
        self.assertIn("unlock", locked_body.lower())
        self.assertNotIn(translated_title, locked_body)
        self.assertNotIn(password, locked_body)

        unlocked = es_client.post(
            f"{watch}?lang=es", data={"password": password}
        )
        self.assertEqual(unlocked.status_code, 200)
        translated_body = unlocked.get_data(as_text=True)
        self.assertIn(translated_title, translated_body)
        self.assertNotIn(password, translated_body)
        set_cookie = unlocked.headers.get("Set-Cookie", "")
        self.assertNotIn(password, set_cookie)
        self.assertIn("HttpOnly", set_cookie)

        en_view = es_client.get(f"{watch}?lang=en")
        self.assertEqual(en_view.status_code, 200)
        en_body = en_view.get_data(as_text=True)
        self.assertNotIn("unlock", en_body.lower())
        self.assertIn("Private rehearsal", en_body)
        self.assertNotIn(translated_title, en_body)

        fresh_client = self.app.test_client()
        fresh_locked = fresh_client.get(f"{watch}?lang=es")
        self.assertEqual(fresh_locked.status_code, 200)
        fresh_body = fresh_locked.get_data(as_text=True)
        self.assertIn("unlock", fresh_body.lower())
        self.assertNotIn(translated_title, fresh_body)
        self.assertNotIn(password, fresh_body)

    def test_recording_only_from_public_event(self):
        created = self.create_event("record-only-rehearsal", "public")
        folder = self.client.post("/api/library/folders", headers=self.auth, json={
            "name": "Event recordings", "color": "#286266", "parent": None,
        })
        self.assertEqual(folder.status_code, 201, folder.get_data(as_text=True))
        folder_id = folder.get_json()["id"]
        server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=QuietHandler)
        base = f"http://127.0.0.1:{server.server_port}"
        flags = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
        with service_worker(server), sync_playwright() as pw, chromium(pw, flags) as browser:
            context = browser.new_context(permissions=["camera", "microphone"])
            context.add_init_script("(" + DISPLAY_FIXTURE + ")()")
            context.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.auth['Authorization'][7:])})")
            page = context.new_page()
            page.goto(base + "/operator/live-events")
            page.get_by_role("button", name="Open production for Private rehearsal", exact=False).first.click()
            writes = []
            opens = created["occurrences"][0]["id"]
            prefix = f"{base}/api/live-events/{created['id']}/{opens}/"

            def capture_writes(request):
                if request.method != "GET" and request.url.startswith(prefix):
                    writes.append(request.url)

            page.on("request", capture_writes)
            page.get_by_role("link", name="Recording only", exact=True).click(timeout=4000)
            expect(page.get_by_label("Production mode")).to_have_value("record")
            viewer = browser.new_page()
            viewer.goto(base + created["watch"])
            expect(viewer.locator("#eventPlayer")).to_have_attribute("data-address", "")
            expect(viewer.locator("#eventPlayer video, #eventPlayer iframe")).to_have_count(0)

            title = "Public event rehearsal take"
            page.get_by_label("Recording title").fill(title)
            page.get_by_label("Destination folder").select_option(folder_id)
            page.locator("#recordLayout").select_option("screen", timeout=5000)
            page.get_by_role("button", name="Connect camera and microphone").click()
            expect(page.locator("#recordStatus")).to_have_text("Recording devices connected.")
            page.get_by_role("button", name="Share screen", exact=True).click()
            expect(page.locator("#recordStatus")).to_have_text("Screen sharing is ready.")
            page.get_by_role("button", name="Start recording", exact=True).click()
            expect(page.locator("#recordTime")).to_have_text("0:01", timeout=5000)
            page.get_by_role("button", name="Pause", exact=True).click()
            expect(page.locator("#recordStatus")).to_have_text("Recording paused.")
            page.get_by_role("button", name="Resume", exact=True).click()
            expect(page.locator("#recordTime")).to_have_text("0:02", timeout=5000)
            page.get_by_role("button", name="Stop", exact=True).click()
            expect(page.locator("#recordStatus")).to_contain_text("Recording ready.")
            page.get_by_role("button", name="Save recording", exact=True).click()
            expect(page.locator("#recordStatus")).to_have_text("Recording saved to the library.", timeout=30000)
            link = page.get_by_role("link", name="Open saved video")
            work_id = link.get_attribute("href").rsplit("/", 1)[-1]
            works = self.client.get("/api/library", headers=self.auth).get_json()
            work = next(item for item in works if item["id"] == work_id)
            self.assertEqual((work["folder"], work["title"]), (folder_id, title))
            self.assertEqual(viewer.request.get(f"{base}/watch/work/{work_id}").status, 404)
            details = self.client.get(f"/api/library/work/{work_id}/details", headers=self.auth)
            self.assertEqual(details.status_code, 200)
            media = details.get_json()["media"]
            self.assertEqual((media["width"], media["height"]), (1280, 720))
            self.assertTrue(media["audio"])

            link.click()
            preview = page.locator("video#workspacePreview")
            expect(preview).to_be_visible()
            preview.evaluate("async video => { video.muted = true; await video.play(); }")
            page.wait_for_function("""() => {
                const video = document.querySelector('#workspacePreview');
                if (!video || video.getVideoPlaybackQuality().totalVideoFrames === 0) return false;
                const canvas = document.createElement('canvas');
                canvas.width = canvas.height = 1;
                const paint = canvas.getContext('2d');
                paint.drawImage(video, video.videoWidth / 2, video.videoHeight / 2, 1, 1, 0, 0, 1, 1);
                const [red, , blue] = paint.getImageData(0, 0, 1, 1).data;
                return video.duration >= 1.8 && blue > 200 && red < 40;
            }""")
            viewer.reload()
            expect(viewer.locator("#eventPlayer")).to_have_attribute("data-address", "")
            expect(viewer.locator("#eventPlayer video, #eventPlayer iframe")).to_have_count(0)
            self.assertEqual(writes, [])
            with self.tx() as con:
                self.assertIsNone(con.execute(
                    "SELECT 1 FROM live_occurrence WHERE event=? AND delivery!='scheduled'", (created["id"],)
                ).fetchone())

    def flush_event_playback(self, page, position, expected_caption):
        page.wait_for_function(
            "(position) => document.querySelector('#eventPlayer video').currentTime >= position + .5",
            arg=position, timeout=8000,
        )
        def paused_with_track(response):
            request = response.request
            if request.method != "POST" or not request.url.endswith("/api/event-playback"):
                return False
            body = request.post_data_json
            return body["playing"] is False and any(
                audio == "alternate:es" and caption == expected_caption and ms > 0
                for audio, caption, ms in body.get("tracks", [])
            )

        with page.expect_response(paused_with_track, timeout=8000) as paused:
            page.locator("#eventPlayer video").evaluate("video => video.pause()")
        self.assertEqual(paused.value.status, 204, paused.value.text())
        return paused.value

    def assert_track_report(self, event, streaming, caption):
        today = datetime.now(timezone.utc).date().isoformat()
        filters = {
            "from": today, "to": today, "videos": [event], "group": "audio", "breakdown": "caption",
            "streaming": streaming, "audio_kind": "alternate", "audio_language": "es",
        }
        resp = self.client.post('/api/analytics/video-report', headers=self.auth, json=filters)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertGreater(data["totals"]["watched_seconds"], 0)
        self.assertTrue(any(r["category"] == "es · Alternate audio" and r["breakdown"] == caption for r in data["rows"]))
        csv_resp = self.client.post('/api/analytics/video-report?format=csv', headers=self.auth, json=filters)
        self.assertEqual(csv_resp.status_code, 200)
        body = csv_resp.get_data(as_text=True)
        self.assertIn("Alternate audio", body)
        self.assertIn(caption, body)
        forbidden = self.client.post('/api/analytics/video-report', json=filters)
        self.assertEqual(forbidden.status_code, 403)
        return data["totals"]["watched_seconds"]

    def verify_track_ingest(self, page, response, event, watched_before):
        sample = response.request.post_data_json
        headers = {"X-CuePorch-Playback": response.request.headers["x-cueporch-playback"]}
        alternate = next(ms for audio, caption, ms in sample["tracks"] if (audio, caption) == ("alternate:es", "none"))
        fresh = sample["sequence"] + 100
        malformed = dict(sample, sequence=fresh, tracks=[["alternate:es", "none", sample["watched"] + 200000]])
        stale = dict(sample, tracks=[])
        dnt = dict(sample, sequence=fresh + 1, watched=sample["watched"] + 1000,
                   tracks=[["alternate:es", "none", alternate + 1000]])
        cases = (
            ({}, malformed, 422),
            ({}, stale, 204),
            ({"DNT": "1"}, dnt, 204),
            ({"Sec-GPC": "1"}, dict(dnt, sequence=fresh + 2), 204),
        )
        for preference, body, status in cases:
            result = page.request.post(response.url, headers=headers | preference, data=body)
            self.assertEqual(result.status, status, result.text())
            self.assertEqual(self.assert_track_report(event, "live", "Off"), watched_before)

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
                response = self.flush_event_playback(page, after, "none")
                watched = self.assert_track_report(event, "live", "Off")
                self.verify_track_ingest(page, response, event, watched)
                alt = playlist.replace("master.m3u8", "1.m3u8")
                self.assertEqual(team.request.get(alt).status, 200)
                self.assertEqual(anon.request.get(alt).status, 404)
                self.release_replay(event, opens)
                revision = secrets.token_hex(16)
                with self.tx() as con:
                    con.execute(
                        "INSERT INTO action(id,step,payload,status,decision,created) "
                        "VALUES (?, 'caption','{}','eligible','automatic',?)",
                        (revision, datetime.now(timezone.utc).timestamp()),
                    )
                    con.execute(
                        "INSERT INTO live_replay_captions VALUES (?,?,'en','ready',?,?,?)",
                        (event, opens, revision, revision,
                         json.dumps([{"start": 0, "end": 14000, "text": "Fixture caption"}])),
                    )
                metadata["isLive"] = False
                replay, width, _ = play_event(page, stable, "Replay")
                self.assertEqual(width, 320)
                track = page.get_by_role("combobox", name="Audio track")
                expect(track).to_be_visible(timeout=2500)
                self.assertEqual(track.locator("option").all_text_contents(), ["English", "Spanish"])
                page.get_by_role("button", name="Show replay captions").click()
                track.select_option(label="Spanish")
                hear_frequency(page, 880)
                replay_position = page.locator("#eventPlayer video").evaluate("video => video.currentTime")
                self.flush_event_playback(page, replay_position, "captions_ai:en")
                self.assert_track_report(event, "replay", "en · Automated captions")
                self.assertEqual(anon.request.get(replay).status, 404)


if __name__ == "__main__":
    unittest.main()
