"""Receive installed CuePorch's custom outputs over actual RTMP and RTMPS."""

from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
import json
import socket
import ssl
import subprocess
from threading import Event, Thread
from time import monotonic, sleep

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_events import play_event, send_signal, wait_for_delivery


def tls_certificate(root):
    certificate = root / "simulcast-test-ca.pem"
    keyfile = root / "simulcast-test-key.pem"
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "CuePorch local TLS fixture")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().issuer_name(name).subject_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]), False)
            .sign(key, SHA256()))
    certificate.write_bytes(cert.public_bytes(Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    return certificate, keyfile


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def receive(ffmpeg, port, name, path):
    with subprocess.Popen([
        str(ffmpeg), "-nostdin", "-v", "error", "-listen", "1", "-timeout", "120",
        "-i", f"rtmp://127.0.0.1:{port}/live/{name}", "-map", "0", "-c", "copy", "-flush_packets", "1",
        "-y", str(path),
    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as proc:
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=6)


@contextmanager
def tls_bridge(port, material):
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(*material)
    secured = Event()
    done = Event()

    def relay(source, target):
        try:
            while not done.is_set() and (data := source.recv(65536)):
                target.sendall(data)
        except OSError:
            pass
        finally:
            done.set()
            for stream in (source, target):
                with suppress(OSError):
                    stream.shutdown(socket.SHUT_RDWR)

    def connect(listener):
        try:
            connection, _ = listener.accept()
            with connection, server_tls.wrap_socket(connection, server_side=True) as secured_socket:
                secured.set()
                with socket.create_connection(("127.0.0.1", port), timeout=5) as receiver:
                    receiver.settimeout(None)
                    outbound = Thread(target=relay, args=(receiver, secured_socket), daemon=True)
                    outbound.start()
                    relay(secured_socket, receiver)
                    outbound.join(timeout=3)
        except OSError:
            done.set()

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(60)
        proxy = Thread(target=connect, args=(listener,), daemon=True)
        proxy.start()
        try:
            yield listener.getsockname()[1], secured
        finally:
            done.set()
            with suppress(OSError):
                listener.shutdown(socket.SHUT_RDWR)
            proxy.join(timeout=4)


def size(path):
    return path.stat().st_size if path.exists() else 0


def wait_until(check, seconds, message):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        if check():
            return
        sleep(.25)
    raise AssertionError(message() if callable(message) else message)


def decode(ffmpeg, path):
    for media, options, minimum in [
        ("v", ["-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo"], 320 * 180),
        ("a", ["-t", "0.5", "-ac", "1", "-ar", "16000", "-f", "s16le"], 4000),
    ]:
        decoded = subprocess.run([str(ffmpeg), "-nostdin", "-v", "error", "-i", str(path),
                                  "-map", f"0:{media}:0", *options, "pipe:1"], capture_output=True, timeout=12,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert decoded.returncode == 0 and len(decoded.stdout) >= minimum and any(decoded.stdout), (
            f"{path.name} {media} decoded {len(decoded.stdout)} bytes: {decoded.stderr.decode(errors='replace')}"
        )


def check_simulcast(base, keys, ffmpeg, root, material):
    now = datetime.now(timezone.utc)
    auth = {"Authorization": "Bearer " + keys["operator"]}
    title = "Station simulcast"
    event = dict(title=title, slug="station-simulcast", visibility="public", type="live",
                 latency="standard", save_replay=False,
                 schedule=dict(timezone="UTC", repeat="days", count=2,
                               start=(now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                               end=(now + timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M")))
    result = requests.post(base + "/api/live-events", headers=auth, json=event, timeout=10)
    result.raise_for_status()
    created = result.json()
    first, tomorrow = created["occurrences"]
    prefix = f"{base}/api/live-events/{created['id']}"
    production = prefix + f"/{first['id']}"
    targets = production + "/destinations"
    watch = base + created["watch"] + f"?occurrence={first['id']}"
    assert requests.get(targets, timeout=10).status_code == 403
    rejected = requests.post(targets, headers=auth, timeout=10,
                             json={"name": "Invalid", "server": "https://127.0.0.1/live", "key": "example"})
    assert rejected.status_code == 422

    def destinations(url=targets):
        response = requests.get(url, headers=auth, timeout=10)
        response.raise_for_status()
        assert "fixture-video-key" not in response.text
        return {item["name"]: item for item in response.json()["destinations"]}

    port_a, port_b, offline_port = (free_port() for _ in range(3))
    assert len({port_a, port_b, offline_port}) == 3
    left, right = root / "receiver-a.flv", root / "receiver-b.flv"
    key_a, key_b = "fixture-video-key-a", "fixture-video-key-b"
    with receive(ffmpeg, port_a, key_a, left) as receiver_a, receive(ffmpeg, port_b, key_b, right) as receiver_b:
        with tls_bridge(port_b, material) as (tls_port, tls_active), sync_playwright() as pw:
            browser = chromium(pw)
            try:
                operator = browser.new_page()
                operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
                operator.goto(base + "/operator/live-events")
                operator.get_by_role("button", name=f"Open production for {title}", exact=False).first.click()
                operator.get_by_text("Simulcast destinations", exact=True).click()
                form = operator.locator("#addDestination")
                for name, server, key in [
                    ("Cable", f"rtmp://127.0.0.1:{port_a}/live", key_a),
                    ("Remote", f"rtmps://127.0.0.1:{tls_port}/live", key_b),
                    ("Unavailable", f"rtmp://127.0.0.1:{offline_port}/live", "fixture-video-key-offline"),
                ]:
                    form.get_by_label("Destination name").fill(name)
                    form.get_by_label("RTMP or RTMPS server").fill(server)
                    form.get_by_label("Destination stream key").fill(key)
                    form.get_by_role("button", name="Add destination").click()
                    expect(operator.get_by_role("button", name=f"Turn off {name}")).to_be_enabled()
                    expect(form.get_by_label("Destination stream key")).to_have_value("")
                reusable = destinations(prefix + f"/{tomorrow['id']}/destinations")
                assert set(reusable) == {"Cable", "Remote", "Unavailable"}
                assert all(item["enabled"] for item in reusable.values())
                with operator.expect_response(lambda response: response.url == production + "/encoder"
                                              and response.request.method == "POST") as provision:
                    operator.get_by_role("button", name="Prepare encoder").click()
                assert provision.value.ok
                encoder_info = provision.value.json()
                with send_signal(ffmpeg, encoder_info["server"].rstrip("/"), encoder_info["key"], 120) as encoder:
                    wait_for_delivery(production, auth, "live", 50, encoder)
                    wait_until(lambda: size(left) > 12000 and size(right) > 12000, 28,
                               lambda: f"Receiver bytes {size(left)}/{size(right)}, TLS handshake {tls_active.is_set()}, "
                               f"states {[(name, item['state']) for name, item in destinations().items()]}")
                    assert tls_active.is_set()
                    assert receiver_a.poll() is None and receiver_b.poll() is None
                    wait_until(lambda: destinations()["Unavailable"]["state"] == "retrying", 15,
                               "The unavailable destination did not surface its reconnect state.")
                    browser_guest = browser.new_page()
                    _, width, _ = play_event(browser_guest, watch, "Live")
                    assert width == 320
                    print(f"RTMP and verified TLS outputs received the program; primary player decoded {width}px.", flush=True)
                    operator.get_by_role("button", name="Turn off Cable").click()
                    expect(operator.get_by_role("button", name="Turn on Cable")).to_be_enabled()
                    wait_until(lambda: receiver_a.poll() is not None, 15, "Turning off Cable did not close its output.")
                    start_bytes = size(right)
                    wait_until(lambda: size(right) > start_bytes + 3000, 12, "Remote stopped after disabling Cable.")
                    assert not destinations()["Cable"]["enabled"] and destinations()["Remote"]["enabled"]
                    with operator.expect_response(lambda response: response.url == production + "/delivery") as withdrawn:
                        operator.get_by_role("button", name="Withdraw this occurrence").click()
                    assert withdrawn.value.status == 204
                    wait_until(lambda: receiver_b.poll() is not None, 18, "Withdrawal did not close the TLS output.")
                    expect(browser_guest.locator("#eventPlayer video")).to_have_count(0, timeout=15000)
                decode(ffmpeg, left)
                decode(ffmpeg, right)
                return dict(primary_width=width, rtmp_video_and_audio_decoded=True,
                            rtmps_verified_video_and_audio_decoded=True, disabled_leg_stops_independently=True,
                            failed_destination_isolated=True, withdraw_closes_remaining_destination=True,
                            future_occurrence_preserves_destinations=True, operator_only_redacted_settings=True)
            finally:
                browser.close()
