"""Timeline card normalization and PNG rendering."""
import unicodedata

from flask import abort
from PIL import Image, ImageDraw, ImageFont

_HEX_CHARS = set("0123456789abcdef")
_LAYOUTS = {"center", "left", "stripe"}


def _has_forbidden_character(value):
    return any(unicodedata.category(ch) in ("Cc", "Cs") for ch in value)


def _clean_text(value, minimum_length, maximum_length):
    if not isinstance(value, str):
        abort(422)
    if _has_forbidden_character(value):
        abort(422)
    cleaned = value.strip()
    if not (minimum_length <= len(cleaned) <= maximum_length):
        abort(422)
    return cleaned


def _normalize_color(value):
    if not isinstance(value, str):
        abort(422)
    if len(value) != 6 or any(ch not in _HEX_CHARS for ch in value):
        abort(422)
    return value


def normalize_card(item):
    if not isinstance(item, dict):
        abort(422)

    card = item.get("card")
    if not isinstance(card, dict):
        abort(422)

    start = item.get("start")
    end = item.get("end")

    if type(start) is not int or start != 0:
        abort(422)
    if type(end) is not int or not (1000 <= end <= 15000):
        abort(422)

    title = _clean_text(card.get("title"), 1, 80)
    subtitle = _clean_text(card.get("subtitle", ""), 0, 120)

    layout = card.get("layout")
    if not isinstance(layout, str):
        abort(422)
    if layout not in _LAYOUTS:
        abort(422)

    return {
        "card": {
            "title": title,
            "subtitle": subtitle,
            "background": _normalize_color(card.get("background")),
            "foreground": _normalize_color(card.get("foreground")),
            "accent": _normalize_color(card.get("accent")),
            "layout": layout,
        },
        "start": 0,
        "end": end,
    }


def _font(size):
    return ImageFont.load_default(size=size)


def _wrap_text(draw, text, font, max_width):
    if not text:
        return []

    lines = []
    current = ""

    for word in text.split():
        candidate = word if not current else current + " " + word
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = ""

        while draw.textlength(word, font=font) > max_width and len(word) > 1:
            cut = len(word) - 1
            while cut > 1 and draw.textlength(word[:cut], font=font) > max_width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]

        current = word

    if current:
        lines.append(current)

    return lines


def _fit_text(draw, text, max_width, max_height, start_size):
    if not text:
        return _font(8), [], 10

    for size in range(max(start_size, 8), 6, -1):
        font = _font(size)
        lines = _wrap_text(draw, text, font, max_width)
        line_height = size + 2
        if (
            len(lines) * line_height <= max_height
            and all(draw.textlength(line, font=font) <= max_width for line in lines)
        ):
            return font, lines, line_height

    font = _font(7)
    lines = _wrap_text(draw, text, font, max_width)
    return font, lines, 9


def _pillow_color(hex_color):
    return "#" + hex_color


def _relative_luminance(hex_color):
    red = int(hex_color[0:2], 16) / 255.0
    green = int(hex_color[2:4], 16) / 255.0
    blue = int(hex_color[4:6], 16) / 255.0

    def channel(value):
        if value <= 0.03928:
            return value / 12.92
        return ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _contrast_ratio(first_color, second_color):
    first_luminance = _relative_luminance(first_color)
    second_luminance = _relative_luminance(second_color)
    lighter = max(first_luminance, second_luminance)
    darker = min(first_luminance, second_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _choose_text_color(foreground, background):
    if _contrast_ratio(foreground, background) >= 4.5:
        return _pillow_color(foreground)

    black_ratio = _contrast_ratio("000000", background)
    white_ratio = _contrast_ratio("ffffff", background)
    return "#000000" if black_ratio >= white_ratio else "#ffffff"


def _draw_text_lines(draw, lines, font, line_height, x, y, width, align, fill):
    for line in lines:
        line_width = draw.textlength(line, font=font)
        draw_x = x + (width - line_width) // 2 if align == "center" else x
        draw.text((draw_x, y), line, fill=fill, font=font)
        y += line_height
    return y


def draw_card(path, card, size):
    width, height = size
    background = card["background"]
    foreground = card["foreground"]
    accent = card["accent"]
    layout = card["layout"]

    image = Image.new("RGB", (width, height), _pillow_color(background))
    draw = ImageDraw.Draw(image)

    margin = max(8, width // 24)
    bar_width = max(4, width // 60)
    bar_gap = max(6, width // 80)
    text_color = _choose_text_color(foreground, background)

    if layout == "stripe":
        panel_width = max(1, width // 4)
        draw.rectangle([0, 0, panel_width, height], fill=_pillow_color(accent))
        content_x = panel_width + margin
        content_width = width - content_x - margin
        align = "left"
        has_accent_bar = False
    elif layout == "left":
        content_x = margin + bar_width + bar_gap
        content_width = width - content_x - margin
        align = "left"
        has_accent_bar = True
    else:
        content_x = margin
        content_width = width - 2 * margin
        align = "center"
        has_accent_bar = False

    title_font, title_lines, title_line_height = _fit_text(
        draw,
        card["title"],
        content_width,
        height // 2,
        max(14, height // 8),
    )
    subtitle_font, subtitle_lines, subtitle_line_height = _fit_text(
        draw,
        card["subtitle"],
        content_width,
        height // 3,
        max(9, height // 14),
    )

    title_height = len(title_lines) * title_line_height
    subtitle_height = len(subtitle_lines) * subtitle_line_height
    gap = 4 if subtitle_lines else 0
    total_height = title_height + gap + subtitle_height
    y = max(margin, (height - total_height) // 2)

    if has_accent_bar:
        accent_height = min(total_height, max(title_line_height, bar_width * 3))
        draw.rectangle(
            [margin, y, margin + bar_width - 1, y + accent_height - 1],
            fill=_pillow_color(accent),
        )

    y = _draw_text_lines(
        draw,
        title_lines,
        title_font,
        title_line_height,
        content_x,
        y,
        content_width,
        align,
        text_color,
    )

    if subtitle_lines:
        y += gap
        _draw_text_lines(
            draw,
            subtitle_lines,
            subtitle_font,
            subtitle_line_height,
            content_x,
            y,
            content_width,
            align,
            text_color,
        )

    image.save(path, "PNG")
