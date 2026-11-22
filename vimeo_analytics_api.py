"""Fetch read-only snapshots from the documented Vimeo Analytics API."""

import hashlib
import json
from datetime import date
import re
from urllib.parse import urlencode

_ACCOUNT_RE = re.compile(r"/users/[0-9]{1,20}")
_MAX_BYTES = 8 * 1024 * 1024
_PAGE_SIZE = 500
_MAX_ROWS = 20000


def _fetch_json(source, path):
    with source.open(source.base + path, True) as response:
        raw = response.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("response exceeds 8 MiB limit")
    return json.loads(raw)


def _account(source):
    payload = _fetch_json(source, "/me")
    if not isinstance(payload, dict):
        raise ValueError("/me response must be a JSON object")
    uri = payload.get("uri")
    if not isinstance(uri, str) or _ACCOUNT_RE.fullmatch(uri) is None:
        raise ValueError("invalid account uri")
    return uri


def _rows(source, path, size):
    rows = _fetch_json(source, path)
    if not isinstance(rows, list):
        raise ValueError("analytics response must be a bare array")
    if len(rows) > size:
        raise ValueError("analytics row count exceeds limit")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("analytics row must be a JSON object")
    return rows


def _analytics_url(account, dimension, first, last, page):
    params = {
        "from": first + "T00:00:00Z",
        "to": last + "T23:59:59Z",
        "dimension": dimension,
        "page": page,
        "per_page": _PAGE_SIZE,
    }
    if dimension == "video":
        params["time_interval"] = "none"
    return account + "/analytics?" + urlencode(params)


def _parse_date(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be YYYY-MM-DD")
    try:
        d = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be YYYY-MM-DD")
    if d.isoformat() != value:
        raise ValueError(f"{name} must be YYYY-MM-DD")
    return value


def _hash_rows(rows):
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _video_uri(row):
    md = row.get("metadata")
    if md is None:
        return None
    if not isinstance(md, dict):
        raise ValueError("metadata must be object")
    conns = md.get("connections")
    if conns is None:
        return None
    if not isinstance(conns, dict):
        raise ValueError("metadata.connections must be object")
    vid = conns.get("video")
    if vid is None:
        return None
    if not isinstance(vid, dict):
        raise ValueError("metadata.connections.video must be object")
    uri = vid.get("uri")
    if uri is None:
        return None
    if not isinstance(uri, str) or not uri:
        raise ValueError("metadata.connections.video.uri invalid")
    return uri

def fetch_video_reports(source, first, last):
    first = _parse_date(first, "first")
    last = _parse_date(last, "last")
    if first > last:
        raise ValueError("Last date must follow first date.")
    account = _account(source)
    results = []
    seen_pages = set()
    seen_uris = set()
    for page in range(1, _MAX_ROWS // _PAGE_SIZE + 2):
        batch = _rows(source, _analytics_url(account, "video", first, last, page), _PAGE_SIZE)
        if len(results) + len(batch) > _MAX_ROWS:
            raise ValueError("Vimeo analytics exceeds the import limit.")
        digest = _hash_rows(batch)
        if digest in seen_pages:
            raise ValueError("Vimeo repeated an analytics page.")
        seen_pages.add(digest)
        for row in batch:
            uri = _video_uri(row)
            if uri is None:
                continue
            if uri in seen_uris:
                raise ValueError("Vimeo repeated a video in an unsegmented report.")
            seen_uris.add(uri)
        results.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
    total = _rows(source, _analytics_url(account, "total", first, last, 1), 1)
    return account, results, total[0] if total else None
