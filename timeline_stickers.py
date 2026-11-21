"""Prepare retained image assets for the timeline editor."""

import re
import uuid

from flask import abort
from PIL import Image, ImageOps

from catalog import video_version_key

POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")


def normalize_sticker(raw, get_work, find_cut):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        abort(422, "Sticker configuration must be an object.")

    work_id = raw.get("work")
    cut_id = raw.get("cut")
    position = raw.get("position")
    size = raw.get("size")

    if not isinstance(work_id, str) or not work_id:
        abort(422, "Sticker work must be a non-empty string.")
    if not isinstance(cut_id, str) or re.fullmatch(r"[0-9a-f]{64}", cut_id) is None:
        abort(422, "Sticker cut must be a lowercase 64-character hex string.")
    if not isinstance(position, str) or position not in POSITIONS:
        abort(422, "Sticker position is invalid.")
    if type(size) is not int or not 10 <= size <= 60:
        abort(422, "Sticker size must be an integer between 10 and 60.")

    work = get_work(work_id)
    if work.get("asset_type") != "image":
        abort(422, "Sticker source work must be an image.")

    cut = find_cut(work, cut_id)
    return {
        "work": work["id"],
        "cut": video_version_key(cut),
        "position": position,
        "size": size,
    }


def sticker_source(root, cut):
    for file in cut["files"]:
        if file.get("role") != "original":
            continue
        if file.get("format") not in ("gif", "png", "jpeg", "jpg", "webp"):
            continue
        sha = file.get("sha256")
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
            continue
        path = root / "objects" / sha
        try:
            with open(path, "rb"):
                pass
        except OSError:
            continue
        return path
    raise ValueError("Sticker source image is unavailable.")


def prepare_sticker(source, scratch, canvas, sticker, preview=False):
    width, height = canvas
    shortest = min(canvas)
    box = max(1, round(shortest * sticker["size"] / 100))
    padding = round(shortest * .04)
    position = sticker["position"]
    frames, durations = [], []
    x = y = 0
    try:
        with Image.open(source, formats=("GIF", "PNG", "JPEG", "WEBP")) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError("Sticker images can contain up to 16 million pixels.")
            start = int(image.format == "PNG" and bool(image.info.get("default_image")))
            count = getattr(image, "n_frames", 1) - start
            if not 1 <= count <= 120:
                raise ValueError("Choose a sticker with up to 120 animation frames.")
            for index in range(start, start + count):
                image.seek(index)
                if image.format == "JPEG":
                    frame = ImageOps.exif_transpose(image).convert("RGBA")
                else:
                    frame = image.convert("RGBA")
                with frame:
                    scale = box / max(frame.size)
                    size = tuple(max(1, round(side * scale)) for side in frame.size)
                    scaled = frame.resize(size, Image.Resampling.LANCZOS)
                if position == "center":
                    x, y = (width - size[0]) // 2, (height - size[1]) // 2
                else:
                    x = padding if position.endswith("left") else width - size[0] - padding
                    y = padding if position.startswith("top") else height - size[1] - padding
                if preview:
                    with scaled:
                        frame = Image.new("RGBA", canvas)
                        frame.paste(scaled, (x, y))
                    frames.append(frame)
                else:
                    frames.append(scaled)
                duration = image.info.get("duration", 100)
                durations.append(round(duration) if isinstance(duration, (int, float))
                                 and 1 <= duration <= 65535 else 100)

        path = scratch / (uuid.uuid4().hex + ".png")
        if len(frames) == 1:
            frames[0].save(path)
        else:
            frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations,
                           loop=0, disposal=0, blend=0, default_image=False)
            with Image.open(path) as result:
                animated = result.is_animated
            if animated:
                path = path.rename(path.with_suffix(".apng"))
        return path, x, y
    except (OSError, EOFError, Image.DecompressionBombError):
        raise ValueError("CuePorch could not decode or prepare this sticker image.") from None
    finally:
        for frame in frames:
            frame.close()
