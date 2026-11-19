"""Exercise the installed Web Push client against a disposable HTTPS receiver."""

import base64
import json
import os
import queue
import ssl
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from threading import Thread

import http_ece
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from playwright.sync_api import expect
from py_vapid import Vapid


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


@contextmanager
def push_receiver(root):
    messages = queue.Queue()
    receiver_key = ec.generate_private_key(ec.SECP256R1())
    auth_secret = os.urandom(16)

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            messages.put((dict(self.headers), body))
            self.send_response(201)
            self.end_headers()

        def log_message(self, *_):
            pass

    issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "CuePorch loopback fixture")]
    )
    tls_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(issuer)
        .issuer_name(issuer)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]),
            False,
        )
        .sign(tls_key, hashes.SHA256())
    )
    pem, private = root / "receiver.pem", root / "receiver.key"
    pem.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(
        tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(pem, private)
    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        origin = f"https://127.0.0.1:{server.server_port}"
        fixture = {
            "env": {
                "CUEPORCH_PUSH_CONTACT": "mailto:cueporch-test@example.invalid",
                "CUEPORCH_PUSH_SERVICE_ORIGINS": origin,
                "REQUESTS_CA_BUNDLE": str(pem),
            },
            "subscription": {
                "endpoint": origin + "/subscription",
                "keys": {
                    "auth": encoded(auth_secret),
                    "p256dh": encoded(
                        receiver_key.public_key().public_bytes(
                            serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint,
                        )
                    ),
                },
            },
            "messages": messages,
            "receiver_key": receiver_key,
            "auth_secret": auth_secret,
        }
        try:
            yield fixture
        finally:
            server.shutdown()
            server_thread.join(timeout=5)


def check_review_push(browser, base, auth, work, fixture):
    link_result = requests.post(
        f"{base}/api/library/work/{work}/review-links",
        headers=auth,
        timeout=10,
        json={
            "name": "Offline push rehearsal",
            "password": "fixture-device-passphrase",
            "allow_comment": True,
        },
    )
    link_result.raise_for_status()
    link = base + link_result.json()["path"]
    recipient = browser.new_context(permissions=["notifications"])
    sender = browser.new_context()

    def unlock(page, url):
        page.goto(url)
        page.get_by_label("Review password").fill("fixture-device-passphrase")
        page.get_by_role("button", name="Open review").click()
        expect(page.locator("#reviewFeedback")).to_be_visible()

    try:
        recipient.add_init_script(f"""(() => {{
          const subscription = {json.dumps(fixture["subscription"])};
          PushManager.prototype.getSubscription = async () => null;
          PushManager.prototype.subscribe = async (options) => {{
            if (!options.userVisibleOnly || !options.applicationServerKey) throw new Error('Missing VAPID settings');
            return {{toJSON: () => subscription}};
          }};
        }})();""")
        page = recipient.new_page()
        unlock(page, link)
        endpoint = base + page.locator("#reviewFeedback").get_attribute("data-feedback")
        page.get_by_text("Notifications", exact=True).click()
        mode = page.get_by_label("Comment notifications")
        mode.select_option("all")
        expect(mode).to_be_enabled()
        invalid = fixture["subscription"] | {
            "endpoint": "https://example.invalid/subscription"
        }
        assert recipient.request.put(endpoint + "/push", data=invalid).status == 400
        page.get_by_role(
            "button", name="Enable device notifications", exact=True
        ).click()
        expect(page.get_by_text("Device notifications enabled.")).to_be_visible()
        active_worker = page.evaluate(
            "async () => (await navigator.serviceWorker.ready).active.scriptURL"
        )
        assert active_worker == base + "/review/notifications-worker.js"
        worker_source = page.request.get(active_worker).text()
        page.close()

        poster = sender.new_page()
        unlock(poster, link)
        posted = sender.request.post(
            endpoint, data={"author": "Curator", "body": "Offline push.", "seconds": 0}
        )
        assert posted.status == 201
        comment = posted.json()["id"]
        headers, body = fixture["messages"].get(timeout=20)
        assert headers["Content-Encoding"] == "aes128gcm"
        authorization = next(
            value for name, value in headers.items() if name.lower() == "authorization"
        )
        assert Vapid.verify(authorization)
        cleartext = http_ece.decrypt(
            body,
            private_key=fixture["receiver_key"],
            auth_secret=fixture["auth_secret"],
            version="aes128gcm",
        )
        payload = json.loads(cleartext)
        assert payload["path"].endswith("#comment-" + comment)
        assert payload["path"].split("?", 1)[0] == link.removeprefix(base)
        worker = poster.evaluate(
            """async ({source, payload}) => {
              const code = source.replace('const worker = globalThis;', 'const worker = globalThis.fixtureWorker;');
              const handlers = new Map();
              const evidence = {};
              globalThis.fixtureWorker = {
                registration: {showNotification: async (title, options) => { evidence.title = title; evidence.options = options; }},
                clients: {openWindow: async (path) => { evidence.path = path; }},
                addEventListener: (type, handler) => handlers.set(type, handler),
              };
              await import(URL.createObjectURL(new Blob([code], {type: 'text/javascript'})));
              handlers.get('push')({data: {json: () => payload}, waitUntil: () => {}});
              handlers.get('notificationclick')({notification: {data: evidence.options.data, close: () => {}}, waitUntil: () => {}});
              return evidence;
            }""",
            {"source": worker_source, "payload": payload},
        )
        assert (
            worker["title"] == "New CuePorch review feedback"
            and worker["path"] == payload["path"]
        )
        recipient.clear_cookies(name="cueporch_review")
        again = recipient.new_page()
        unlock(again, base + payload["path"])
        expect(again).to_have_url(base + payload["path"])
        expect(again.locator("#comment-" + comment)).to_be_focused()
        again.get_by_text("Notifications (1)", exact=True).click()
        again.get_by_role(
            "button", name="Turn off device notifications", exact=True
        ).click()
        expect(again.get_by_text("Device notifications disabled.")).to_be_visible()
        assert (
            sender.request.post(
                endpoint, data={"author": "Curator", "body": "Disabled.", "seconds": 1}
            ).status
            == 201
        )
        try:
            fixture["messages"].get(timeout=3)
        except queue.Empty:
            pass
        else:
            raise AssertionError("A disabled device received a push notification")
        return {
            "signed_encrypted_https_push": True,
            "closed_recipient_tab": True,
            "notification_click_focus": True,
            "protected_comment_link_survives_unlock": True,
        }
    finally:
        sender.close()
        recipient.close()
