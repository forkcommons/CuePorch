"""Player options restricted to recognized presentation controls."""

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def options(args, saved=None):
    values = {
        name: (saved or {}).get(name, False)
        for name in ("autoplay", "muted", "loop", "api", "dnt")
    }
    for name in values:
        if args.get(name) in {"1", "true", "0", "false"}:
            values[name] = args[name] in {"1", "true"}
    return values


def peertube_address(destination, args):
    url = urlsplit(destination)
    if not url.path.startswith("/videos/embed/"):
        return destination
    overrides = {
        name: "1" if args.get(name) in {"1", "true"} else "0"
        for name in ("autoplay", "muted", "loop")
        if args.get(name) in {"1", "true", "0", "false"}
    }
    if not overrides:
        return destination
    query = [
        (name, value)
        for name, value in parse_qsl(url.query, keep_blank_values=True)
        if name not in overrides
    ]
    return urlunsplit(url._replace(query=urlencode(query + list(overrides.items()))))


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_START = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,3})?")


def oembed_url(origin, uid):
    if origin == "Unknown":
        return None
    return (
        origin
        + "/api/public/oembed?"
        + urlencode({"url": f"{origin}/watch/work/{uid}", "format": "json"})
    )


def parse_oembed_url(origin, value):
    if not isinstance(value, str) or not value.isascii() or len(value) > 512:
        return None
    if (
        any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value)
        or "\\" in value
        or "#" in value
    ):
        return None
    try:
        o, p = urlsplit(origin), urlsplit(value)
    except ValueError:
        return None
    if p.scheme != o.scheme or p.netloc != o.netloc:
        return None
    pre = "/watch/work/"
    if not p.path.startswith(pre) or not _UUID.fullmatch(p.path[len(pre) :]):
        return None
    uid = p.path[len(pre) :]
    q = parse_qsl(p.query, keep_blank_values=True)
    if not q:
        return uid, None
    if len(q) == 1 and q[0][0] == "start" and _START.fullmatch(q[0][1]):
        return uid, q[0][1]
    return None


def oembed_video(origin, uid, title, start=None, maxwidth=None, maxheight=None):
    scale = 1.0
    if maxwidth and maxwidth > 0:
        scale = min(scale, maxwidth / 640)
    if maxheight and maxheight > 0:
        scale = min(scale, maxheight / 360)
    width = max(1, int(640 * scale))
    height = max(1, int(360 * scale))
    params = {}
    if start is not None:
        params["start"] = start
    query = ("?" + urlencode(params)) if params else ""
    src = html.escape(f"{origin}/embed/work/{uid}{query}", quote=True)
    title_esc = html.escape(title, quote=True)
    iframe = (
        f'<iframe src="{src}" width="{width}" height="{height}" '
        f'title="{title_esc}" referrerpolicy="origin" allowfullscreen '
        f'allow="picture-in-picture"></iframe>'
    )
    return {
        "type": "video",
        "version": "1.0",
        "provider_name": "CuePorch",
        "provider_url": origin,
        "cache_age": 0,
        "title": title,
        "width": width,
        "height": height,
        "html": iframe,
    }
