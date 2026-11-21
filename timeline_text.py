"""Validate stored and previewed timeline text layers."""

import re
import unicodedata

from flask import abort

STYLE_SIZES = {"title": 10, "headline": 8, "subheadline": 6, "caption": 4}
EFFECTS = ("cut", "fade", "slide", "reveal", "typewriter", "flicker", "scale",
           "underline", "lines", "frame", "cutout", "wipe", "circle", "capsule", "shadow", "rounded")
MOTIONS = frozenset(("fade", "slide", "reveal", "typewriter", "flicker", "scale", "wipe"))
CHOICES = {
    "style": STYLE_SIZES,
    "font": ("sans", "serif"),
    "align": ("left", "center", "right"),
    "position": ("top", "center", "bottom"),
    "effect": EFFECTS,
}
DEFAULT_TEXT = {"style": "title", "font": "sans", "align": "center", "position": "center", "effect": "cut"}


def normalize_text(raw):
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 4:
        abort(422, "Choose up to four text layers per clip.")
    result = []
    for layer in raw:
        if not isinstance(layer, dict):
            abort(422, "Choose a valid text layer.")
        text = layer.get("text")
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 150
                or any(c != "\n" and unicodedata.category(c) in ("Cc", "Cs") for c in text)):
            abort(422, "Enter up to 150 characters for the text layer.")
        values = {"text": text.strip()}
        for name, choices in CHOICES.items():
            default = "bottom" if name == "position" and values["style"] == "caption" else DEFAULT_TEXT[name]
            value = layer.get(name, default)
            if not isinstance(value, str) or value not in choices:
                abort(422, "Choose a valid text style, font, placement or effect.")
            values[name] = value

        size = layer.get("size", STYLE_SIZES[values["style"]])
        shadow = layer.get("shadow", False)
        if type(size) is not int or not 2 <= size <= 18 or type(shadow) is not bool:
            abort(422, "Choose a text size between 2% and 18% and a valid shadow setting.")
        values.update(size=size, shadow=shadow)

        for name, default in (("color", "ffffff"), ("fill", ""), ("highlight", "")):
            color = layer.get(name, default)
            if not isinstance(color, str) or (color or default) and not re.fullmatch(r"[a-f0-9]{6}", color, re.I):
                abort(422, "Choose valid text colors.")
            values[name] = color.lower()
        result.append(values)
    return result
