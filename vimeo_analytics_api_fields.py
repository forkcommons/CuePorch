"""Normalize known Vimeo Analytics metrics without synthesizing playback."""

import math
import re

COUNT_KEYS = [
    "views", "impressions", "finishes", "downloads", "completions",
    "unique_viewers", "unique_impressions"
]
DEC_ALIASES = {
    "average_percent_watched": ("mean_percent_watched",),
    "average_seconds_watched": ("mean_seconds_watched",),
    "total_seconds_watched": (),
}
ORDER = ["video_title"] + COUNT_KEYS + list(DEC_ALIASES)
_MISS = object()

def _pick(d, name, aliases):
    for k in aliases + (name,):
        if k in d:
            v = d[k]
            if v is None:
                continue
            return v
    return _MISS

def _check_count(v):
    if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > 2**53 - 1:
        raise ValueError("invalid count metric")
    return v

def _check_num(v):
    if isinstance(v, bool):
        raise ValueError("invalid numeric metric")
    if isinstance(v, int):
        if v < 0:
            raise ValueError("invalid numeric metric")
        return v
    if isinstance(v, float):
        if not math.isfinite(v) or v < 0:
            raise ValueError("invalid numeric metric")
        return v
    raise ValueError("invalid numeric metric")

def _metrics(d):
    out = {}
    for k in COUNT_KEYS:
        v = _pick(d, k, ())
        if v is not _MISS:
            out[k] = _check_count(v)
    for k, aliases in DEC_ALIASES.items():
        v = _pick(d, k, aliases)
        if v is _MISS:
            fallback = {"average_seconds_watched": "average_time_watched", "total_seconds_watched": "total_time_watched"}.get(k)
            if fallback:
                v = _pick(d, fallback, ())
        if v is not _MISS:
            out[k] = _check_num(v)
    return out


def _video(row):
    details = row.get("metadata")
    connections = details.get("connections") if isinstance(details, dict) else None
    video = connections.get("video") if isinstance(connections, dict) else None
    return video if isinstance(video, dict) else {}


def normalize(video_rows, total_row):
    rows = []
    present = set()
    for row in video_rows:
        video = _video(row)
        uri = video.get("uri")
        source_id = ""
        if uri is not None:
            match = re.fullmatch(r"/videos/([0-9]{1,20})", uri) if isinstance(uri, str) else None
            if match is None:
                raise ValueError("Invalid video URI in the Vimeo analytics report.")
            source_id = str(int(match.group(1)))
        dimensions = {}
        title = video.get("title")
        if isinstance(title, str) and title and not title.startswith(("https://", "http://")):
            dimensions["video_title"] = title[:250].replace("\0", "")
        metrics = _metrics(row)
        present.update(dimensions)
        present.update(metrics)
        rows.append(dict(source_id=source_id, dimensions=dimensions, metrics=metrics))
    total = _metrics(total_row) if total_row is not None else None
    if total is not None:
        present.update(total)
    return dict(columns=[key for key in ORDER if key in present], rows=rows, total=total)
