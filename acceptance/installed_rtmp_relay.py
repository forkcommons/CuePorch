"""Exercise the CuePorch Low transport with real RTMP encoders, browsers and MediaMTX."""

from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
from statistics import median
import subprocess
from threading import Event, Thread
from time import monotonic, time
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_events import wait_for_delivery
from installed_simulcast import free_port, tls_certificate


@contextmanager
def timed_encoder(server, key, ffmpeg, certificate):
    """Stamp the source clock in the picture before sending it over RTMP."""
    stop = Event()
    tls = ["-tls_verify", "1", "-ca_file", str(certificate)] if server.startswith("rtmps://") else []
    command = [str(ffmpeg), "-nostdin", "-loglevel", "error",
               "-f", "rawvideo", "-pixel_format", "gray", "-video_size", "320x180", "-framerate", "12", "-i", "pipe:0",
               "-f", "lavfi", "-i", "sine=frequency=550:sample_rate=48000",
               "-c:v", "libx264", "-threads", "2", "-filter_threads", "2",
               "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-g", "12", "-sc_threshold", "0",
               "-c:a", "aac", "-shortest", *tls, "-f", "flv", server + "/" + key]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def send():
        started = monotonic()
        frame = 0
        gray = bytes([115]) * 320 * 132
        with suppress(OSError), process.stdin:
            while not stop.is_set():
                stamp = int(time() * 10) % 65536
                bits = [1, 0, 1, 0] + [(stamp >> index) & 1 for index in range(16)]
                row = b"".join(bytes([230 if bit else 20]) * 16 for bit in bits)
                process.stdin.write(row * 48 + gray)
                frame += 1
                stop.wait(max(0, started + frame / 12 - monotonic()))

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


def disconnected(process, timeout):
    try:
        return process.wait(timeout=timeout) != 0
    except subprocess.TimeoutExpired:
        raise AssertionError("The relay kept the publishing connection open") from None


DECODE_STAMPS = """async v => {
  const canvas = document.createElement('canvas');
  canvas.width = 320;
  canvas.height = 48;
  const context = canvas.getContext('2d', {willReadFrequently: true});
  const measurements = [];
  let last = -1;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('Too few advancing frames with an identifiable source stamp')), 25000);
    function read(_, frame) {
      if (frame.mediaTime >= last + .95) {
        context.drawImage(v, 0, 0, v.videoWidth, v.videoHeight * 48 / 180, 0, 0, 320, 48);
        const pixels = context.getImageData(0, 23, 320, 1).data;
        const bits = Array.from({length: 20}, (_, index) => Number(pixels[(index * 16 + 8) * 4] > 125));
        if (bits.slice(0, 4).join('') === '1010') {
          const stamp = bits.slice(4).reduce((sum, bit, index) => sum + bit * 2 ** index, 0);
          const now = Math.floor(Date.now() / 100) % 65536;
          const delay = (now - stamp + 65536) % 65536 / 10;
          if (delay > 0 && delay < 180) {
            measurements.push(delay);
            last = frame.mediaTime;
            if (measurements.length === 10) {
              clearTimeout(timer);
              resolve(measurements);
              return;
            }
          }
        }
      }
      v.requestVideoFrameCallback(read);
    }
    v.requestVideoFrameCallback(read);
  });
}"""


