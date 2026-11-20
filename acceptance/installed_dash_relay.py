"""Browser, upload interruption, and withdrawal acceptance for an actual DASH feed."""

from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
import json
import secrets
from statistics import median
import subprocess
from threading import Event, Thread
from time import monotonic, time
from urllib.parse import urljoin

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_rtmp_relay import DECODE_STAMPS


@contextmanager
def encoder(directory, ffmpeg):
    stop = Event()
    command = [str(ffmpeg), "-nostdin", "-loglevel", "error",
               "-f", "rawvideo", "-pixel_format", "gray", "-video_size", "320x180", "-framerate", "12", "-i", "pipe:0",
               "-f", "lavfi", "-i", "sine=frequency=550:sample_rate=48000",
               "-c:v", "libx264", "-threads", "2", "-filter_threads", "2", "-b:v", "600k",
               "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-g", "24", "-sc_threshold", "0",
               "-c:a", "aac", "-shortest", "-f", "dash", "-seg_duration", "2", "-window_size", "75",
               "-adaptation_sets", "id=0,streams=v id=1,streams=a", "feed.mpd"]
    process = subprocess.Popen(command, cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def send():
        origin = monotonic()
        frame = 0
        gray = bytes([115]) * 320 * 132
        with suppress(OSError), process.stdin:
            while not stop.is_set():
                stamp = int(time() * 10) % 65536
                bits = [1, 0, 1, 0] + [(stamp >> index) & 1 for index in range(16)]
                row = b"".join(bytes([230 if bit else 20]) * 16 for bit in bits)
                process.stdin.write(row * 48 + gray)
                frame += 1
                stop.wait(max(0, origin + frame / 12 - monotonic()))

    producer = Thread(target=send, daemon=True)
    producer.start()
    try:
        yield process
    finally:
        stop.set()
        if process.poll() is None:
            process.terminate()
        producer.join(5)
        process.wait(6)


class Sender:
    def __init__(self, path, connection, certificate):
        self.path = path
        self.manifest = connection["manifest"]
        self.headers = {"Authorization": "Bearer " + connection["token"]}
        self.certificate = certificate
        self.stopping = Event()
        self.paused_until = 0
        self.uploaded = set()
        self.pause_observed = Event()
        self.resumed = Event()
        self.error = None
        self.thread = Thread(target=self.run, daemon=True)

    def run(self):
        saved = None
        try:
            with requests.Session() as client:
                client.verify = str(self.certificate) if self.certificate else True
                while not self.stopping.is_set():
                    if self.paused_until > monotonic():
                        self.pause_observed.set()
                        self.stopping.wait(.1)
                        continue
                    for item in sorted(self.path.glob("*.m4s"), key=lambda file: file.name.startswith("chunk")):
                        if item.name in self.uploaded:
                            continue
                        result = client.put(urljoin(self.manifest, item.name), headers=self.headers,
                                            data=item.read_bytes(), timeout=6)
                        assert result.status_code == 204, f"Fragment upload rejected with HTTP {result.status_code}: {result.text}"
                        self.uploaded.add(item.name)
                        if self.pause_observed.is_set():
                            self.resumed.set()
                    mpd = self.path / "feed.mpd"
                    if mpd.is_file() and (manifest := mpd.read_bytes()) != saved:
                        result = client.put(self.manifest, headers=self.headers, data=manifest, timeout=6)
                        assert result.status_code == 204, f"Manifest upload rejected with HTTP {result.status_code}: {result.text}"
                        saved = manifest
                    self.stopping.wait(.16)
        except (OSError, AssertionError, requests.RequestException) as error:
            self.error = error

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.finish()

    def finish(self):
        self.stopping.set()
        self.thread.join(8)
        if self.error:
            raise self.error
        assert not self.thread.is_alive()


def verify_delivery(browser, base, operator, path, ffmpeg, certificate):
    now = datetime.now(timezone.utc)
    password = secrets.token_urlsafe(16)
    auth = {"Authorization": "Bearer " + operator}
    client = requests.Session()
    client.verify = str(certificate) if certificate else True
    response = client.post(base + "/api/live-events", headers=auth, timeout=15, json={
        "title": "Fault injection in MPEG-DASH", "type": "live", "latency": "fail-safe", "visibility": "password",
        "view_password": password, "save_replay": False, "dvr_seconds": 120,
        "schedule": {"timezone": "UTC", "repeat": "none",
                     "start": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                     "end": (now + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")},
    })
    response.raise_for_status()
    event = response.json()
    (slot,) = event["occurrences"]
    operation = f"{base}/api/live-events/{event['id']}/{slot['id']}"
    watch = urljoin(base, event["watch"]) + f"?occurrence={slot['id']}"
    assert client.post(operation + "/dash", timeout=8).status_code == 403
    with browser.new_context(ignore_https_errors=bool(certificate)) as staff:
        page = staff.new_page()
        page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator)});")
        page.goto(base + "/operator/live-events")
        page.get_by_role("button", name="Open production for Fault injection in MPEG-DASH", exact=False).click()
        with page.expect_response(lambda item: item.url == operation + "/dash", timeout=45000) as prepared:
            page.get_by_role("button", name="Prepare MPEG-DASH contribution").click()
        assert prepared.value.ok, f"Preparing DASH returned HTTP {prepared.value.status}"
        connection = prepared.value.json()
        assert connection["buffer_seconds"] == 60 and connection["manifest"].startswith(base + "/event-ingest/")
        assert page.locator("#dashManifest").input_value() == connection["manifest"]
        assert page.locator("#dashUploadToken").input_value() == connection["token"]
    assert client.put(connection["manifest"], data="unapproved", timeout=8).status_code == 403
    assert client.put(connection["manifest"], headers={"Authorization": "Bearer " + connection["token"]},
                      data="<no-media />", timeout=8).status_code == 422
    assert client.get(watch + "&status=1", timeout=8).headers["Content-Type"].startswith("text/html")
    outgoing = path / "encoded"
    outgoing.mkdir()
    with browser.new_context(ignore_https_errors=bool(certificate)) as audience:
        page = audience.new_page()
        page.goto(watch)
        page.get_by_label("Event password").fill(password)
        page.get_by_role("button", name="Unlock event").click()
        expect(page.locator("#eventStatus")).to_have_text("Buffering the Fail-safe signal")
        started = monotonic()
        with encoder(outgoing, ffmpeg) as process, Sender(outgoing, connection, certificate) as sender:
            expect(page.locator("#eventStatus")).to_have_text("Live", timeout=115000)
            player = page.locator("#eventPlayer video")
            player.evaluate("v => { v.muted = true; void v.play(); }")
            page.wait_for_function("document.querySelector('#eventPlayer video')?.readyState >= 2", timeout=25000)
            first_picture = round(monotonic() - started, 2)
            assert process.poll() is None and sender.error is None
            assert player.evaluate("v => v.videoWidth") == 320
            assert page.locator("#eventPlayer").get_attribute("data-transport").startswith("local:")
            signed = urljoin(base, page.locator("#eventPlayer").get_attribute("data-address"))
            master = client.get(signed, timeout=8)
            master.raise_for_status()
            assert "#EXT-X-MEDIA:TYPE=AUDIO" in master.text and "no-store" in master.headers["Cache-Control"]
            variant = client.get(urljoin(signed, "0.m3u8"), timeout=8)
            variant.raise_for_status()
            fragment = next(line for line in variant.text.splitlines() if line.endswith(".m4s"))
            assert client.get(urljoin(signed, fragment), timeout=8).ok
            assert client.get(urljoin(signed, "chunk-stream0-00030.m4s"), timeout=8).status_code == 404
            prior = player.evaluate(DECODE_STAMPS)
            start_time = player.evaluate("v => v.currentTime")
            sender.paused_until = monotonic() + 8
            assert sender.pause_observed.wait(2)
            post = player.evaluate(DECODE_STAMPS)
            assert sender.resumed.wait(12)
            advanced = player.evaluate("v => v.currentTime") - start_time
            assert advanced >= 8 and 60 <= median(prior) <= 70 and 60 <= median(post) <= 70
            deadline = monotonic() + 25
            while monotonic() < deadline:
                health = client.get(operation + "/health", headers=auth, timeout=12)
                health.raise_for_status()
                if health.json()["state"] == "live":
                    break
                Event().wait(.5)
            else:
                raise AssertionError("The connected PeerTube did not receive the delayed stream")
            sender.finish()
            client.delete(operation + "/delivery", headers=auth, timeout=35).raise_for_status()
            assert client.get(signed, timeout=8).status_code == 404
            assert client.get(urljoin(signed, fragment), timeout=8).status_code == 404
            assert client.put(connection["manifest"], headers=sender.headers,
                              data=(outgoing / "feed.mpd").read_bytes(), timeout=8).status_code == 404
    return dict(first_picture_seconds=first_picture, baseline_seconds=prior, baseline_median=median(prior),
                interruption_seconds=8, during_and_after_seconds=post, after_median=median(post),
                player_advanced_seconds=round(advanced, 2), decoded_width=320, peertube_received_delayed_relay=True,
                invalid_uploads_rejected=True, unreleased_fragment_denied=True, tls_verified_uploader=bool(certificate),
                withdrawal_revoked_view_and_ingest=True)


def check_failsafe_dash(base, keys, ffmpeg, path, certificate=None):
    with sync_playwright() as playwright, chromium(playwright) as browser:
        return verify_delivery(browser, base, keys["operator"], path, ffmpeg, certificate)
