"""Player options restricted to recognized presentation controls."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def options(args):
    return {name: args.get(name) in {"1", "true"} for name in ("autoplay", "muted", "loop", "api")}


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
    query = [(name, value) for name, value in parse_qsl(url.query, keep_blank_values=True) if name not in overrides]
    return urlunsplit(url._replace(query=urlencode(query + list(overrides.items()))))
