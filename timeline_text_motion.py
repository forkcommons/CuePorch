"""Draw the same text effects for editor previews and finished video."""

import math
from contextlib import ExitStack
from pathlib import Path

from PIL import Image

from timeline_text import MOTIONS
from timeline_text_raster import draw_layer


def _opacity(image, factor):
    with image.getchannel("A") as alpha, alpha.point(lambda value: round(value * factor)) as scaled:
        image.putalpha(scaled)
    return image


def _clip_in(image, bounds, phase, axis):
    left, top, right, bottom = bounds
    if axis == "x":
        right = left + int((right - left) * phase)
    else:
        bottom = top + int((bottom - top) * phase)
    with image.getchannel("A") as alpha, Image.new("L", image.size) as revealed:
        if left < right and top < bottom:
            with alpha.crop((left, top, right, bottom)) as visible:
                revealed.paste(visible, (left, top))
        image.putalpha(revealed)
    return image


def _motion(image, effect, phase):
    if phase >= 1 or effect not in MOTIONS or effect == "typewriter":
        return image
    bounds = image.getbbox()
    if bounds is None:
        return image
    if effect == "fade":
        return _opacity(image, phase)
    if effect == "flicker":
        # One brief dip in brightness during the entrance.
        return _opacity(image, 1 - .85 * max(0, 1 - abs(phase - .44) * 5))
    if effect == "reveal":
        return _clip_in(image, bounds, phase, "x")
    if effect == "wipe":
        return _clip_in(image, bounds, phase, "y")

    shifted = Image.new("RGBA", image.size)
    if effect == "slide":
        offset = round(min(image.size) * .08 * (1 - phase))
        shifted.paste(image, (0, offset))
    elif effect == "scale":
        left, top, right, bottom = bounds
        ratio = .60 + phase * .40
        width = max(1, round((right - left) * ratio))
        height = max(1, round((bottom - top) * ratio))
        with image.crop(bounds) as crop, crop.resize((width, height), Image.Resampling.LANCZOS) as scaled:
            shifted.paste(scaled, ((left + right - width) // 2, (top + bottom - height) // 2))
    return shifted


def render_text(layers, size, phase):
    canvas = Image.new("RGBA", size)
    try:
        for spec in layers:
            characters = math.ceil(len(spec["text"]) * phase) if spec["effect"] == "typewriter" else None
            with ExitStack() as cleanup:
                source = cleanup.enter_context(draw_layer(spec, size, characters=characters))
                frame = _motion(source, spec["effect"], phase)
                if frame is not source:
                    cleanup.enter_context(frame)
                canvas.alpha_composite(frame)
        return canvas
    except Exception:
        canvas.close()
        raise


def prepare_text(layers, size, target):
    target = Path(target)
    if not any(spec["effect"] in MOTIONS for spec in layers):
        with render_text(layers, size, 1) as image:
            image.save(target, "PNG")
        return target
    with ExitStack() as cleanup:
        frames = [cleanup.enter_context(render_text(layers, size, i / 9)) for i in range(10)]
        frames[0].save(target, save_all=True, append_images=frames[1:], format="PNG",
                       duration=80, loop=1, default_image=False, disposal=0, blend=0)
    with Image.open(target) as im:
        animated = getattr(im, "is_animated", False)
    if animated and target.suffix.lower() != ".apng":
        apng = target.with_suffix(".apng")
        target.rename(apng)
        target = apng
    return target
