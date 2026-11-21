"""Translate bounded, relative DASH contribution assets into HLS playlists."""

from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
import re
from xml.etree import ElementTree


NS = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
MAX_MANIFEST = 256 * 1024
MAX_SEGMENTS = 300
MAX_TRACKS = 12
TRACK_ID = re.compile(r"[0-9]{1,4}")
INIT_FILE = re.compile(r"init-stream([0-9]{1,4})\.m4s")
MEDIA_FILE = re.compile(r"chunk-stream([0-9]{1,4})-([0-9]{5,10})\.m4s")
VIDEO_CODEC = re.compile(r"avc[13]\.[0-9a-fA-F]{6}")
AUDIO_CODEC = re.compile(r"mp4a\.40\.(?:2|5|29)")
INIT_TEMPLATE = "init-stream$RepresentationID$.m4s"
MEDIA_TEMPLATE = "chunk-stream$RepresentationID$-$Number%05d$.m4s"


@dataclass(frozen=True)
class Segment:
    sequence: int
    opens: float
    duration: float

    @property
    def ends(self):
        return self.opens + self.duration


@dataclass(frozen=True)
class Track:
    identity: str
    codec: str
    bandwidth: int
    resolution: tuple[int, int] | None
    segments: tuple[Segment, ...]

    @property
    def init_file(self):
        return f"init-stream{self.identity}.m4s"

    def filename(self, sequence):
        return f"chunk-stream{self.identity}-{sequence:05d}.m4s"


def integer(value, maximum, minimum=0):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("The DASH timeline contains an invalid integer.")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError("The DASH timeline exceeds the supported range.")
    return parsed


def segment_timeline(template):
    if (template is None or template.get("initialization") != INIT_TEMPLATE or
            template.get("media") != MEDIA_TEMPLATE):
        raise ValueError("Use relative numbered DASH fragments.")
    scale = integer(template.get("timescale", "1"), 1_000_000_000, 1)
    offset = integer(template.get("presentationTimeOffset", "0"), 10**18)
    sequence = integer(template.get("startNumber", "1"), 4_000_000_000, 1)
    start = offset
    segments = []
    for entry in template.findall("./mpd:SegmentTimeline/mpd:S", NS):
        point = integer(entry.get("t", str(start)), 10**18)
        span = integer(entry.get("d"), scale * 12, 1)
        repeat = integer(entry.get("r", "0"), MAX_SEGMENTS - 1) + 1
        if (point < start and segments) or len(segments) + repeat > MAX_SEGMENTS:
            raise ValueError("The DASH timeline has an overlapping or oversized window.")
        for number in range(repeat):
            segments.append(Segment(sequence + number, (point + number * span - offset) / scale, span / scale))
        sequence += repeat
        start = point + repeat * span
    if not segments:
        raise ValueError("The DASH manifest has no complete fragments.")
    return tuple(segments)


def parse_manifest(payload):
    if not payload or len(payload) > MAX_MANIFEST:
        raise ValueError("The DASH manifest is missing or too large.")
    source = payload.decode("utf-8-sig")
    if "<!DOCTYPE" in source or "<!ENTITY" in source:
        raise ValueError("The DASH manifest contains a document type or entity declaration.")
    try:
        root = ElementTree.fromstring(source)
    except ElementTree.ParseError:
        raise ValueError("The DASH manifest is not valid XML.") from None
    periods = root.findall("mpd:Period", NS)
    if root.tag != f"{{{NS['mpd']}}}MPD" or len(periods) != 1:
        raise ValueError("A DASH contribution uses a single program period.")
    if root.get("type") not in ("dynamic", "static"):
        raise ValueError("The DASH manifest needs a live or finalized presentation type.")
    if any(element.tag.endswith(("}BaseURL", "}SegmentList", "}SegmentBase")) for element in root.iter()):
        raise ValueError("DASH contribution assets must use relative segment templates.")
    tracks = {}
    for adaptation in periods[0].findall("mpd:AdaptationSet", NS):
        shared = adaptation.find("mpd:SegmentTemplate", NS)
        for rendition in adaptation.findall("mpd:Representation", NS):
            identity, codec = rendition.get("id", ""), rendition.get("codecs", "")
            mime = rendition.get("mimeType", adaptation.get("mimeType"))
            if not TRACK_ID.fullmatch(identity) or identity in tracks or len(tracks) >= MAX_TRACKS:
                raise ValueError("Use distinct numeric representation IDs within the supported track limit.")
            if mime == "video/mp4" and VIDEO_CODEC.fullmatch(codec):
                resolution = (integer(rendition.get("width"), 8192, 1), integer(rendition.get("height"), 8192, 1))
            elif mime == "audio/mp4" and AUDIO_CODEC.fullmatch(codec):
                resolution = None
            else:
                raise ValueError("DASH contributions use H.264 video and AAC audio in fragmented MP4.")
            template = rendition.find("mpd:SegmentTemplate", NS)
            tracks[identity] = Track(identity, codec, integer(rendition.get("bandwidth"), 150_000_000, 1),
                                     resolution, segment_timeline(template if template is not None else shared))
    if not any(track.resolution for track in tracks.values()):
        raise ValueError("The DASH manifest needs a video representation.")
    return tracks, root.get("type") == "static"


def master_playlist(tracks, prefix=""):
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    audio = [track for track in tracks.values() if track.resolution is None]
    for index, track in enumerate(audio):
        lines.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="program",NAME="Audio {index + 1}",'
                     f'DEFAULT={"YES" if index == 0 else "NO"},AUTOSELECT=YES,URI="{prefix}{track.identity}.m3u8"')
    audio_bandwidth = max((track.bandwidth for track in audio), default=0)
    audio_codecs = list(dict.fromkeys(track.codec for track in audio))
    for track in tracks.values():
        if track.resolution:
            width, height = track.resolution
            codecs = ",".join([track.codec, *audio_codecs])
            info = f'#EXT-X-STREAM-INF:BANDWIDTH={track.bandwidth + audio_bandwidth},RESOLUTION={width}x{height},CODECS="{codecs}"'
            lines.extend([info + (',AUDIO="program"' if audio else ""), f"{prefix}{track.identity}.m3u8"])
    return "\n".join(lines) + "\n"


def media_playlist(track, segments, ended=False, starts_at=None):
    duration = ceil(max(segment.duration for segment in track.segments))
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{duration}",
             f"#EXT-X-MEDIA-SEQUENCE:{segments[0].sequence}", f'#EXT-X-MAP:URI="{track.init_file}"']
    for fragment in segments:
        lines.append(f"#EXTINF:{fragment.duration:.6f},")
        if starts_at is not None:
            date = datetime.fromtimestamp(starts_at + fragment.opens, timezone.utc)
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{date.isoformat(timespec='milliseconds')}")
        lines.append(track.filename(fragment.sequence))
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
