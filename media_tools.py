"""Prepare the browser-compatible copy retained for exact-cut screenings."""

import subprocess


def prepare_screening(source, target):
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
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
    if result.returncode or not target.is_file():
        raise ValueError("Unable to prepare the screening copy")
    duration = [
        int(value)
        for line in result.stdout.splitlines()
        for key, _, value in [line.partition(b"=")]
        if key == b"out_time_us" and value.isdigit()
    ]
    return duration[-1] / 1_000_000 if duration else None
