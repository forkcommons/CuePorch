"""Align zero-based CivicCast WebVTT cues to an MPEG-TS HLS presentation clock."""

import argparse
import json
import os
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path


def presentation_ticks(first_segment):
    output = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=start_time",
            "-of",
            "json",
            str(first_segment),
        ],
        text=True,
    )
    seconds = Decimal(json.loads(output)["streams"][0]["start_time"])
    return round(seconds * 90000) % (1 << 33)


def align(captions, first_segment):
    ticks = presentation_ticks(first_segment)
    header = f"X-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:{ticks}"
    for path in captions.glob("*.vtt"):
        text = path.read_text(encoding="utf-8-sig")
        if "X-TIMESTAMP-MAP=" in text:
            continue
        head, newline, body = text.partition("\n")
        if not head.startswith("WEBVTT") or not newline:
            raise ValueError("Invalid WebVTT header")
        with tempfile.NamedTemporaryFile(
            dir=path.parent, mode="w", encoding="utf8", delete=False
        ) as stream:
            stream.write(head + "\n" + header + "\n" + body)
            staged = Path(stream.name)
        try:
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
    return ticks


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("caption_directory", type=Path)
    p.add_argument(
        "first_program_segment",
        type=Path,
        help="First segment at cue time zero, retained from encoder startup",
    )
    a = p.parse_args()
    print(align(a.caption_directory, a.first_program_segment))
