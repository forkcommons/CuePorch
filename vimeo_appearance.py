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
