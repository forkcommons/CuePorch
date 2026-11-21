"""Align interpreter HLS audio to the programme's local MPEG-TS timeline."""

from datetime import datetime, timezone
from fractions import Fraction
import math
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from time import time
from uuid import uuid4

from event_audio import timed_fragments
from event_media import manifest_text


RATE = 48000
AAC_FRAME = 1024
PTS_PERIOD = (1 << 33) / 90000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def sample_span(fragment):
    if not (math.isfinite(fragment.start) and math.isfinite(fragment.duration) and 0 < fragment.duration <= 30):
        raise ValueError("Invalid audio window.")
    return round(fragment.start * RATE), round((fragment.start + fragment.duration) * RATE)


def aligned_pcm(target, sources, read_pcm, fallback_pcm=b""):
    start, end = sample_span(target)
    size = (end - start) * 2
    raw = fallback_pcm[:size]
    raw = raw[:len(raw) - len(raw) % 2]
    output = bytearray(raw.ljust(size, b"\0"))
    for source in sources:
        left, right = sample_span(source)
        start_overlap = max(start, left)
        end_overlap = min(end, right)
        if end_overlap <= start_overlap:
            continue
        pcm = read_pcm(source)
        offset = start_overlap - left
        length = min(end_overlap - start_overlap, len(pcm) // 2 - offset)
        if length <= 0:
            continue
        destination = (start_overlap - start) * 2
        output[destination:destination + length * 2] = pcm[offset * 2:(offset + length) * 2]
    return bytes(output)


def first_video_pts(path):
    response = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file",
         "-copyts", "-f", "mpegts", "-i", str(path), "-map", "0:v:0", "-c", "copy",
         "-frames:v", "1", "-avoid_negative_ts", "disabled", "-f", "framecrc", "pipe:1"],
        check=True, capture_output=True, timeout=7, creationflags=NO_WINDOW,
    )
    lines = response.stdout.decode("ascii").splitlines()
    time_base = next(line.split(": ", 1)[1] for line in lines if line.startswith("#tb 0: "))
    packet = next(line for line in lines if line.startswith("0,"))
    return float(int(packet.split(",")[2]) * Fraction(time_base))


def decode_pcm(path):
    response = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file",
         "-f", "mpegts", "-i", str(path), "-t", "30", "-map", "0:a:0",
         "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"],
        check=True, capture_output=True, timeout=7, creationflags=NO_WINDOW,
    )
    return response.stdout


def encode_audio_segment(pcm, pts, destination):
    if not pcm or not math.isfinite(pts):
        raise ValueError("Missing synchronized audio samples.")
    temp = destination.with_suffix(".tmp")
    # AAC encoding emits a priming packet before the first input timestamp.
    offset = (pts % PTS_PERIOD) + AAC_FRAME / RATE
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "pipe,file",
             "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0",
             "-c:a", "aac", "-b:a", "128k", "-mpegts_copyts", "1", "-muxdelay", "0",
             "-output_ts_offset", str(offset), "-f", "mpegts", str(temp)],
            input=pcm, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            check=True, timeout=7, creationflags=NO_WINDOW,
        )
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)


def discontinuities(lines):
    base, marks, pending = None, [], False
    for line in lines:
        if line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            base = int(line.partition(":")[2])
        elif line == "#EXT-X-DISCONTINUITY":
            pending = True
            if base is None:
                base = 0
        elif line and not line.startswith("#"):
            marks.append(pending)
            pending = False
    return base, marks


def paired_playlists(primary_text, selected, language, name):
    lines = primary_text.splitlines()
    target_duration = next(int(line.split(":", 1)[1]) for line in lines
                           if line.startswith("#EXT-X-TARGETDURATION:"))
    primary = list(timed_fragments(primary_text))
    index = primary.index(selected[0])
    if primary[index:index + len(selected)] != selected:
        raise ValueError("The aligned audio must cover consecutive video fragments.")
    sequence = next((int(line.split(":", 1)[1]) for line in lines
                     if line.startswith("#EXT-X-MEDIA-SEQUENCE:")), 0) + index
    headers = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target_duration}",
               f"#EXT-X-MEDIA-SEQUENCE:{sequence}"]
    discontinuity_base, markers = discontinuities(lines)
    if discontinuity_base is not None:
        headers.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuity_base + sum(markers[:index])}")
    video, audio = headers.copy(), headers.copy()
    for fragment, changed in zip(selected, markers[index:index + len(selected)], strict=True):
        if not re.fullmatch(r"0-\d+\.ts", fragment.name) or fragment.initialization:
            raise ValueError("Invalid programme segment.")
        tags = ["#EXT-X-DISCONTINUITY"] if changed else []
        if discontinuity_base is None:
            date = datetime.fromtimestamp(fragment.start, timezone.utc).isoformat()
            tags.append(f"#EXT-X-PROGRAM-DATE-TIME:{date}")
        tags.append(f"#EXTINF:{fragment.duration:.6f},")
        video.extend([*tags, fragment.name])
        audio.extend([*tags, "1-" + fragment.name[2:]])
    return audio_master(language, name), "\n".join(video) + "\n", "\n".join(audio) + "\n"


