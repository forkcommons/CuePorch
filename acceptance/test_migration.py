"""Run the production migration command against an independently specified source."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CAPTION = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nCuePorch\n"


class Migration(unittest.TestCase):
    def test_rehearsal(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            clip = workspace / "sample.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=160x90:r=12:d=1",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(clip),
                ],
                check=True,
            )
            media = clip.read_bytes()
            requests = []
            expired = True
            limited = True

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def respond(self, data):
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def do_GET(self):
                    nonlocal limited, expired
                    path = urlsplit(self.path).path
                    requests.append((path, self.headers.get("Authorization")))
                    if path.startswith("/asset/"):
                        if self.headers.get("Authorization"):
                            return self.send_error(400)
                        if path == "/asset/2" and expired:
                            expired = False
                            return self.send_error(403)
                        self.send_response(302)
                        self.send_header("Location", path.replace("/asset/", "/cdn/"))
                        self.end_headers()
                        return
                    if path.startswith("/cdn/"):
                        return self.respond(CAPTION if path.endswith("vtt") else media)
                    if self.headers.get("Authorization") != "Bearer cueporch-fixture":
                        return self.send_error(401)
                    if path == "/me":
                        return self.respond(b'{"uri":"/users/91"}')
                    if path == "/me/videos" and limited:
                        limited = False
                        self.send_response(429)
                        self.send_header("Retry-After", "0")
                        self.end_headers()
                        return
                    rows = []
                    following = None
                    if path == "/me/videos":
                        second = "page=2" in self.path
                        numbers = [3] if second else [1, 2]
                        for i in numbers:
                            rows.append(
                                dict(
                                    uri=f"/videos/{i}",
                                    name="Same title",
                                    modified_time="2026-01-01",
                                    duration=1,
                                    privacy=dict(view="nobody", embed="whitelist"),
                                    download=[]
                                    if i == 3
                                    else [
                                        dict(
                                            type="video/mp4",
                                            size=len(media),
                                            quality="source" if i == 1 else "hd",
                                            link=f"{origin}/asset/{i}",
                                        )
                                    ],
                                )
                            )
                        total = 3
                        following = None if second else "/me/videos?page=2"
                    elif path.endswith("/privacy/domains"):
                        rows = [{"domain": "museum.example"}]
                        total = 1
                    elif path.endswith("/chapters"):
                        return self.respond(
                            json.dumps(
                                [{"title": "Call to order", "timecode": 0}]
                            ).encode()
                        )
                    elif path.endswith("/versions"):
                        return self.send_error(403)
                    elif path.endswith("/credits"):
                        rows = [{"name": "Producer", "role": "Director"}]
                        total = 1
                    elif path.endswith("/texttracks"):
                        if path == "/videos/1/texttracks":
                            rows = [
                                dict(
                                    uri=path + "/9",
                                    language="en",
                                    type="subtitles",
                                    link=origin + "/asset/en.vtt",
                                )
                            ]
                        total = len(rows)
                    else:
                        return self.send_error(404)
                    self.respond(
                        json.dumps(
                            dict(total=total, data=rows, paging=dict(next=following))
                        ).encode()
                    )

            with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
                origin = "http://127.0.0.1:" + str(server.server_port)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                root = workspace / "collection"
                cmd = [
                    sys.executable,
                    str(ROOT / "cueporch.py"),
                    "vimeo-transfer",
                    "Museum",
                    str(root),
                    "--simulate",
                    origin,
                ]
                try:
                    subprocess.run(cmd + ["--dry-run"], check=True)
                    self.assertFalse(
                        any(path.startswith("/asset/") for path, _ in requests)
                    )
                    subprocess.run(cmd, check=True)
                    subprocess.run(cmd, check=True)
                finally:
                    server.shutdown()
                    thread.join()
            works = json.loads((root / "manifest.json").read_text())["works"]
            self.assertEqual(len({w["id"] for w in works}), 3)
            self.assertEqual([w["source_id"] for w in works], ["1", "2", "3"])
            self.assertEqual(
                [w["disposition"] for w in works],
                ["original recovered", "playback copy only", "owner file needed"],
            )
            self.assertTrue(
                all(
                    w["credits"][0]["name"] == "Producer"
                    and w["chapters"][0]["timecode"] == 0
                    and w["unavailable_metadata"] == {"/versions": 403}
                    for w in works
                )
            )
            self.assertTrue(
                all(
                    w["publication"] == "private"
                    and w["allowed_domains"] == ["museum.example"]
                    for w in works
                )
            )
            self.assertEqual(
                [(f["role"], f["sha256"]) for f in works[0]["files"]],
                [
                    ("original", hashlib.sha256(media).hexdigest()),
                    ("caption", hashlib.sha256(CAPTION).hexdigest()),
                ],
            )
            self.assertEqual(sum(path == "/asset/2" for path, _ in requests), 2)
            self.assertEqual(works[0]["files"][1]["text_kind"], "subtitles")
            self.assertEqual(sum(path == "/asset/1" for path, _ in requests), 1)
            self.assertFalse(
                any(token for path, token in requests if path.startswith("/cdn/"))
            )
            restored = workspace / "restore"
            shutil.copytree(root, restored)
            subprocess.run(
                [sys.executable, str(ROOT / "cueporch.py"), "verify", str(restored)],
                check=True,
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-loglevel",
                    "error",
                    "-i",
                    str(restored / "objects" / hashlib.sha256(media).hexdigest()),
                    "-f",
                    "null",
                    "-",
                ],
                check=True,
            )


if __name__ == "__main__":
    unittest.main()
