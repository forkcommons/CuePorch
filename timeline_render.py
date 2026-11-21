"""Render an authorized sequence of retained cuts with optional background audio."""

from dataclasses import dataclass
from pathlib import Path

from media_tools import run_media, video_properties


@dataclass(frozen=True)
class Clip:
    source: Path
    start: int
    end: int
    has_audio: bool
    still: bool = False


@dataclass(frozen=True)
class Music:
    source: Path
    start: int
    gain: int


def render(target, clips, size, color, music=None, stopping=None):
    if not clips:
        raise ValueError("Add at least one clip to the timeline.")
    width, height = size
    sources = []
    filters = []
    timeline = []
    total = 0
    for index, clip in enumerate(clips):
        milliseconds = clip.end - clip.start
        length = f"{milliseconds / 1000:.3f}"
        if clip.still:
            sources.extend(("-protocol_whitelist", "file,pipe", "-f", "image2", "-loop", "1",
                            "-framerate", "30", "-t", length, "-i", str(clip.source)))
        else:
            sources.extend(("-protocol_whitelist", "file,pipe", "-ss", f"{clip.start / 1000:.3f}", "-t", length,
                            "-i", str(clip.source)))
        filters.append(
            f"[{index}:V:0]setpts=PTS-STARTPTS,fps=30,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x{color},setsar=1,format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration={length},trim=duration={length}[v{index}]"
        )
        if clip.has_audio and not clip.still:
            audio = f"[{index}:a:0]aresample=48000,aformat=channel_layouts=stereo,apad"
        else:
            audio = "anullsrc=r=48000:cl=stereo"
        filters.append(f"{audio},atrim=duration={length},asetpts=PTS-STARTPTS[a{index}]")
        timeline.append(f"[v{index}][a{index}]")
        total += milliseconds
    duration = f"{total / 1000:.3f}"
    filters.append(f"{''.join(timeline)}concat=n={len(clips)}:v=1:a=1[assembled][speech]")
    audio_out = "[speech]"
    if music is not None:
        sources.extend(("-protocol_whitelist", "file,pipe", "-ss", f"{music.start / 1000:.3f}",
                        "-i", str(music.source)))
        filters.append(
            f"[{len(clips)}:a:0]aresample=48000,aformat=channel_layouts=stereo,volume={music.gain / 100:.2f},"
            f"apad,atrim=duration={duration},asetpts=PTS-STARTPTS[music];"
            "[speech][music]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.97[audio]"
        )
        audio_out = "[audio]"
    result = run_media(
        [
            "ffmpeg", "-nostdin", "-v", "error", *sources, "-filter_complex_threads", "2",
            "-filter_complex", ";".join(filters), "-map", "[assembled]", "-map", audio_out,
            "-t", duration, "-c:v", "libx264", "-threads", "2", "-crf", "20",
            "-c:a", "aac", "-movflags", "+faststart", str(target),
        ],
        timeout=max(180, 12 * total / 1000), stopping=stopping,
    )
    measurement = video_properties(str(target)) if result.returncode == 0 else {}
    length = measurement.get("duration")
    if not length or abs(length - total / 1000) > .1 + len(clips) / 30:
        raise ValueError("The selected clips could not be combined into a complete video.")
    return length
