"""An account-free source rehearsal using portable media and Vimeo's documented HTTP shapes."""

import argparse
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def build_server(directory, port=0):
    directory.mkdir(parents=True, exist_ok=True)
    clip = directory / "meeting.mp4"
    if not clip.exists():
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=320x180:r=24:d=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=620:duration=4",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(clip),
            ],
            check=True,
        )
    media = clip.read_bytes()
    caption = (
        b"WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nThe public meeting is in session.\n"
    )

    class API(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            if path.startswith("/media/"):
                data = caption if path.endswith(".vtt") else media
            else:
                if self.headers.get("Authorization") != "Bearer cueporch-fixture":
                    return self.send_error(401)
                origin = f"http://127.0.0.1:{self.server.server_port}"
                if path == "/me":
                    data = b'{"uri":"/users/10"}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path == "/me/videos":
                    values = [
                        dict(
                            uri="/videos/71",
                            name="Station meeting",
                            privacy=dict(view="anybody", embed="public"),
                            duration=4,
                            modified_time="2026-11-01",
                            download=[
                                dict(
                                    quality="source",
                                    type="video/mp4",
                                    size=len(media),
                                    link=origin + "/media/71.mp4",
                                )
                            ],
                        ),
                        dict(
                            uri="/videos/72",
                            name="Exhibition cut",
                            privacy=dict(view="nobody", embed="whitelist"),
                            duration=4,
                            modified_time="2026-11-01",
                            download=[
                                dict(
                                    quality="hd",
                                    type="video/mp4",
                                    size=len(media),
                                    link=origin + "/media/72.mp4",
                                )
                            ],
                        ),
                        dict(
                            uri="/videos/73",
                            name="Exhibition cut",
                            privacy=dict(view="nobody", embed="private"),
                            duration=4,
                            modified_time="2026-11-01",
                            download=[],
                        ),
                    ]
                elif path == "/videos/72/privacy/domains":
                    values = [{"domain": "museum.example"}]
                elif path.endswith("/texttracks"):
                    values = (
                        [
                            dict(
                                uri=path + "/1",
                                language="en",
                                link=origin + "/media/captions.vtt",
                            )
                        ]
                        if "/71/" in path
                        else []
                    )
                elif path.endswith("/chapters"):
                    values = (
                        [dict(title="Opening", timecode=0)] if "/71/" in path else []
                    )
                elif path.endswith("/credits"):
                    values = [dict(name="Fixture station", role="Producer")]
                elif path.endswith("/versions"):
                    values = []
                else:
                    return self.send_error(404)
                data = json.dumps(
                    dict(data=values, total=len(values), paging=dict(next=None))
                ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer(("127.0.0.1", port), API)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--port", type=int, default=19002)
    a = p.parse_args()
    with build_server(a.directory, a.port) as server:
        print(
            "Simulated source on http://127.0.0.1:" + str(server.server_port),
            flush=True,
        )
        server.serve_forever()
