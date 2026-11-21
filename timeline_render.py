"""Render an authorized sequence of retained cuts with optional background audio."""

from dataclasses import dataclass
from pathlib import Path

from media_tools import run_media, video_properties


@dataclass(frozen=True)
class Sticker:
    path: Path
    x: int
    y: int


@dataclass(frozen=True)
class Clip:
    source: Path
    start: int
    end: int
    has_audio: bool
    still: bool = False
    graphic: Path | None = None
    sticker: Sticker | None = None
    text: Path | None = None


@dataclass(frozen=True)
class Music:
    source: Path
    start: int
    gain: int


def render(target, clips, size, color, music=None, stopping=None, *, credit_metadata=None):
    if not clips:
        raise ValueError("Add at least one clip to the timeline.")
    width, height = size
    sources = []
    filters = []
    timeline = []
    total = 0
    video_input = 0
    for index, clip in enumerate(clips):
        milliseconds = clip.end - clip.start
        length = f"{milliseconds / 1000:.3f}"
        clip_input = video_input
        if clip.still:
            sources.extend(("-protocol_whitelist", "file,pipe", "-f", "image2", "-loop", "1",
                            "-framerate", "30", "-t", length, "-i", str(clip.source)))
        else:
            sources.extend(("-protocol_whitelist", "file,pipe", "-ss", f"{clip.start / 1000:.3f}", "-t", length,
                            "-i", str(clip.source)))
        video_input += 1
        base_filter = (
            f"setpts=PTS-STARTPTS,fps=30,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x{color},setsar=1,format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration={length},trim=duration={length}"
        )
        has_graphic = clip.graphic is not None
        has_sticker = clip.sticker is not None
        # Choose the label the base stream is written to so the clip chain
        # always ends on [v{index}] regardless of graphic/sticker presence.
        has_text = clip.text is not None
        if not has_graphic and not has_sticker and not has_text:
            base_out = f"v{index}"
        else:
            base_out = f"b{index}"
        chain_parts = [f"[{clip_input}:V:0]{base_filter}[{base_out}]"]
        current = base_out
        if has_graphic:
            graphic_input = video_input
            sources.extend(("-protocol_whitelist", "file,pipe", "-f", "image2",
                            "-framerate", "30", "-i", str(clip.graphic)))
            video_input += 1
            graphic_out = f"g{index}" if has_sticker or has_text else f"v{index}"
            chain_parts.append(
                f"[{current}][{graphic_input}:V:0]overlay=0:0:eof_action=repeat[{graphic_out}]"
            )
            current = graphic_out
        if has_sticker:
            sticker = clip.sticker
            sticker_input = video_input
            is_apng = sticker.path.suffix.lower() == ".apng"
            if is_apng:
                sources.extend(("-protocol_whitelist", "file,pipe", "-f", "apng",
                                "-ignore_loop", "0", "-i", str(sticker.path)))
                overlay_opts = "shortest=1"
            else:
                sources.extend(("-protocol_whitelist", "file,pipe", "-f", "image2",
                                "-framerate", "30", "-i", str(sticker.path)))
                overlay_opts = "eof_action=repeat:shortest=0"
            video_input += 1
            sticker_out = f"s{index}" if has_text else f"v{index}"
            chain_parts.append(
                f"[{current}][{sticker_input}:V:0]overlay={sticker.x}:{sticker.y}:{overlay_opts}[{sticker_out}]"
            )
            current = sticker_out
        if clip.text is not None:
            path = str(clip.text)
            if clip.text.suffix.lower() == ".apng":
                sources.extend(["-f", "apng", "-ignore_loop", "1",
                                   "-protocol_whitelist", "file,pipe",
                                   "-i", path])
            else:
                sources.extend(["-f", "image2", "-framerate", "30",
                                   "-protocol_whitelist", "file,pipe",
                                   "-i", path])
            text_input = video_input
            video_input += 1
            chain_parts.append(
                f"[{current}][{text_input}:V:0]overlay=0:0:eof_action=repeat:shortest=0[v{index}]"
            )
        filters.append(";".join(chain_parts))
        if clip.has_audio and not clip.still:
            audio = f"[{clip_input}:a:0]aresample=48000,aformat=channel_layouts=stereo,apad"
        else:
            audio = "anullsrc=r=48000:cl=stereo"
        filters.append(f"{audio},atrim=duration={length},asetpts=PTS-STARTPTS[a{index}]")
        timeline.append(f"[v{index}][a{index}]")
        total += milliseconds
    duration = f"{total / 1000:.3f}"
    filters.append(f"{''.join(timeline)}concat=n={len(clips)}:v=1:a=1[assembled][speech]")
    audio_out = "[speech]"
    if music is not None:
        music_input = video_input
        sources.extend(("-protocol_whitelist", "file,pipe", "-ss", f"{music.start / 1000:.3f}",
                        "-i", str(music.source)))
        filters.append(
            f"[{music_input}:a:0]aresample=48000,aformat=channel_layouts=stereo,volume={music.gain / 100:.2f},"
            f"apad,atrim=duration={duration},asetpts=PTS-STARTPTS[music];"
            "[speech][music]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.97[audio]"
        )
        audio_out = "[audio]"
    metadata_output = []
    if credit_metadata is not None:
        sources += ["-protocol_whitelist", "file,pipe", "-f", "ffmetadata", "-i", str(credit_metadata)]
        metadata_output = ["-map_metadata", str(video_input + int(music is not None))]
    result = run_media(
        [
            "ffmpeg", "-nostdin", "-v", "error", *sources, "-filter_complex_threads", "2",
            "-filter_complex", ";".join(filters), "-map", "[assembled]", "-map", audio_out,
            "-t", duration, "-c:v", "libx264", "-threads", "2", "-crf", "20",
            "-c:a", "aac", "-movflags", "+faststart", *metadata_output, str(target),
        ],
        timeout=max(180, 12 * total / 1000), stopping=stopping,
    )
    measurement = video_properties(str(target)) if result.returncode == 0 else {}
    length = measurement.get("duration")
    if not length or abs(length - total / 1000) > .1 + len(clips) / 30:
        raise ValueError("The selected clips could not be combined into a complete video.")
    return length
