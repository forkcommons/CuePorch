"""Surface revocable private PeerTube HLS under the CuePorch viewer origin."""

import os
import re
from threading import Lock
import time
from urllib.parse import urlsplit

from flask import Response
import requests

from peertube_destination import PeerTube


HLS_PREFIXES = (
    "/static/streaming-playlists/hls/private/",
    "/object-storage-proxy/streaming-playlists/hls/private/",
)
HLS_FILE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,250}\.(?:m3u8|m4s|mp4|ts|aac|vtt)")
MANIFEST_URI = re.compile(r'\bURI="([^"\r\n]+)"')
RANGE = re.compile(r"bytes=(?:\d+-\d*|-\d+)")
FORMATS = {
    "m3u8": "application/vnd.apple.mpegurl",
    "m4s": "video/iso.segment",
    "mp4": "video/mp4",
    "ts": "video/mp2t",
    "aac": "audio/aac",
    "vtt": "text/vtt",
}


def root_path(origin, url, remote):
    parts = urlsplit(url)
    host = urlsplit(origin)
    if parts.netloc != host.netloc or parts.scheme != host.scheme or parts.query or parts.fragment:
        raise ValueError("A connected host must serve the protected playlist.")
    directory, _, name = parts.path.rpartition("/")
    if directory + "/" not in {prefix + remote + "/" for prefix in HLS_PREFIXES} or not HLS_FILE.fullmatch(name):
        raise ValueError("The protected playlist must belong to this occurrence.")
    return directory, name


def manifest_text(payload):
    """Validate PeerTube's relative manifest references for scoped gateway routing."""
    content = payload.decode("utf-8")
    if not content.startswith("#EXTM3U"):
        raise ValueError("PeerTube returned an invalid playlist.")
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        files = MANIFEST_URI.findall(line) if line.startswith("#") else [line]
        if any(not HLS_FILE.fullmatch(name) for name in files):
            raise ValueError("PeerTube returned a playlist outside this occurrence.")
    return content


class Gateway:
    def __init__(self):
        self.lock = Lock()
        self.credentials = None
        self.authorization = None
        self.expires = 0

    def credential(self, origin):
        settings = (os.environ.get("CUEPORCH_PEERTUBE_ORIGIN", "").rstrip("/"),
                    os.environ.get("PEERTUBE_USERNAME"), os.environ.get("PEERTUBE_PASSWORD"))
        if settings[0] != origin or not all(settings):
            raise ValueError("Connect the protected event's video host.")
        with self.lock:
            if self.credentials != settings or time.monotonic() >= self.expires:
                peer = PeerTube(*settings)
                try:
                    self.authorization = peer.session.headers["Authorization"]
                    self.expires = peer.expires_at
                    self.credentials = settings
                finally:
                    peer.session.close()
            return self.authorization

    def read(self, origin, path, byte_range=None, stream=False):
        headers = {"Authorization": self.credential(origin), "Accept-Encoding": "identity"}
        if byte_range:
            if not RANGE.fullmatch(byte_range):
                raise ValueError("Invalid media byte range.")
            headers["Range"] = byte_range
        response = requests.get(origin + path, headers=headers, allow_redirects=False, timeout=(3, 10), stream=stream)
        if response.status_code in (401, 403):
            with self.lock:
                if self.authorization == headers["Authorization"]:
                    self.expires = 0
        if response.status_code not in (200, 206):
            response.close()
            raise ValueError("The protected video resource is unavailable.")
        return response

    def metadata(self, origin, remote):
        with self.read(origin, "/api/v1/videos/" + remote) as response:
            return response.json()

    def media(self, origin, remote, gateway, filename, byte_range):
        if not HLS_FILE.fullmatch(filename):
            raise ValueError("Unrecognized video resource.")
        suffix = filename.rsplit(".", 1)[-1]
        upstream = HLS_PREFIXES[gateway] + remote + "/" + filename
        response = self.read(origin, upstream, byte_range if suffix != "m3u8" else None, stream=True)
        if suffix == "m3u8":
            with response:
                sample = response.raw.read(512 * 1024 + 1)
                if len(sample) > 512 * 1024:
                    raise ValueError("The protected playlist is too large.")
                result = Response(manifest_text(sample), mimetype=FORMATS[suffix])
        else:
            result = Response(response.iter_content(chunk_size=64 * 1024), status=response.status_code,
                              mimetype=FORMATS[suffix])
            for header in ("Content-Range", "Content-Length", "Accept-Ranges"):
                if header in response.headers:
                    result.headers[header] = response.headers[header]
            result.call_on_close(response.close)
        result.cache_control.no_store = True
        result.headers["X-Content-Type-Options"] = "nosniff"
        return result
