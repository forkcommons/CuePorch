"""Prepare the browser-compatible copy retained for exact-cut screenings."""

import re
import os
import subprocess
import time
from functools import lru_cache


LOCAL_FFMPEG = ["ffmpeg", "-nostdin", "-protocol_whitelist", "file,pipe"]


def run_media(args, timeout=None, stopping=None):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    if stopping is None:
        return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags)
    deadline = time.monotonic() + timeout if timeout is not None else None
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags) as process:
        try:
            while not stopping.is_set():
                remaining = 0.4 if deadline is None else deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(remaining, 0.4))
                    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
            raise InterruptedError("The service is closing.")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


def prepare_screening(source, target, input_format=None, *, interval=None, full_resolution=False, stopping=None):
    decoder = ["-f", input_format] if input_format else []
    clip = []
    if interval is not None:
        start, end = interval
        decoder.extend(["-ss", f"{start:.3f}"])
        clip = ["-t", f"{end - start:.3f}"]
    width = "iw" if full_resolution else "min(1280,iw)"
    result = run_media(
        [
            *LOCAL_FFMPEG,
            "-v",
            "error",
            *decoder,
            "-i",
            str(source),
            *clip,
            "-map",
            "0:V:0",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "-1",
            "-vf",
            f"scale=w='trunc({width}/2)*2':h=-2" if full_resolution else "scale=w='min(1280,iw)':h=-2",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-crf",
            "23",
            "-c:a",
            "aac",
            *(["-pix_fmt", "yuv420p"] if interval is not None else []),
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            str(target),
        ],
        stopping=stopping,
    )
    if result.returncode or not target.is_file() or not target.stat().st_size:
        raise ValueError("Unable to prepare the screening copy")
    duration = [
        int(value)
        for line in result.stdout.splitlines()
        for key, _, value in [line.partition(b"=")]
        if key == b"out_time_us" and value.isdigit()
    ]
    return duration[-1] / 1_000_000 if duration else None


def make_gif(source, target, start, end):
    result = run_media(
        [
            *LOCAL_FFMPEG,
            "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
            "-filter_complex_threads", "1",
            "-filter_complex",
            "[0:V:0]fps=12,scale=w='min(480,iw)':h=-2:flags=lanczos,split[a][b];"
            "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer",
            "-an", "-t", f"{end - start:.3f}", "-loop", "0", str(target),
        ],
        timeout=180,
    )
    if result.returncode or not target.is_file() or not target.stat().st_size:
        raise ValueError("Unable to make an animated GIF from this selection.")


@lru_cache(maxsize=256)
def video_properties(path):
    """Read dimensions and nominal frame rate from the packaged FFmpeg."""
    try:
        result = run_media(
            [
                *LOCAL_FFMPEG,
                "-hide_banner",
                "-i",
                str(path),
            ],
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    description = result.stderr.decode("utf-8", errors="replace")
    audio = re.search(r"Audio: ([^,\r\n]+)(?:, (\d+ Hz), ([^,\r\n]+))?", description)
    audio_info = ", ".join(part for part in audio.groups() if part) if audio else None
    duration = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", description)
    for line in description.splitlines():
        if "Video:" not in line or "(attached pic)" in line:
            continue
        size = re.search(r"\b([1-9]\d{1,4})x([1-9]\d{1,4})\b", line)
        rate = re.search(r"(\d+(?:\.\d+)?) fps\b", line)
        return {
            "width": int(size[1]) if size else None,
            "height": int(size[2]) if size else None,
            "fps": float(rate[1]) if rate else None,
            "audio": audio_info,
            "duration": (int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])) if duration else None,
        }
    return {}
