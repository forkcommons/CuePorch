"""Read timed media from a connected PeerTube HLS event for local captions."""

from dataclasses import dataclass
from datetime import datetime
import math
import re
import subprocess
from urllib.parse import urlsplit

from event_media import HLS_FILE, HLS_PREFIXES, MANIFEST_URI, manifest_text


MASTER_MAX = 2 * 1024 * 1024
SEGMENT_MAX = 16 * 1024 * 1024


@dataclass(frozen=True)
class Fragment:
    name: str
    start: float
    duration: float
    initialization: str | None


def trusted_directory(origin, remote, address):
    source = urlsplit(origin)
    url = urlsplit(address)
    directory, _, filename = url.path.rpartition("/")
    if (url.scheme, url.netloc) != (source.scheme, source.netloc) or url.query or url.fragment:
        raise ValueError("The live playlist must come from the connected host.")
    if directory + "/" not in {prefix + remote + "/" for prefix in HLS_PREFIXES}:
        raise ValueError("The live playlist must belong to this video.")
    if not HLS_FILE.fullmatch(filename) or not filename.endswith(".m3u8"):
        raise ValueError("The live playlist name is invalid.")
    return directory + "/", filename


def selected_variant(master):
    for line in master.splitlines():
        if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO,"):
            names = MANIFEST_URI.findall(line)
            if len(names) == 1 and HLS_FILE.fullmatch(names[0]) and names[0].endswith(".m3u8"):
                return names[0]
    ranked = []
    for attributes, filename in re.findall(r"^#EXT-X-STREAM-INF:([^\r\n]+)\r?\n([^\r\n]+)", master, re.MULTILINE):
        bandwidth = re.search(r"(?:^|,)BANDWIDTH=(\d+)(?:,|$)", attributes)
        if bandwidth and HLS_FILE.fullmatch(filename) and filename.endswith(".m3u8"):
            ranked.append((int(bandwidth[1]), filename))
    if not ranked:
        raise ValueError("The host did not provide a live rendition for captioning.")
    return min(ranked)[1]


def timed_fragments(playlist):
    start = None
    duration = None
    initialization = None
    for line in playlist.splitlines():
        if line.startswith("#EXT-X-MAP:"):
            sources = MANIFEST_URI.findall(line)
            if len(sources) != 1 or not HLS_FILE.fullmatch(sources[0]):
                raise ValueError("The live initialization resource is invalid.")
            if "BYTERANGE=" in line:
                raise ValueError("This host uses a shared HLS initialization resource.")
            initialization = sources[0]
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            mark = datetime.fromisoformat(line.split(":", 1)[1])
            if mark.tzinfo is None:
                raise ValueError("The live timestamp has no time zone.")
            start = mark.timestamp()
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("The live fragment duration is invalid.")
        elif line == "#EXT-X-DISCONTINUITY":
            start = None
        elif line.startswith(("#EXT-X-KEY:", "#EXT-X-BYTERANGE:")):
            raise ValueError("This HLS audio format is unavailable for captioning.")
        elif line and not line.startswith("#") and start is not None and duration is not None:
            if not HLS_FILE.fullmatch(line):
                raise ValueError("The live fragment name is invalid.")
            yield Fragment(line, start, duration, initialization)
            start += duration
            duration = None


class LiveAudio:
    def __init__(self, media, origin, remote, playlist):
        self.media = media
        self.origin = origin
        self.directory, master = trusted_directory(origin, remote, playlist)
        self.variant = selected_variant(manifest_text(self.read(master, MASTER_MAX)))
        self.init_name = None
        self.init_data = b""

    def read(self, name, limit):
        if not HLS_FILE.fullmatch(name):
            raise ValueError("Unexpected HLS resource.")
        with self.media.read(self.origin, self.directory + name, stream=True) as response:
            payload = response.raw.read(limit + 1)
        if len(payload) > limit:
            raise ValueError("The HLS resource exceeded the decoder limit.")
        return payload

    def fragments(self):
        return list(timed_fragments(manifest_text(self.read(self.variant, MASTER_MAX))))

    def pcm(self, fragment):
        if self.init_name != fragment.initialization:
            self.init_data = self.read(fragment.initialization, MASTER_MAX) if fragment.initialization else b""
            self.init_name = fragment.initialization
        media = self.init_data + self.read(fragment.name, SEGMENT_MAX)
        decode = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "pipe", "-i", "pipe:0", "-map", "0:a:0",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
            input=media, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if decode.returncode:
            raise ValueError("The live rendition had no decodable audio.")
        samples = round(fragment.duration * 16000) * 2
        return decode.stdout[:samples].ljust(samples, b"\0")
