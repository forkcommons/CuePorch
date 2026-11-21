"""Rasterize the font layout and geometric treatments for a text layer."""

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from timeline_cards import _wrap_text

_ROOT = Path(__file__).resolve().parent
_FONT_FILES = {"sans": "source-sans-3-latin-700-normal.woff2",
               "serif": "dm-serif-text-latin-400-normal.woff2"}


@lru_cache(maxsize=64)
def _font(family, size):
    filename = _FONT_FILES[family]
    for folder in (_ROOT / "static" / "fonts", _ROOT / "docs" / "assets" / "fonts"):
        try:
            return ImageFont.truetype(str(folder / filename), size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _layout(draw, text, size, font_name, percent):
    width, height = size
    shortest = min(size)
    margin = max(4, round(shortest * .05))
    requested = max(4, round(shortest * percent / 100))
    for pixels in range(requested, 3, -1):
        font = _font(font_name, pixels)
        pad = max(2, round(pixels * .23))
        max_width = width - 2 * (margin + pad)
        lines = [line for paragraph in text.split("\n")
                 for line in _wrap_text(draw, paragraph, font, max_width) or [""]]
        line_height = max(1, round(pixels * 1.22))
        if len(lines) * line_height + 2 * (margin + pad) <= height:
            break
    return font, lines, line_height, pad, margin


def draw_treatment(draw, spec, box, pad):
    effect = spec["effect"]
    fill = "#" + spec["fill"] if spec["fill"] else None
    accent = "#" + (spec["highlight"] or spec["color"])
    width = max(1, pad // 3)
    if effect == "circle":
        draw.ellipse(box, fill=fill or "#182629", outline=accent, width=width)
    elif effect in ("capsule", "rounded"):
        radius = (box[3] - box[1]) // 2 if effect == "capsule" else pad
        draw.rounded_rectangle(box, radius=radius, fill=fill or "#182629", outline=accent, width=width)
    elif effect == "frame":
        draw.rectangle(box, fill=fill, outline=accent, width=width)
    else:
        if fill or effect == "cutout":
            draw.rectangle(box, fill=fill or "#182629")
        if effect in ("underline", "lines"):
            draw.line((box[0], box[3], box[2], box[3]), fill=accent, width=width)
        if effect == "lines":
            draw.line((box[0], box[1], box[2], box[1]), fill=accent, width=width)


def _paint_lines(draw, spec, font, lines, line_height, width, left, top, characters, erase):
    remaining = characters
    strong_shadow = spec["effect"] == "shadow"
    shadow_offset = max(1, font.size // (7 if strong_shadow else 16))
    for index, line in enumerate(lines):
        if remaining is not None and remaining <= 0:
            break
        shown = line if remaining is None else line[:remaining]
        if remaining is not None:
            remaining -= len(line) + 1
        if not shown:
            continue
        line_width = draw.textlength(line, font=font)
        x = left
        if spec["align"] == "center":
            x += round((width - line_width) / 2)
        elif spec["align"] == "right":
            x += round(width - line_width)
        y = top + index * line_height
        if spec["highlight"]:
            visible_width = draw.textlength(shown, font=font)
            draw.rectangle((x, y, x + visible_width, y + line_height - 1), fill="#" + spec["highlight"])
        if erase is not None:
            erase.text((x, y), shown, font=font, fill=255, anchor="lt")
            continue
        if spec["shadow"] or strong_shadow:
            draw.text((x + shadow_offset, y + shadow_offset), shown, font=font,
                      fill=(0, 0, 0, 230), anchor="lt")
        draw.text((x, y), shown, font=font, fill="#" + spec["color"], anchor="lt")


def draw_layer(spec, size, characters=None):
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    font, lines, line_height, pad, margin = _layout(draw, spec["text"], size, spec["font"], spec["size"])
    width = max(1, round(max(draw.textlength(line, font=font) for line in lines)))
    height = len(lines) * line_height
    x = {"left": margin + pad, "center": (size[0] - width) // 2,
         "right": size[0] - width - margin - pad}[spec["align"]]
    y = {"top": margin + pad, "center": (size[1] - height) // 2,
         "bottom": size[1] - height - margin - pad}[spec["position"]]
    box = (x - pad, y - pad, x + width + pad, y + height + pad)
    draw_treatment(draw, spec, box, pad)
    if spec["effect"] == "cutout":
        with Image.new("L", size) as mask:
            _paint_lines(draw, spec, font, lines, line_height, width, x, y, characters, ImageDraw.Draw(mask))
            with image.getchannel("A") as alpha, ImageChops.subtract(alpha, mask) as punched:
                image.putalpha(punched)
    else:
        _paint_lines(draw, spec, font, lines, line_height, width, x, y, characters, None)
    return image
