"""Search a bounded set of reusable stock assets on Wikimedia Commons."""
import re
import html
import unicodedata
from urllib.parse import urlsplit

import requests

HEADERS = {"User-Agent": "CuePorch/0.3 (https://github.com/forkcommons/CuePorch)"}
MAX_BYTES = 64 * 1024 * 1024
CATEGORIES = {
    "Nature": "landscape",
    "Cities": "city",
    "People": "people",
    "Arts": "art",
    "Science": "science",
    "Events": "event",
}
MIME_BY_KIND = {
    "image": {("image/jpeg", "jpg"), ("image/jpeg", "jpeg"), ("image/png", "png"),
              ("image/gif", "gif"), ("image/webp", "webp")},
    "video": {("video/mp4", "mp4"), ("video/webm", "webm")},
}


def safe_https(value, host, path_prefix):
    """True iff value is an absolute https:// URL that:
    - uses the fixed hostname host (case-insensitive) with no userinfo / port
    - has no query string and no fragment
    - has a plain path (no backslashes, no NULs, no "//", no ".", ".."
      segments, no percent-encoded "." / "/" / "\\" / "%" sequences)
    - starts with the given path_prefix.
    """
    if not isinstance(value, str) or any(ch.isspace() for ch in value):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme.lower() != "https":
        return False
    if parts.netloc.lower() != host.lower():
        return False
    if parts.query or parts.fragment:
        return False

    path = parts.path
    if not path or not path.startswith("/"):
        return False
    if "\\" in path or "\x00" in path or "//" in path:
        return False

    lowered = path.lower()
    for marker in ("%2e", "%2f", "%5c", "%00", "%25"):
        if marker in lowered:
            return False

    for segment in path.split("/"):
        if segment in (".", ".."):
            return False

    if not path.startswith(path_prefix):
        return False
    return True


_LICENSE_PATHS = {
    "/licenses/by/4.0": "CC-BY-4.0",
    "/publicdomain/zero/1.0": "CC0-1.0",
}


def _license(value):
    """Return the supported license ID from canonical Creative Commons metadata."""
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    if parts.netloc.lower() != "creativecommons.org":
        return False
    if parts.query or parts.fragment:
        return False
    path = parts.path.rstrip("/")
    for suffix in ("/deed.en", "/legalcode"):
        if path.endswith(suffix):
            path = path.removesuffix(suffix)
    return _LICENSE_PATHS.get(path, "")


def _plain_text(html_text):
    """Strip tags first, then decode entities, then remove control
    characters, collapse whitespace, strip edges, and truncate to 160."""
    if not isinstance(html_text, str):
        return ""
    text = re.sub(r"<[^>]*>", "", html_text)
    text = html.unescape(text)
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("C") or ch.isspace())
    text = re.sub(r"\s+", " ", text).strip()
    return text[:160]


def _safe_name(title):
    """Sanitise a URL-decoded file title: drop any directory prefix,
    drop the extension suffix, remove unsafe filename characters,
    then trim to at most 110 characters."""
    if not isinstance(title, str) or not title.startswith("File:"):
        return ""
    name = title.removeprefix("File:").replace("\\", "_").replace("/", "_")
    if "." in name:
        name = name.rsplit(".", 1)[0]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "", name)
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C")).strip(" .")
    return name[:110]


def _entry(page, kind, category):
    try:
        info = page["imageinfo"][0]
        meta = info["extmetadata"]
        url = urlsplit(info["url"])._replace(query="", fragment="").geturl()
        source = info["descriptionurl"]
        if not safe_https(url, "upload.wikimedia.org", "/wikipedia/commons/"):
            return None
        if not safe_https(source, "commons.wikimedia.org", "/wiki/File:"):
            return None
        extension = url.rsplit(".", 1)[-1].lower()
        if (info["mime"], extension) not in MIME_BY_KIND[kind]:
            return None
        size, sha1, page_id = info["size"], info["sha1"], page["pageid"]
        if type(size) is not int or not 1 <= size <= MAX_BYTES or type(page_id) is not int or page_id <= 0:
            return None
        if not isinstance(sha1, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", sha1):
            return None
        title = _safe_name(page["title"])
        license_id = _license(meta["LicenseUrl"]["value"])
        credit = _plain_text(meta.get("Attribution", {}).get("value")) or _plain_text(
            meta.get("Artist", {}).get("value")
        )
        if not title or not license_id or (not credit and license_id != "CC0-1.0"):
            return None
        rights = dict(category=category, mood="", genre="", source_url=source,
                      credit=credit or "Wikimedia Commons", license=license_id)
        return dict(id=str(page_id), title=title, kind=kind, type="", extension=extension,
                    url=url, size=size, sha1=sha1.lower(), rights=rights)
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return None


def search(kind, category, query, offset):
    """GET Commons search.

    Each page is passed to _entry(page, kind, category), which consumes
    page["imageinfo"][0] containing url, mime, size, sha1, and extmetadata.

    Returns (entries, next_offset), where entries are non-None _entry results
    in page index order and next_offset is None or an increasing gsroffset <= 990.
    """
    filetype = "bitmap" if kind == "image" else "video"
    terms = f"{CATEGORIES[category]} {query}".strip()
    gsrsearch = f'filetype:{filetype} incategory:"CC-BY-4.0|CC-Zero" {terms}'

    params = {
        "action": "query",
        "format": "json",
        "formatversion": 2,
        "generator": "search",
        "gsrnamespace": 6,
        "gsrlimit": 30,
        "gsroffset": offset,
        "prop": "imageinfo",
        "iiprop": "url|mime|size|sha1|extmetadata",
        "iiextmetadatafilter": "Artist|LicenseUrl|Attribution",
        "gsrsearch": gsrsearch,
    }

    try:
        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params=params,
            headers=HEADERS,
            timeout=(5, 12),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise ValueError("Commons search is unavailable.") from exc

    if resp.status_code != 200:
        raise ValueError("Commons search returned invalid data.")

    try:
        data = resp.json()
    except ValueError as exc:
        raise ValueError("Commons search returned invalid data.") from exc

    if not isinstance(data, dict) or "error" in data:
        raise ValueError("Commons search returned invalid data.")

    result = data.get("query", {})
    pages = result.get("pages", []) if isinstance(result, dict) else None
    if not isinstance(pages, list):
        raise ValueError("Commons search returned invalid data.")

    ordered_pages = sorted((page for page in pages if isinstance(page, dict)),
                           key=lambda page: page["index"] if type(page.get("index")) is int else 1_000_000)

    entries = []
    for page in ordered_pages:
        entry = _entry(page, kind, category)
        if entry is not None:
            entries.append(entry)

    next_offset = None
    cont = data.get("continue")
    if isinstance(cont, dict):
        cand = cont.get("gsroffset")
        if isinstance(cand, int) and not isinstance(cand, bool):
            if cand > offset and cand <= 990:
                next_offset = cand

    return entries, next_offset