def measure(browser, base, keys, ffmpeg, connection, certificate):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    protocol = urlsplit(connection).scheme
    title = f"Low latency from {protocol.upper()}"
    now = datetime.now(timezone.utc)
    password = secrets.token_urlsafe(16)
    event = {
        "title": title, "visibility": "password", "view_password": password, "type": "live",
        "latency": "low", "save_replay": False, "dvr_seconds": 120,
        "schedule": {
            "timezone": "UTC", "repeat": "none",
            "start": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
            "end": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M"),
        },
    }
    portal = base + "/api/live-events"
    response = requests.post(portal, headers=auth, json=event, timeout=12)
    response.raise_for_status()
    created = response.json()
    (slot,) = created["occurrences"]
    production = f"{portal}/{created['id']}/{slot['id']}"
    stable = urljoin(base, created["watch"]) + f"?occurrence={slot['id']}"
    settings = portal + "/" + created["id"] + "/encoder-settings"
    requests.put(settings, headers=auth, json={"preview": True}, timeout=8).raise_for_status()
    assert requests.post(production + "/rtmp", headers=auth, timeout=8).status_code == 409
    requests.put(settings, headers=auth, json={"preview": False}, timeout=8).raise_for_status()
    assert requests.post(production + "/rtmp", timeout=8).status_code == 403
    operator = browser.new_page()
    try:
        operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])});")
        operator.goto(base + "/operator/live-events")
        operator.get_by_role("button", name=f"Open production for {title}", exact=False).click()
        with operator.expect_response(lambda value: value.url == production + "/rtmp", timeout=45000) as prepared:
            operator.get_by_role("button", name="Prepare low-latency RTMP").click()
        assert prepared.value.ok, f"Preparing the low-latency relay returned HTTP {prepared.value.status}"
        destination = prepared.value.json()
        assert "no-store" in prepared.value.headers["cache-control"]
        expect(operator.locator("#encoderSettings")).to_be_visible()
        assert operator.locator("#rtmpServer").input_value() == destination["server"] == connection + "/cueporch"
        assert operator.locator("#encoderKey").input_value() == destination["key"]
    finally:
        operator.close()
    assert requests.post(production + "/rtmp", headers=auth, timeout=8).json() == destination
    assert requests.post(base + "/api/internal/rtmp/authorize", json={}, timeout=8).status_code == 404
    with timed_encoder(destination["server"], destination["key"] + "invalid", ffmpeg, certificate) as invalid:
        assert disconnected(invalid, 18)
    assert requests.get(production + "/health?source=relay", headers=auth, timeout=8).json()["encoder"]["source"] == "rtmp"
    assert requests.get(stable + "&status=1", timeout=8).headers["Content-Type"].startswith("text/html")
    page = browser.new_page()
    try:
        page.goto(stable)
        page.get_by_label("Event password").fill(password)
        page.get_by_role("button", name="Unlock event").click()
        expect(page.locator("#eventStatus")).to_have_text("Waiting for the encoder")
        started = monotonic()
        with timed_encoder(destination["server"], destination["key"], ffmpeg, certificate) as encoder:
            expect(page.locator("#eventStatus")).to_have_text("Live", timeout=65000)
            player = page.locator("#eventPlayer video")
            player.evaluate("video => { video.muted = true; void video.play(); }")
            page.wait_for_function("document.querySelector('#eventPlayer video')?.readyState >= 2", timeout=100000)
            first_frame = round(monotonic() - started, 1)
            assert page.locator("#eventPlayer").get_attribute("data-transport").startswith("local:")
            manifest = page.locator("#eventPlayer").get_attribute("data-address")
            playlist = requests.get(urljoin(base, manifest), timeout=5)
            playlist.raise_for_status()
            assert "no-store" in playlist.headers["Cache-Control"].split(", "), playlist.headers
            segment = next(line for line in playlist.text.splitlines() if line.endswith(".ts"))
            assert requests.get(urljoin(base + manifest, segment), timeout=5).ok
            assert requests.get(base + "/event-media/invalid/0.m3u8", timeout=5).status_code == 404
            measurements = player.evaluate(DECODE_STAMPS)
            wait_for_delivery(production, auth, "live", 100, encoder)
            delivery = requests.get(production + "/health", headers=auth, timeout=12).json()["delivery_settings"]
            assert delivery["latency"] == "low", delivery
            assert requests.post(production + "/rtmp", headers=auth, timeout=8).json() == destination
            width = player.evaluate("video => video.videoWidth")
            previous = player.evaluate("""video => {
              video.pause();
              const start = video.seekable.start(video.seekable.length - 1);
              video.currentTime = Math.max(start + .5, video.currentTime - 6);
              return video.currentTime;
            }""")
            live = page.get_by_role("button", name="Return to live", exact=True)
            expect(live).to_be_enabled()
            live.click()
            page.wait_for_function("""before => {
              const video = document.querySelector('#eventPlayer video');
              return video && !video.paused && !video.seeking && video.videoWidth > 0 &&
                  video.currentTime > before + 3;
            }""", arg=previous, timeout=25000)
            resumed = player.evaluate("video => video.currentTime")
            live_samples = player.evaluate(DECODE_STAMPS)
            returned = dict(jump_seconds=round(resumed - previous, 2),
                            median_delay_seconds=median(live_samples),
                            samples=live_samples)
            requests.delete(production + "/delivery", headers=auth, timeout=35).raise_for_status()
            assert requests.get(base + manifest, timeout=5).status_code == 404
            assert requests.get(urljoin(base + manifest, segment), timeout=5).status_code == 404
            assert disconnected(encoder, 15)
            with timed_encoder(destination["server"], destination["key"], ffmpeg, certificate) as withdrawn:
                assert disconnected(withdrawn, 18)
        central = median(measurements)
        assert width == 320
        assert central <= 7, f"{protocol.upper()} Low median was {central:.1f} seconds"
        return dict(protocol=protocol, private_preview_preserved=True, invalid_publisher_rejected=True,
                    active_publisher_kicked=True, withdrawn_credentials_rejected=True, signed_media_revoked=True,
                    peertube_relay_live=True, decoded_width=width, first_decoded_seconds=first_frame,
                    samples_seconds=measurements, median_delay_seconds=central, return_to_live=returned)
    finally:
        page.close()