def audio_master(language, name):
    if not re.fullmatch(r"[a-z0-9-]+", language):
        raise ValueError("Invalid interpreter language.")
    safe_name = " ".join(name.replace('"', "'").split())
    return "\n".join([
        "#EXTM3U", "#EXT-X-VERSION:3",
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Original",DEFAULT=YES,AUTOSELECT=YES',
        f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="{safe_name}",LANGUAGE="{language}",'
        'DEFAULT=NO,AUTOSELECT=YES,URI="1.m3u8"',
        '#EXT-X-STREAM-INF:BANDWIDTH=12000000,AUDIO="audio"',
        "2.m3u8",
    ]) + "\n"


def read_timeline(directory, rendition, allow_discontinuity=False):
    with (directory / f"{rendition}.m3u8").open("rb") as playlist:
        payload = playlist.read(256 * 1024 + 1)
    if len(payload) > 256 * 1024:
        raise ValueError("The timed playlist is too large.")
    text = manifest_text(payload)
    if not allow_discontinuity and "#EXT-X-DISCONTINUITY" in text:
        raise ValueError("The input timeline changed.")
    fragments = list(timed_fragments(text))
    if (len(fragments) != sum(bool(line) and not line.startswith("#") for line in text.splitlines()) or
            any(f.initialization or not re.fullmatch(rf"{rendition}-\d+\.ts", f.name) for f in fragments)):
        raise ValueError("The input timing is incomplete.")
    return text, fragments


def number(fragment):
    return int(fragment.name.partition("-")[2].removesuffix(".ts"))


class AlignedAudio:
    manifests = ("master.m3u8", "1.m3u8", "2.m3u8")

    def __init__(self, primary, recording=None, *, allow_discontinuity=False):
        self.primary = primary
        self.recording = recording
        self.allow_discontinuity = allow_discontinuity
        self.identity = uuid4().hex
        self.path = None
        self.latest = None
        self.decoded = {}

    def available(self):
        path = self.path
        try:
            return path is not None and time() - (path / "master.m3u8").stat().st_mtime < 6
        except OSError:
            return False

    def resource(self, filename):
        if filename in self.manifests or re.fullmatch(r"1-\d+\.ts", filename):
            path = self.path
            if path is None:
                raise ValueError("The interpreter ended.")
            return path, self.manifests
        if re.fullmatch(r"0-\d+\.ts", filename):
            return self.primary.path, ()
        raise ValueError("Unrecognized interpreted video resource.")

    def render(self, target, sources, item):
        base_file = self.primary.path / target.name
        try:
            fallback = decode_pcm(base_file)
        except subprocess.CalledProcessError:
            fallback = b""
        pcm = aligned_pcm(target, sources, lambda source: self.decoded[source.name], fallback)
        destination = self.path / ("1-" + target.name[2:])
        encode_audio_segment(pcm, first_video_pts(base_file), destination)
        if self.recording is not None:
            self.recording.audio(destination, item, bool(sources))

    def update(self, item):
        primary_text, programme = read_timeline(self.primary.path, 0, self.allow_discontinuity)
        if len(programme) < 3:
            return
        interpreter_path = item.path
        interpreter = []
        if interpreter_path is not None:
            try:
                _, interpreter = read_timeline(interpreter_path, 1)
            except (OSError, ValueError):
                pass
        # Refresh only audio needed for this pass. Capture paths change on reconnect.
        self.decoded = {}
        candidates = programme[:-1] if self.latest is not None else programme[-5:-1]
        changed = False
        for target in candidates:
            if not item.live() or (self.latest is not None and number(target) <= self.latest):
                continue
            sources = []
            for source in interpreter:
                if source.start >= target.start + target.duration or source.start + source.duration <= target.start:
                    continue
                if source.name not in self.decoded:
                    try:
                        self.decoded[source.name] = decode_pcm(interpreter_path / source.name)
                    except (OSError, subprocess.SubprocessError):
                        self.decoded[source.name] = b""
                if self.decoded[source.name]:
                    sources.append(source)
            if self.latest is None and not sources:
                continue
            self.render(target, sources, item)
            self.latest = number(target)
            changed = True
        if not changed:
            return
        selected = []
        for target in programme[:-1]:
            if (self.path / ("1-" + target.name[2:])).is_file():
                selected.append(target)
            else:
                selected.clear()
        if len(selected) >= 2:
            master, video, audio = paired_playlists(primary_text, selected, item.language, item.name)
            for name, content in zip(("2.m3u8", "1.m3u8", "master.m3u8"), (video, audio, master)):
                temporary = self.path / (name + ".tmp")
                temporary.write_text(content, encoding="utf-8")
                temporary.replace(self.path / name)
        for segment in self.path.glob("1-*.ts"):
            if int(segment.stem[2:]) < number(programme[0]) - 2:
                segment.unlink(missing_ok=True)

    def run(self, item):
        with TemporaryDirectory(dir=item.directory, prefix="aligned-", ignore_cleanup_errors=True) as temporary:
            self.path = Path(temporary)
            try:
                while item.live():
                    try:
                        self.update(item)
                    except (OSError, ValueError, LookupError, StopIteration, subprocess.SubprocessError):
                        pass
                    item.stop.wait(.2)
            finally:
                self.path = None
