"""Read-only operator playback on loopback. Run: python preview.py COLLECTION"""

import argparse
import html
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cueporch import verify


def serve(root, port):
    verify(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    works = {w["id"]: w for w in manifest["works"]}
    formats = {"mp4": "video/mp4", "vtt": "text/vtt; charset=utf-8"}
    objects = {
        f["sha256"]: formats[f["format"]]
        for w in works.values()
        for f in w["files"]
        if f["format"] in formats
    }
    esc = html.escape

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond(True)

        def do_HEAD(self):
            self.respond(False)

        def respond(self, body):
            if self.path == "/":
                rows = "".join(
                    f'<li><a href="/watch/{w["id"]}">{esc(w["title"])}</a> · '
                    f"{esc(w['disposition'])}</li>"
                    for w in works.values()
                )
                return self.page(
                    "CuePorch · Private collection", "<ul>" + rows + "</ul>", body
                )
            section, _, identity = self.path.lstrip("/").partition("/")
            if section == "watch" and identity in works:
                work = works[identity]
                video = next(
                    (
                        f
                        for f in work["files"]
                        if f["role"] in ("playback", "original")
                        and f["format"] == "mp4"
                    ),
                    None,
                )
                player = "<p>Awaiting playable media.</p>"
                if video:
                    tracks = "".join(
                        f'<track kind="captions" src="/media/{f["sha256"]}" '
                        f'srclang="{esc(f.get("language", "und"))}" '
                        f'label="{esc(f.get("provenance", "Captions"))}">'
                        for f in work["files"]
                        if f["role"] == "caption" and f["format"] == "vtt"
                    )
                    player = f'<video controls preload="metadata" src="/media/{video["sha256"]}">{tracks}</video>'
                return self.page(
                    work["title"],
                    player + '<p><a href="/">Back to collection</a></p>',
                    body,
                )
            if section != "media" or identity not in objects:
                return self.send_error(404)
            path = root / "objects" / identity
            size = path.stat().st_size
            start, end = 0, size - 1
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                if match and any(match.groups()):
                    lo, hi = match.groups()
                    start = int(lo) if lo else max(0, size - int(hi))
                    end = min(int(hi), end) if lo and hi else end
                else:
                    start = size
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            self.send_response(206 if requested else 200)
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", objects[identity])
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            if body:
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        data = stream.read(min(remaining, 262144))
                        if not data:
                            return
                        try:
                            self.wfile.write(data)
                        except (
                            ConnectionResetError,
                            BrokenPipeError,
                            ConnectionAbortedError,
                        ):
                            return
                        remaining -= len(data)

        def page(self, title, content, body):
            page = (
                f'<!doctype html><html lang="en"><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{esc(title)}</title><style>body{{max-width:58rem;margin:3rem auto;"
                f"padding:0 1rem;font:1.1rem/1.6 system-ui}}video{{width:100%}}</style>"
                f"<main><h1>{esc(title)}</h1>{content}</main></html>"
            ).encode()
            self.send_response(200)
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            if body:
                self.wfile.write(page)

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.collection, args.port)