@contextmanager
def relay_fixture(root, app_port, binary, use_tls=True):
    """Run the same authenticated sidecar against the test application on loopback."""
    rtmp_port, secure_port, api_port = free_port(), free_port(), free_port()
    shared = secrets.token_urlsafe(32)
    control = f"http://127.0.0.1:{api_port}"
    reader = f"rtmp://127.0.0.1:{rtmp_port}"
    publish = f"rtmps://127.0.0.1:{secure_port}" if use_tls else reader
    certificate, key = tls_certificate(root)
    config = root / "mediamtx.yml"
    config.write_text(json.dumps(dict(
        logLevel="warn", rtsp=False, rtmp=True, rtmpAddress=f"127.0.0.1:{rtmp_port}", hls=False,
        rtmpEncryption="optional" if use_tls else "no", rtmpsAddress=f"127.0.0.1:{secure_port}",
        rtmpServerCert=str(certificate), rtmpServerKey=str(key),
        webrtc=False, srt=False, moq=False, api=True, apiAddress=f"127.0.0.1:{api_port}",
        authMethod="http",
        authHTTPAddress=f"http://cueporch-relay:{shared}@127.0.0.1:{app_port}/api/internal/rtmp/authorize",
        authHTTPExclude=[dict(action="api")],
        paths={"~^cueporch/[a-f0-9]{32}$": dict(source="publisher", overridePublisher=False)},
    )), encoding="utf-8")
    with (root / "mediamtx.log").open("w", encoding="utf8") as log, subprocess.Popen(
        [str(Path(binary).resolve()), str(config)], stdout=log, stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as relay:
        try:
            deadline = monotonic() + 10
            while monotonic() < deadline and relay.poll() is None:
                try:
                    if requests.get(control + "/v3/rtmp/conns/list", timeout=.5).ok:
                        break
                except requests.RequestException:
                    Event().wait(.2)
            else:
                raise RuntimeError("The isolated MediaMTX relay did not start")
            yield {
                "CUEPORCH_RTMP_RELAY_URL": reader, "CUEPORCH_RTMP_PUBLISH_URL": publish,
                "CUEPORCH_RTMP_RELAY_API": control, "CUEPORCH_RTMP_RELAY_SECRET": shared,
            }, certificate
        finally:
            if relay.poll() is None:
                relay.terminate()
                relay.wait(timeout=6)


def check_low_rtmp(base, keys, ffmpeg, publish, certificate):
    with sync_playwright() as playwright, chromium(playwright) as browser:
        return measure(browser, base, keys, ffmpeg, publish, certificate)


@contextmanager
def marked_feed(connection, key, ffmpeg, certificate, colour, dimensions, fps, tone):
    tls = ["-tls_verify", "1", "-ca_file", str(certificate)] if connection.startswith("rtmps://") else []
    command = [str(ffmpeg), "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi",
               "-i", f"color={colour}:size={dimensions}:rate={fps}",
               "-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000",
               "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-tune", "zerolatency",
               "-pix_fmt", "yuv420p", "-g", str(fps), "-sc_threshold", "0", "-c:a", "aac", *tls,
               "-f", "flv", connection + "/" + key]
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(6)


CHECK_COLOUR = """idx => {
  const video = document.querySelector('#eventPlayer video');
  if (!video || video.paused || video.readyState < 2) return false;
  const canvas = window.fixtureCanvas ??= document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const context = canvas.getContext('2d', {willReadFrequently: true});
  context.drawImage(video, video.videoWidth / 2, video.videoHeight / 2, 1, 1, 0, 0, 1, 1);
  const channels = [...context.getImageData(0, 0, 1, 1).data].slice(0, 3);
  return channels[idx] > 95 && channels.every((value, index) => index === idx || value < 45);
}"""


def check_backup_rtmp(base, keys, ffmpeg, publish, certificate):
    operator = keys["operator"]
    auth = {"Authorization": "Bearer " + operator}
    password = secrets.token_urlsafe(16)
    now = datetime.now(timezone.utc)
    title = "Independent Low encoder failover"
    created = requests.post(base + "/api/live-events", headers=auth, timeout=15, json={
        "title": title, "type": "live", "latency": "low", "visibility": "password", "view_password": password,
        "save_replay": False, "dvr_seconds": 120,
        "schedule": {"timezone": "UTC", "repeat": "none",
                     "start": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                     "end": (now + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")},
    })
    created.raise_for_status()
    event = created.json()
    (slot,) = event["occurrences"]
    production = f"{base}/api/live-events/{event['id']}/{slot['id']}"
    stable = urljoin(base, event["watch"]) + f"?occurrence={slot['id']}"
    endpoint = production + "/rtmp"

    def inputs(primary, backup, selected):
        deadline = monotonic() + 28
        while monotonic() < deadline:
            health = requests.get(production + "/health?source=relay", headers=auth, timeout=8)
            health.raise_for_status()
            current = (health.json()["encoder"] or {}).get("inputs")
            if current == dict(primary=primary, backup=backup, selected=selected):
                return
            Event().wait(.3)
        raise AssertionError(f"Expected input state {(primary, backup, selected)}, got {current}")

    def picture(page, channel):
        try:
            page.wait_for_function(CHECK_COLOUR, arg=channel, timeout=23000)
        except Exception as error:
            state = page.evaluate("""() => {
              const container = document.querySelector('#eventPlayer'), v = container.querySelector('video');
              return {phase: container.dataset.phase, transport: container.dataset.transport?.split(':')[0],
                      at: v?.currentTime, paused: v?.paused, ready: v?.readyState, error: v?.error?.code,
                      samePlayer: window.originalVideo === v};
            }""")
            raise AssertionError(f"Expected picture channel {channel}, player state {state}") from error

    with sync_playwright() as playwright, chromium(playwright) as browser, ExitStack() as feeds:
        staff = browser.new_page()
        staff.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator)});")
        staff.goto(base + "/operator/live-events")
        staff.get_by_role("button", name=f"Open production for {title}", exact=False).click()
        staff.get_by_label("Use an independent backup encoder").check()
        staff.get_by_label("Backup programme output").select_option("360")
        assert requests.post(endpoint, json={"backup": True}, timeout=8).status_code == 403
        with staff.expect_response(lambda answer: answer.url == endpoint, timeout=30000) as reply:
            staff.get_by_role("button", name="Prepare low-latency RTMP").click()
        assert reply.value.ok, f"Could not prepare dual encoders: HTTP {reply.value.status}"
        connection = reply.value.json()
        assert connection["server"] == publish + "/cueporch"
        assert connection["key"] != connection["backup_key"] and connection["output_height"] == 360
        assert staff.get_by_label("Backup stream key", exact=True).input_value() == connection["backup_key"]
        again = requests.post(endpoint, headers=auth, json={"backup": True, "height": 360}, timeout=8)
        assert again.json() == connection
        assert requests.post(endpoint, headers=auth, json={"backup": False}, timeout=8).status_code == 409
        audience = browser.new_page()
        audience.goto(stable)
        audience.get_by_label("Event password").fill(password)
        audience.get_by_role("button", name="Unlock event").click()
        primary = feeds.enter_context(marked_feed(connection["server"], connection["key"], ffmpeg, certificate,
                                                   "red", "320x180", 12, 440))
        inputs(True, False, "primary")
        status = audience.locator("#eventStatus")
        expect(status).to_have_text("Live", timeout=25000)
        player = audience.locator("#eventPlayer video")
        player.evaluate("v => { v.muted = true; void v.play(); window.originalVideo = v; }")
        picture(audience, 0)
        address = urljoin(base, audience.locator("#eventPlayer").get_attribute("data-address"))
        assert audience.locator("#eventPlayer").get_attribute("data-transport").startswith("local:")
        assert player.evaluate("v => v.videoWidth") == 640
        backup = feeds.enter_context(marked_feed(connection["server"], connection["backup_key"], ffmpeg, certificate,
                                                  "blue", "640x360", 25, 770))
        inputs(True, True, "primary")
        wait_for_delivery(production, auth, "live", 40, primary)
        staff.get_by_role("button", name="Refresh stream status").click()
        expect(staff.locator("#eventHealthDetails")).to_contain_text("Backup receiving")
        expect(staff.locator("#eventHealthDetails")).to_contain_text("On air Primary")
        primary.terminate()
        started = monotonic()
        primary.wait(6)
        inputs(False, True, "backup")
        picture(audience, 2)
        failover = round(monotonic() - started, 2)
        assert audience.evaluate("window.originalVideo === document.querySelector('#eventPlayer video')")
        assert audience.url == stable and requests.get(address, timeout=8).ok
        manifest = requests.get(address, timeout=8)
        assert "#EXT-X-DISCONTINUITY" in manifest.text.splitlines()
        restored = feeds.enter_context(marked_feed(connection["server"], connection["key"], ffmpeg, certificate,
                                                    "green", "480x270", 20, 880))
        inputs(True, True, "backup")
        assert audience.evaluate(CHECK_COLOUR, 2)
        backup.terminate()
        started = monotonic()
        backup.wait(6)
        inputs(True, False, "primary")
        picture(audience, 1)
        failback = round(monotonic() - started, 2)
        manifest = requests.get(address, timeout=8)
        assert manifest.text.splitlines().count("#EXT-X-DISCONTINUITY") == 2
        wait_for_delivery(production, auth, "live", 20, restored)
        requests.delete(production + "/delivery", headers=auth, timeout=35).raise_for_status()
        assert requests.get(address, timeout=8).status_code == 404
        revoked = feeds.enter_context(marked_feed(connection["server"], connection["backup_key"], ffmpeg, certificate,
                                                    "blue", "640x360", 25, 770))
        assert disconnected(restored, 18) and disconnected(revoked, 12)
        return dict(protocol=urlsplit(publish).scheme, different_input_resolutions=True,
                    protected_output_width=640, failover_seconds=failover, failback_seconds=failback,
                    same_open_player=True, primary_recovery_did_not_preempt_backup=True, peertube_live=True,
                    withdrawal_disconnected_encoder_and_revoked_grants=True)
