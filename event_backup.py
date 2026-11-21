"""Select independent encoder feeds and keep one continuous live playlist."""

from collections import deque
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import ceil
from pathlib import Path
import re
import secrets
from shutil import copyfile
from threading import Thread


FRESH_FOR = 3.5
FRAGMENT = re.compile(r"feed-([0-9]{9})\.ts")
PROGRAM_ASSET = re.compile(r"0\.m3u8|0-[0-9]{9}\.ts")


@dataclass(frozen=True)
class Fragment:
    path: Path
    duration: float
    date: str
    sequence: int


def snapshot(directory):
    try:
        document = (directory / "feed.m3u8").read_text(encoding="ascii")
    except (FileNotFoundError, PermissionError):
        return []
    fragments = []
    duration, date = 0, ""
    for line in document.splitlines():
        if line.startswith("#EXTINF:"):
            duration = float(line.partition(":")[2].rstrip(","))
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            date = line.partition(":")[2]
        elif duration and date and (name := FRAGMENT.fullmatch(line)):
            path = directory / line
            if path.is_file():
                fragments.append(Fragment(path, duration, date, int(name.group(1))))
            duration, date = 0, ""
    return fragments


@contextmanager
def serve_program(directory):
    # FFmpeg keeps local playlist files open and prevents atomic replacement on Windows.
    prefix = "/" + secrets.token_urlsafe(24) + "/"

    class ProgramMedia(BaseHTTPRequestHandler):
        def do_GET(self):
            filename = self.path.removeprefix(prefix)
            if filename == self.path or not PROGRAM_ASSET.fullmatch(filename):
                self.send_error(404)
                return
            try:
                data = (directory / filename).read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with suppress(ConnectionError):
                self.wfile.write(data)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), ProgramMedia) as server:
        thread = Thread(target=server.serve_forever, name="CuePorch programme loopback", daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}{prefix}0.m3u8"
        finally:
            server.shutdown()
            thread.join()


class Program:
    def __init__(self, path, window):
        self.path = path
        self.window = window
        self.fragments = deque()
        self.sequence = 0
        self.discontinuities = 0
        self.selected = None
        self.last = None
        self.dirty = False
        self.expired = deque()

    def update(self, feeds):
        """Keep a healthy selected encoder, even after the primary reconnects."""
        if not feeds.get(self.selected):
            available = next((name for name, fragments in feeds.items() if fragments), None)
        else:
            available = self.selected
        if available:
            fragments = feeds[available]
            same = (available == self.selected and self.last and
                    self.last.path.parent == fragments[-1].path.parent)
            if same:
                pending = [fragment for fragment in fragments if fragment.sequence > self.last.sequence]
            elif self.last:
                pending = fragments[-1:]
            else:
                pending = fragments
            for fragment in pending:
                try:
                    self.append(fragment, self.last is not None and not same)
                except (FileNotFoundError, PermissionError):
                    break
                self.selected = available
                same = True
        if self.dirty:
            self.publish()
        if not self.dirty:
            while len(self.expired) > 2:
                try:
                    (self.path / self.expired[0]).unlink(missing_ok=True)
                except PermissionError:
                    break
                self.expired.popleft()

    def append(self, fragment, switch):
        name = f"0-{self.sequence:09d}.ts"
        destination = self.path / name
        temporary = destination.with_suffix(".partial")
        copyfile(fragment.path, temporary)
        temporary.replace(destination)
        self.fragments.append((name, fragment, switch))
        self.last = fragment
        self.sequence += 1
        while len(self.fragments) > self.window:
            name, _, changed = self.fragments.popleft()
            self.discontinuities += int(changed)
            self.expired.append(name)
        self.dirty = True

    def publish(self, ended=False):
        duration = ceil(max(fragment.duration for _, fragment, _ in self.fragments))
        lines = ["#EXTM3U", "#EXT-X-VERSION:6", f"#EXT-X-TARGETDURATION:{duration}",
                 f"#EXT-X-MEDIA-SEQUENCE:{self.sequence - len(self.fragments)}",
                 f"#EXT-X-DISCONTINUITY-SEQUENCE:{self.discontinuities}", "#EXT-X-INDEPENDENT-SEGMENTS"]
        for filename, fragment, changed in self.fragments:
            if changed:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.extend([f"#EXT-X-PROGRAM-DATE-TIME:{fragment.date}", f"#EXTINF:{fragment.duration:.6f},", filename])
        if ended:
            lines.append("#EXT-X-ENDLIST")
        target = self.path / "0.m3u8"
        temporary = target.with_suffix(".partial")
        try:
            temporary.write_text("\n".join(lines) + "\n", encoding="ascii")
            temporary.replace(target)
        except PermissionError:
            return
        self.dirty = False
