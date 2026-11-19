"""Prepare the browser-compatible copy retained for exact-cut screenings."""

import re
import subprocess
from functools import lru_cache


def prepare_screening(source, target, input_format=None):
    decoder = ["-f", input_format] if input_format else []
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            *decoder,
            "-i",
            str(source),
            "-map",
            "0:V:0",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "-1",
            "-vf",
            "scale=w='min(1280,iw)':h=-2",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            str(target),
        ],
        capture_output=True,
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


@lru_cache(maxsize=256)
def video_properties(path):
    """Read dimensions and nominal frame rate from the packaged FFmpeg."""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(path),
            ],
            capture_output=True,
            encoding="utf8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    for line in result.stderr.splitlines():
        if "Video:" not in line or "(attached pic)" in line:
            continue
        size = re.search(r"\b([1-9]\d{1,4})x([1-9]\d{1,4})\b", line)
        rate = re.search(r"(\d+(?:\.\d+)?) fps\b", line)
        return {
            "width": int(size[1]) if size else None,
            "height": int(size[2]) if size else None,
            "fps": float(rate[1]) if rate else None,
        }
    return {}
