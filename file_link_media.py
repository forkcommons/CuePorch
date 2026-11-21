"""Render progressive MP4 and shared HLS/DASH fragments for a retained cut."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from media_tools import LOCAL_FFMPEG, run_media, video_properties


RECIPE = "h264-aac-1"
LEVELS = ((1080, 5500), (720, 3000), (540, 1800), (360, 900), (240, 450))


def variants(source, destination, stopping):
    inventory = destination / "renditions.json"
    if inventory.is_file():
        return json.loads(inventory.read_text(encoding="utf8"))

    info = video_properties(source)
    if not info.get("height") or not info.get("width"):
        raise ValueError("CuePorch could not determine the stored video's resolution.")
    levels = [(height, rate) for height, rate in LEVELS if height <= info["height"]]
    levels = levels or [(info["height"] // 2 * 2, 450)]
    if levels[0][0] < 2:
        raise ValueError("The stored video is too small to prepare playback links.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".render-", dir=destination.parent) as scratch:
        stage = Path(scratch) / "files"
        stage.mkdir()
        rendered = []
        inputs, maps = [], []
        for index, (height, rate) in enumerate(levels):
            name = f"{height}p.mp4"
            target = stage / name
            result = run_media([
                *LOCAL_FFMPEG, "-v", "error", "-i", str(source), "-map", "0:V:0", "-map", "0:a:0?",
                "-map_metadata", "-1", "-map_chapters", "-1", "-vf",
                f"scale=w='max(2,trunc(dar*oh/2)*2)':h='trunc(min(ih,{height})/2)*2'",
                "-threads", "2", "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-maxrate", f"{rate}k", "-bufsize", f"{rate * 2}k", "-pix_fmt", "yuv420p",
                "-sc_threshold", "0", "-force_key_frames", "expr:gte(t,n_forced*4)",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", str(target),
            ], timeout=14400, stopping=stopping)
            if result.returncode:
                raise ValueError("Unable to encode a progressive video rendition.")
            result_info = video_properties(target)
            if not result_info.get("width") or not result_info.get("height"):
                raise ValueError("Unable to read an encoded video rendition.")
            rendered.append(dict(name=name, width=result_info["width"], height=result_info["height"],
                                 bytes=target.stat().st_size))
            inputs.extend(["-i", str(target)])
            maps.extend(["-map", f"{index}:v:0"])
        audio = ["-map", "0:a:0"] if video_properties(stage / rendered[0]["name"]).get("audio") else []
        sets = "id=0,streams=v id=1,streams=a" if audio else "id=0,streams=v"
        result = run_media([
            *LOCAL_FFMPEG, "-v", "error", *inputs, *maps, *audio, "-c", "copy",
            "-f", "dash", "-seg_duration", "4", "-use_template", "1", "-use_timeline", "1",
            "-adaptation_sets", sets, "-hls_playlist", "1", "-hls_master_name", "hls.m3u8",
            (stage / "dash.mpd").as_posix(),
        ], timeout=14400, stopping=stopping)
        if result.returncode or not (stage / "dash.mpd").is_file() or not (stage / "hls.m3u8").is_file():
            raise ValueError("Unable to prepare HLS and DASH manifests.")
        (stage / inventory.name).write_text(json.dumps(rendered), encoding="utf8")
        stage.rename(destination)
        return rendered
