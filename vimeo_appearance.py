"""Map documented, portable Vimeo embed settings to CuePorch appearance."""

import re
_HEX = re.compile(r'#?([0-9a-fA-F]{6})\Z', re.ASCII)
_POS = re.compile(r'(auto|bottom|center)\Z')
_SHOW = re.compile(r'(show|hide)\Z')

def _get(d, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d

def _color(v):
    if isinstance(v, str):
        m = _HEX.fullmatch(v)
        if m:
            return '#' + m.group(1).lower()
    return None

def embed_appearance(value):
    if not isinstance(value, dict):
        return {}
    out = {}
    colors = value.get('colors')
    accent = None
    if isinstance(colors, dict):
        c = _color(colors.get('color_one'))
        if c is not None:
            out['primary'] = c
        c = _color(colors.get('color_three'))
        if c is not None:
            out['icon'] = c
            out['text'] = c
        c = _color(colors.get('color_four'))
        if c is not None:
            out['background'] = c
        accent = _color(colors.get('color_two'))
    if accent is None:
        accent = _color(value.get('color'))
    if accent is not None:
        out['accent'] = accent

    bool_map = {
        ('playbar',): 'progress',
        ('volume',): 'volume',
        ('closed_captions',): 'captions',
        ('audio_tracks',): 'audio_tracks',
        ('chapters',): 'chapters',
        ('speed',): 'speed',
        ('pip',): 'pip',
        ('context_menu',): 'right_click',
        ('autoplay',): 'autoplay',
        ('muted',): 'muted',
        ('buttons', 'fullscreen'): 'fullscreen',
    }
    for path, key in bool_map.items():
        v = _get(value, *path)
        if type(v) is bool:
            out[key] = v

    name = _get(value, 'title', 'name')
    if isinstance(name, str):
        m = _SHOW.fullmatch(name)
        if m:
            out['show_title'] = m.group(1) == 'show'

    pos = _get(value, 'play_button', 'position')
    if isinstance(pos, str):
        m = _POS.fullmatch(pos)
        if m:
            out['play_position'] = m.group(1)

    return out


def watch_appearance(value):
    result = embed_appearance(_get(value, "player"))
    result.pop("background", None)
    result.pop("text", None)
    pairs = (
        (("title",), "show_title"),
        (("description",), "description"),
        (("chapters",), "chapters"),
        (("credits",), "credits"),
        (("tags",), "tags"),
        (("categories",), "categories"),
        (("creative_commons",), "license"),
        (("player", "fullscreen"), "fullscreen"),
        (("customization", "ambient_backlight"), "ambient"),
    )
    for path, dest in pairs:
        v = _get(value, *path)
        if type(v) is bool:
            result[dest] = v
    h = _get(value, "customization", "hide_vimeo_header")
    if type(h) is bool:
        result["show_chrome"] = not h
    bg = _get(value, "customization", "background_color")
    if isinstance(bg, str) and bg.startswith("#"):
        c = _color(bg)
        if c:
            result["background"] = c
    return result


def embed_ending(value):
    mode = _get(value, "end_screen", "type")
    if isinstance(mode, str):
        return mode if mode in ("empty", "loop", "share", "thumbnail") else "default"
    legacy = _get(value, "outro_type")
    if isinstance(legacy, str):
        return legacy if legacy in ("empty", "loop", "share", "thumbnail") else "default"
    return None


def custom_logo(value):
    """Parse Vimeo ``logos.custom.active`` and ``logos.custom.url`` only.

    - Explicit ``active is False`` yields ``False`` (explicit disable).
    - ``active is True`` with a non-empty string ``url`` yields that string.
    - Anything else yields ``None``.

    No network access is performed here; only the documented fields are
    inspected so unrelated logo state cannot leak into the record.
    """
    active = _get(value, "logos", "custom", "active")
    if active is False:
        return False
    if active is True:
        url = _get(value, "logos", "custom", "url")
        if isinstance(url, str) and url.strip():
            return url
        return None
    return None
