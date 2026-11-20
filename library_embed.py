"""Player options restricted to recognized presentation controls."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def options(args, saved=None):
    values = {name: (saved or {}).get(name, False) for name in ("autoplay", "muted", "loop", "api", "dnt")}
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
    query = [(name, value) for name, value in parse_qsl(url.query, keep_blank_values=True) if name not in overrides]
    return urlunsplit(url._replace(query=urlencode(query + list(overrides.items()))))
