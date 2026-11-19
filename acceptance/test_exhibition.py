"""Unattended two-exhibitor rehearsal using an actual browser and playable derivative."""

import json
import secrets
import shutil
from contextlib import closing
import sqlite3
from unittest.mock import patch
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright
from browser import chromium
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cueporch import import_collection
from institution import create_app


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


class Exhibition(unittest.TestCase):
    def test_two_exhibitors(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            tmp = Path(folder)
            subprocess.run(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=s=160x90:r=12:d=3",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(tmp / "film.mp4"),
                ],
                check=True,
            )
            source = tmp / "intake.json"
            source.write_text(
                json.dumps(
                    {
                        "institution": "Arts",
                        "works": [
                            {
                                "title": "Test film",
                                "provider": "artist",
                                "source_id": "cut-A",
                                "source_url": "",
                                "files": [
                                    {
                                        "role": "playback",
                                        "format": "mp4",
                                        "path": "film.mp4",
                                    }
                                ],
                            }
                        ],
                    }
                )
            )
            collection = tmp / "collection"
            import_collection(source, collection)
            work = json.loads((collection / "manifest.json").read_text())["works"][0]
            secret = secrets.token_hex(32)
            curator_key = secrets.token_hex(32)
            with patch.dict(
                "os.environ",
                {"CUEPORCH_OPERATOR_KEYS": json.dumps({"Duty curator": curator_key})},
            ):
                app = create_app(collection, tmp / "operator.sqlite", secret, "Arts")
            origin = stack.enter_context(
                running(make_server("127.0.0.1", 0, app, threaded=True))
            )
            headers = {"Authorization": "Bearer " + secret}

            class Site(BaseHTTPRequestHandler):
                def do_GET(self):
                    data = (
                        '<iframe src="' + self.server.embed + '"></iframe>'
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(data)

                def log_message(self, *args):
                    pass

            def museum():
                host = ThreadingHTTPServer(("127.0.0.1", 0), Site)
                domain = stack.enter_context(running(host))
                result = requests.post(
                    origin + "/api/grants",
                    headers=headers,
                    json=dict(
                        work=work["id"],
                        version=work["files"][0]["sha256"],
                        origin=domain,
                        starts=time.time() - 1,
                        ends=time.time() + 600,
                        agreement="Exhibition 1",
                        audience="ticket holders",
                    ),
                ).json()
                path = origin + "/api/grants/" + result["id"]

                def ticket():
                    return requests.post(
                        path + "/session",
                        headers={
                            "Authorization": "Bearer " + result["exhibitor_secret"]
                        },
                    )

                return host, domain, path, ticket

            host, a, path, ticket = museum()
            session = ticket()
            self.assertEqual(session.status_code, 200)
            host.embed = origin + session.json()["embed"]
            settings = origin + "/api/policy/grant"
            requests.put(
                settings, headers=headers, json={"required": True}
            ).raise_for_status()
            other, b, path_b, ticket_b = museum()
            self.assertEqual(ticket_b().status_code, 403)
            requests.put(
                settings, headers=headers, json={"required": False}
            ).raise_for_status()
            other.embed = origin + ticket_b().json()["embed"]
            self.assertEqual(requests.get(host.embed).status_code, 403)
            self.assertEqual(requests.get(host.embed + "/media").status_code, 403)
            with sync_playwright() as pw:
                browser = chromium(pw)
                page = browser.new_page()
                pages = []
                for site in [a, b]:
                    page = browser.new_page()
                    pages.append(page)
                    page.goto(site)
                    video = page.frame_locator("iframe").locator("video")
                    self.assertEqual(
                        video.evaluate(
                            "async v=>{v.muted=true;await v.play();return v.videoWidth}"
                        ),
                        160,
                    )
                requests.delete(path, headers=headers).raise_for_status()
                pages[0].frame_locator("iframe").get_by_text(
                    "This screening has closed."
                ).wait_for(timeout=6000)
                self.assertEqual(
                    requests.get(
                        host.embed + "/media", headers={"Referer": host.embed}
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    requests.get(
                        other.embed + "/media",
                        headers={"Referer": other.embed, "Range": "bytes=0-5"},
                    ).status_code,
                    206,
                )
                browser.close()
            case = requests.post(
                origin + "/api/works/" + work["id"] + "/complaints",
                json={"details": "Please review this film"},
            ).json()
            endpoint = origin + "/api/cases/" + case["case"]
            creator = {"Authorization": "Bearer " + case["case_key"]}
            curator = {"Authorization": "Bearer " + curator_key}
            requests.post(
                endpoint,
                headers=creator,
                json={
                    "kind": "context",
                    "details": "The artist supplied exhibition context.",
                },
            ).raise_for_status()
            for kind, user in [
                ("decision", curator),
                ("appeal", creator),
                ("restoration", curator),
            ]:
                requests.post(
                    endpoint,
                    headers=user,
                    json={
                        "kind": kind,
                        "details": "test record",
                        "suspend_screenings": kind == "decision",
                    },
                ).raise_for_status()
                expected = 206 if kind == "restoration" else 403
                self.assertEqual(
                    requests.get(
                        other.embed + "/media",
                        headers={"Referer": other.embed, "Range": "bytes=0-5"},
                    ).status_code,
                    expected,
                )
            self.assertEqual(
                [
                    e["kind"]
                    for e in requests.get(endpoint, headers=creator).json()["events"]
                ],
                ["notice", "context", "decision", "appeal", "restoration"],
            )
            history = requests.get(endpoint, headers=creator).json()["events"]
            self.assertEqual(history[-1]["actor"], "Duty curator")

            # Deterministic time progression exercises the *same* grant authorization path before
            # opening and at the closing instant. A copied institution must enforce the saved grant.
            clock = time.time() + 3600
            client = app.test_client()
            with patch("institution.time.time", return_value=clock):
                future = client.post(
                    "/api/grants",
                    headers=headers,
                    json=dict(
                        work=work["id"],
                        version=work["files"][0]["sha256"],
                        origin=a,
                        starts=clock + 10,
                        ends=clock + 30,
                        agreement="Timed exhibition",
                        audience="Members",
                    ),
                ).json
                uri = "/api/grants/" + future["id"] + "/session"
                exhibition_auth = {
                    "Authorization": "Bearer " + future["exhibitor_secret"]
                }
                self.assertEqual(
                    client.post(uri, headers=exhibition_auth).status_code, 403
                )
            with patch("institution.time.time", return_value=clock + 10):
                opened = client.post(uri, headers=exhibition_auth).json
                self.assertEqual(opened["expires"], clock + 30)
            restored = tmp / "restored"
            shutil.copytree(collection, restored)
            with (
                closing(sqlite3.connect(tmp / "operator.sqlite")) as src,
                closing(sqlite3.connect(tmp / "restored.sqlite")) as dst,
            ):
                src.backup(dst)
            restored_client = create_app(
                restored, tmp / "restored.sqlite", secret, "Arts"
            ).test_client()
            with patch("institution.time.time", return_value=clock + 29):
                self.assertEqual(
                    restored_client.get(
                        opened["embed"], headers={"Referer": a + "/"}
                    ).status_code,
                    200,
                )
            with patch("institution.time.time", return_value=clock + 30):
                self.assertEqual(
                    restored_client.get(
                        opened["embed"] + "/media", headers={"Referer": opened["embed"]}
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    restored_client.post(uri, headers=exhibition_auth).status_code, 403
                )


if __name__ == "__main__":
    unittest.main()
