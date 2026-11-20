"""Watch a real PeerTube event directly in its frame, then watch its public replay."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import sqlite3
from threading import Thread
import time
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_embed_builder import PublisherPage
from installed_events import send_signal, wait_for_delivery


def check_peer_event_report(base, keys, home, ffmpeg):
    auth = {"Authorization": "Bearer " + keys["operator"]}

    def api(method, path, data=None):
        response = requests.request(method, base + path, headers=auth, timeout=20, json=data)
        response.raise_for_status()
        return response.json() if response.content else None

    now = datetime.now(timezone.utc)
    event = api("POST", "/api/live-events", dict(
        title="Public hosted broadcast", slug="public-hosted-broadcast", type="live",
        latency="standard", visibility="public", save_replay=True, archive_replay=False,
        schedule=dict(timezone="UTC", repeat="none",
                      start=(now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M"),
                      end=(now + timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M"))))
    uid = event["id"]
    # Restore the settings shape of an event created before CuePorch-managed media delivery.
    with closing(sqlite3.connect(home / "institution.sqlite", timeout=15)) as database, database:
        database.execute("UPDATE live_event SET settings=json_remove(settings,'$.cueporch_delivery') WHERE id=?", (uid,))
    occurrence = event["occurrences"][0]["id"]
    path = f"/api/live-events/{uid}/{occurrence}"
    watch = base + event["watch"]
    embed = base + event["embed"]
    encoder = api("POST", path + "/encoder")

    def count(phase, **filters):
        report = api("POST", "/api/analytics/video-report", {"videos": [uid], "streaming": phase} | filters)
        return report["totals"]

    def wait_count(phase, views, **filters):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            result = count(phase, **filters)
            if result["views"] == views and (views == 0 or result["watched_seconds"] > 0):
                return result
            time.sleep(.2)
        raise AssertionError(result)

    def play(container, opt_out=False):
        frame = container.locator("#eventPlayer iframe")
        expect(frame).to_have_count(1, timeout=25000)
        address = urlsplit(frame.get_attribute("src"))
        assert address.netloc != urlsplit(base).netloc and address.path.startswith("/videos/embed/")
        assert parse_qs(address.query).get("api") == (None if opt_out else ["1"])
        content = frame.element_handle().content_frame()
        assert content
        video = content.locator("video").first
        width = video.evaluate("""video => Promise.race([(async () => {
            video.muted = true;
            await video.play();
            await new Promise(resolve => {
                function advanced() {
                    if (video.videoWidth && video.currentTime > start + 1.6) {
                        video.removeEventListener('timeupdate', advanced); resolve();
                    }
                }
                const start = video.currentTime;
                video.addEventListener('timeupdate', advanced);
            });
            video.pause();
            return video.videoWidth;
        })(), new Promise((_, reject) => setTimeout(() => reject(new Error("No decoded PeerTube playback")), 18000))])""",
                               timeout=20000)
        return width

    with sync_playwright() as pw, ThreadingHTTPServer(("127.0.0.1", 0), PublisherPage) as publisher:
        outside_origin = f"http://127.0.0.1:{publisher.server_port}"
        publisher.code = f'<iframe title="Public program" src="{embed}" width="800" height="650" allowfullscreen></iframe>'
        thread = Thread(target=publisher.serve_forever, daemon=True)
        thread.start()
        browser = chromium(pw)
        try:
            watch_page = browser.new_page()
            outside = browser.new_page()
            privacy = browser.new_page()
            with send_signal(ffmpeg, encoder["server"], encoder["key"], 59) as process:
                wait_for_delivery(base + path, auth, "live", 35, process)
                watch_page.goto(watch)
                expect(watch_page.locator("#eventStatus")).to_have_text("Live")
                # A message from the containing page is ignored beside a real player's impression.
                watch_page.evaluate("""() => {
                    for (const position of [0, .1]) {
                        window.postMessage(JSON.stringify({method: 'peertube::playbackStatusUpdate',
                            params: {position, playbackState: 'playing'}}), location.origin);
                    }
                }""")
                watch_page.wait_for_timeout(700)
                assert count("live")["views"] == 0
                assert play(watch_page) == 320
                privacy.goto(watch + "?dnt=1")
                assert play(privacy, opt_out=True) == 320
                assert wait_count("live", 1, surface="watch")["unique_viewers"] == 1
                assert process.wait(timeout=70) == 0

            print("Direct PeerTube live playback and the opt-out passed. Waiting for public replay processing.", flush=True)
            expect(watch_page.locator("#eventStatus")).to_have_text("Replay", timeout=430000)
            outside.goto(outside_origin)
            frame = outside.get_by_title("Public program").element_handle().content_frame()
            assert frame
            assert play(frame) == 320
            replay = wait_count("replay", 1, surface="embed", source=outside_origin)
            assert replay["average_watched_fraction"] > 0 and replay["unique_viewers"] == 1
            assert count("live")["views"] == count("replay")["views"] == 1
            assert count("vod")["views"] == 0
            return dict(public_direct_peer_frames=True, live_width=320, replay_width=320, live_views=1, replay_views=1,
                        replay_external_origin=True, positive_watched_seconds=True, do_not_track_played=True,
                        non_player_message_ignored=True, stable_watch_link=watch_page.url == watch)
        finally:
            browser.close()
            publisher.shutdown()
            thread.join()
