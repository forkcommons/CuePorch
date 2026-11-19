"""Decode private external feeds before releasing or withdrawing them."""

from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
from shutil import copyfile
import socket
import subprocess
from threading import Thread
from time import monotonic
from urllib.parse import urlencode, urljoin

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_embed_builder import PublisherPage
from installed_events import play_event, send_signal, wait_for_delivery
from installed_simulcast import free_port, receive, size, wait_until


def check_encoder_preview(base, keys, ffmpeg, root, diagnostics):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    public_json = {"Accept": "application/json"}

    def api(method, address, data=None):
        result = requests.request(method, address, headers=auth, json=data, timeout=25)
        result.raise_for_status()
        return result.json() if result.content else None

    def create(name, slug, retain=False):
        now = datetime.now(timezone.utc)
        event = api("POST", base + "/api/live-events", dict(
            title=name, slug=slug, type="live", latency="standard", visibility="public", save_replay=retain, archive_replay=False,
            schedule=dict(timezone="UTC", repeat="days" if retain else "none", count=2 if retain else None,
                          start=(now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M"),
                          end=(now + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M"))))
        opens = event["occurrences"][0]["id"]
        path = f"{base}/api/live-events/{event['id']}/{opens}"
        return path, *(f"{base}{event[surface]}?occurrence={opens}" for surface in ("watch", "embed"))

    direct = "RTMP private rehearsal"
    production, watch, embed = create(direct, "rtmp-private-rehearsal", retain=True)
    settings = production.rsplit("/", 1)[0] + "/encoder-settings"
    preview = production + "/preview"
    assert requests.get(preview, timeout=10).status_code == 403
    assert requests.post(preview, timeout=10).status_code == 403
    key = "cueporch-private-preview-fixture"
    capture = root / "preview-destination.flv"
    port = free_port()
    destination = api("POST", production + "/destinations", dict(name="Local cable", server=f"rtmp://127.0.0.1:{port}/live", key=key))["destinations"][0]

    def media_references(manifest):
        response = requests.get(manifest, timeout=10)
        response.raise_for_status()
        return [line for line in response.text.splitlines() if line and not line.startswith("#")]

    with sync_playwright() as pw, ThreadingHTTPServer(("127.0.0.1", 0), PublisherPage) as publisher:
        publisher.code = f'<iframe title="Station embed" src="{embed}" width="800" height="700" allowfullscreen></iframe>'
        thread = Thread(target=publisher.serve_forever, daemon=True)
        thread.start()
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            media_statuses = []
            operator.on("response", lambda response: media_statuses.append(response.status)
                        if response.url.startswith(base + "/encoder-preview/") else None)
            toggle = operator.get_by_label("Preview external encoder privately")
            private_video = operator.get_by_label("Private encoder preview")
            go_live = operator.get_by_role("button", name="Go live", exact=True)
            p_status = operator.locator("#encoderPreviewStatus")

            def select(name):
                operator.goto(base + "/operator/live-events")
                operator.get_by_role("button", name=f"Open production for {name}").first.click()

            def enable_preview():
                expect(toggle).not_to_be_checked()
                toggle.check()
                expect(operator.locator("#eventsStatus")).to_have_text("Encoder preview preference saved.")

            def color(page, selector, channel, timeout=30000):
                page.wait_for_function("""({selector, channel}) => {
                    const v = document.querySelector(selector);
                    if (!v || v.readyState < 2) return false;
                    const canvas = document.createElement('canvas');
                    canvas.width = canvas.height = 1;
                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(v, 0, 0, 1, 1);
                    const color = ctx.getImageData(0, 0, 1, 1).data;
                    return color[channel] > 145 && color[(channel + 1) % 3] < 70 && color[(channel + 2) % 3] < 70;
                }""", arg=dict(selector=selector, channel=channel), timeout=timeout)

            def check_preview(width, path, audience, frames=()):
                operator.bring_to_front()
                expect(p_status).to_have_text("The private encoder signal is ready.", timeout=40000)
                data = api("GET", path + "/preview")
                assert data["state"] == "signal"
                manifest = base + data["address"]
                ticket = requests.get(manifest, timeout=10)
                assert ticket.ok and ticket.text.startswith("#EXTM3U"), "Private manifest HTTP " + str(ticket.status_code)
                assert "no-store" in ticket.headers["Cache-Control"]
                expect(private_video).to_be_visible()
                try:
                    private_video.evaluate("""async v => {
                        v.muted = true;
                        await Promise.race([v.play(), new Promise((_, reject) =>
                            setTimeout(() => reject(Error('Private preview did not start')), 15000))]);
                    }""")
                    operator.wait_for_function("""w => {
                        const v = document.getElementById('encoderPreviewVideo');
                        return v.videoWidth === w && v.currentTime > 1 && v.readyState >= 2;
                    }""", arg=width, timeout=22000)
                except Exception:
                    state = private_video.evaluate("""v => ({width: v.videoWidth, t: v.currentTime, ready: v.readyState,
                        muted: v.muted, paused: v.paused, error: v.error?.code, network: v.networkState,
                        visibility: document.visibilityState})""")
                    print(f"Private preview browser state {state}, media response codes {media_statuses[-20:]}", flush=True)
                    raise
                audio_tracks = sum(line.startswith("#EXT-X-MEDIA:TYPE=AUDIO,") for line in ticket.text.splitlines())
                print(f"Decoded {width}px private video. The host announced {audio_tracks} separate audio renditions.", flush=True)
                for page, link in ((audience, audience.url), *frames):
                    expect(page.locator("#eventStatus")).to_have_text("Scheduled")
                    expect(page.locator("#eventPlayer video, #eventPlayer iframe")).to_have_count(0)
                    assert requests.get(link, headers=public_json, timeout=8).json()["address"] is None
                return manifest

            select(direct)
            enable_preview()
            assert requests.put(settings, json={"preview": False}, timeout=10).status_code == 403
            operator.get_by_role("button", name=f"Open production for {direct}").last.click()
            expect(toggle).to_be_checked()
            select(direct)
            expect(toggle).to_be_checked()
            with operator.expect_response(lambda response: response.url == production + "/encoder") as prepared:
                operator.get_by_role("button", name="Prepare encoder").click()
            assert prepared.value.ok
            encoder = prepared.value.json()
            assert encoder["preview"] and not encoder["public"]
            expect(go_live).to_be_disabled()
            early = requests.post(preview, headers=auth, timeout=10)
            assert early.status_code == 409
            direct_attempt = requests.post(production + "/encoder", json={"preview": False}, headers=auth, timeout=10)
            assert direct_attempt.status_code == 409
            guest = browser.new_page()
            guest.goto(watch)
            outside = browser.new_page()
            outside.goto(f"http://127.0.0.1:{publisher.server_port}/")
            frame = outside.get_by_title("Station embed").element_handle().content_frame()
            assert frame

            with receive(ffmpeg, port, key, capture, timeout=240) as output:
                transition = "color=c=red:s=320x180:r=24,drawbox=c=blue:t=fill:enable='gte(t,45)'"
                with send_signal(ffmpeg, encoder["server"], encoder["key"], 225, video_source=transition) as signal:
                    held = check_preview(320, production, guest, ((frame, embed),))
                    color(operator, "#encoderPreviewVideo", 0)
                    assert size(capture) == 0 and output.poll() is None
                    variants = media_references(held)
                    previous_segments = media_references(urljoin(held, variants[0]))
                    assert previous_segments
                    private_part = requests.get(urljoin(held, previous_segments[-1]), timeout=8)
                    assert private_part.ok
                    color(operator, "#encoderPreviewVideo", 2, timeout=80000)
                    with operator.expect_response(lambda response: response.url == preview and response.request.method == "POST") as response:
                        go_live.click()
                    assert response.value.ok
                    expect(private_video).to_be_hidden()
                    assert requests.get(held, timeout=10).status_code == 404
                    for surface in (guest, frame):
                        expect(surface.locator("#eventStatus")).to_have_text("Live", timeout=65000)
                    public_manifest, first, _ = play_event(guest, watch, "Live", navigate=False)
                    _, second, _ = play_event(frame, embed, "Live", navigate=False)
                    assert first == second == 320 and guest.url == watch and frame.url == embed
                    for surface in (guest, frame):
                        color(surface, "#eventPlayer video", 2)
                    public_part = requests.get(urljoin(public_manifest, previous_segments[-1]), timeout=8)
                    assert not public_part.ok or public_part.content != private_part.content
                    assert requests.get(urljoin(held, previous_segments[-1]), timeout=8).status_code == 404
                    print("Audience decoded only the released color and the original private grant was revoked.", flush=True)
                    elapsed = monotonic()
                    changes = []
                    checked_at = 0.0

                    def receiver_ready():
                        nonlocal checked_at
                        now = monotonic()
                        if now - checked_at > 2:
                            checked_at = now
                            states = api("GET", production + "/destinations")["destinations"]
                            observation = (states[0]["state"], output.poll(), signal.poll())
                            if not changes or changes[-1][1] != observation:
                                changes.append((round(now - elapsed, 1), observation))
                        return size(capture) > 64000

                    wait_until(receiver_ready, 40, lambda: f"Receiver had {size(capture)} bytes and states {changes}.")
                    assert signal.poll() is None
                    api("PUT", production + "/destinations/" + destination["id"], {"enabled": False})
                    wait_until(lambda: output.poll() is not None, 20, "Disabling did not stop simulcast.")
                    assert signal.wait(timeout=230) == 0
                decoded = subprocess.run([str(ffmpeg), "-nostdin", "-v", "info", "-i", str(capture), "-map", "0:a:0",
                                          "-af", "ashowinfo", "-t", "8", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                                         capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                first_sample = next((line.split("pts_time:", 1)[1].split()[0] for line in decoded.stderr.decode(errors="replace").splitlines()
                                     if "ashowinfo" in line and " n:0 " in line and "pts_time:" in line), None)
                if (decoded.returncode or len(decoded.stdout) < 16000 or not any(decoded.stdout)
                        or first_sample is None or abs(float(first_sample)) >= .5):
                    copyfile(capture, diagnostics.with_suffix(".rejected-synthetic-relay.flv"))
                    raise AssertionError(f"Released relay held {size(capture)} bytes, audio decoder exit {decoded.returncode}, "
                                         f"decoded {len(decoded.stdout)} PCM bytes, first sample {first_sample}.")
                print(f"Released relay decoded {len(decoded.stdout)} PCM bytes from its first eight seconds, with audio starting at {first_sample}s.", flush=True)
                sampled = subprocess.run([str(ffmpeg), "-nostdin", "-v", "error", "-i", str(capture), "-frames:v", "1",
                                          "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
                                         capture_output=True, timeout=15, check=True,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                assert len(sampled) == 3 and sampled[2] > 145 and max(sampled[:2]) < 70
            print("The private RTMP feed became public on the original links, and the receiver decoded it after release.", flush=True)

            title = "SRT private rehearsal"
            srt_path, srt_watch, _ = create(title, "srt-private-rehearsal")
            select(title)
            enable_preview()
            operator.get_by_text("SRT encoder", exact=True).click()
            with socket.socket(type=socket.SOCK_DGRAM) as udp:
                udp.bind(("127.0.0.1", 0))
                srt_port = udp.getsockname()[1]
            operator.get_by_label("UDP port").fill(str(srt_port))
            with operator.expect_response(lambda response: response.url == srt_path + "/srt") as started:
                operator.get_by_role("button", name="Start SRT listener").click()
            assert started.value.ok
            connection = started.value.json()
            endpoint = connection["server"] + "?" + urlencode(dict(
                mode="caller", pbkeylen=16, passphrase=connection["passphrase"], pkt_size=1316))
            viewer = browser.new_page()
            viewer.goto(srt_watch)
            with send_signal(ffmpeg, endpoint, None, 70, "mpegts"):
                withheld = check_preview(320, srt_path, viewer)
                api("DELETE", srt_path + "/delivery")
                assert requests.get(withheld, timeout=10).status_code == 404
                expect(viewer.locator("#eventStatus")).to_have_text("This occurrence was withdrawn", timeout=15000)

            default = "Automatic encoder"
            normal, normal_watch, _ = create(default, "automatic-encoder")
            normal_connection = api("POST", normal + "/encoder")
            assert not normal_connection["preview"] and normal_connection["public"]
            with send_signal(ffmpeg, normal_connection["server"], normal_connection["key"], 40) as signal:
                wait_for_delivery(normal, auth, "live", 30, signal)
                _, auto_width, _ = play_event(browser.new_page(), normal_watch, "Live")
                assert auto_width == 320
            print("Encrypted SRT stayed private until withdrawal. Waiting for the released recording to finalize.", flush=True)
            expect(guest.locator("#eventStatus")).to_have_text("Replay", timeout=440000)
            _, replay_width, duration = play_event(guest, watch, "Replay", navigate=False)
            assert replay_width == 320 and duration >= 15
            color(guest, "#eventPlayer video", 2)
            guest.locator("#eventPlayer video").evaluate("v => { v.pause(); v.currentTime = 0; }")
            guest.wait_for_function("""() => {
                const v = document.querySelector('#eventPlayer video');
                return !v.seeking && v.readyState >= 2 && v.currentTime < .15;
            }""")
            color(guest, "#eventPlayer video", 2)
            return dict(private_rtmp_width=320, private_encrypted_srt_width=320, unrelated_public_requests_denied=True,
                        direct_encoder_action_cannot_bypass_preview=True, release_requires_signal=True,
                        release_and_withdraw_revoke_granted_preview=True, original_watch_and_embed_continued=True,
                        simulcast_receives_only_released_stream=True, automatic_default_width=auto_width,
                        simulcast_pcm_bytes=len(decoded.stdout), simulcast_first_audio_seconds=float(first_sample), recurring_preference=True,
                        original_preview_segment_revoked=True, decoded_replay_seconds=round(duration, 2))
        finally:
            browser.close()
            publisher.shutdown()
            thread.join()
