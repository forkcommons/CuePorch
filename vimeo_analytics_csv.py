"""Read aggregate metrics from a Vimeo Video report CSV."""

import csv
import io
import re
from datetime import date
from urllib.parse import urlsplit


_DIMENSIONS = {
    "date": "date", "device": "device", "device type": "device", "region": "region",
    "source": "source", "source url": "source", "streaming type": "streaming",
}
_COUNTS = {
    "views": "views", "impressions": "impressions", "finishes": "finishes",
    "downloads": "downloads", "likes": "likes", "comments": "comments",
    "unique viewers": "unique_viewers", "unique impressions": "unique_impressions",
}
_DECIMALS = {
    "average percent watched": "average_percent_watched",
    "average seconds watched": "average_seconds_watched",
    "total time watched": "total_seconds_watched",
    "total seconds watched": "total_seconds_watched",
}
_IDENTITIES = {
    "video id": "video_id", "id": "video_id", "video url": "video_url",
    "video link": "video_url", "video uri": "video_uri",
    "video": "video_title", "video title": "video_title", "title": "video_title",
}
_HEADERS = _DIMENSIONS | _COUNTS | _DECIMALS | _IDENTITIES
_TOTAL_LABELS = {"report total", "total"}
_COUNT_KEYS = set(_COUNTS.values())
_DURATION_KEYS = {"average_seconds_watched", "total_seconds_watched"}
_NUMBER = r"(?:[0-9]{1,15}|[0-9]{1,3}(?:,[0-9]{3}){1,4})"
_SOURCE_ID = r"([0-9]{1,20})"


def valid_utc_date(value):
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _source_id(value):
    direct = re.fullmatch(r"(?:/videos/)?" + _SOURCE_ID, value)
    if direct:
        return str(int(direct.group(1)))
    try:
        url = urlsplit(value)
    except ValueError:
        return None
    if url.scheme != "https" or url.username is not None or url.password is not None:
        return None
    prefix = {
        "vimeo.com": "/", "www.vimeo.com": "/",
        "player.vimeo.com": "/video/", "api.vimeo.com": "/videos/",
    }.get(url.netloc.lower())
    if prefix:
        path = re.fullmatch(re.escape(prefix) + _SOURCE_ID + r"(?:/[A-Za-z0-9]+)?/?", url.path)
        if path:
            return str(int(path.group(1)))
    return None


def _source_domain(value):
    try:
        url = urlsplit(value if "://" in value or value.startswith("//") else "//" + value)
        host = url.hostname or ""
        if url.scheme in ("", "http", "https") and re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", host):
            return host.rstrip(".").lower()
    except ValueError:
        pass
    return "Unknown"


def _metric(key, value):
    if key in _COUNT_KEYS:
        if re.fullmatch(_NUMBER, value):
            return int(value.replace(",", ""))
    else:
        if key == "average_percent_watched":
            value = value.removesuffix("%").rstrip()
        if re.fullmatch(_NUMBER + r"(?:\.[0-9]{1,9})?", value):
            return float(value.replace(",", ""))
        if key in _DURATION_KEYS:
            duration = re.fullmatch(r"([0-9]{1,8}):([0-5][0-9]):([0-5][0-9](?:\.[0-9]{1,9})?)", value)
            if duration:
                hours, minutes, seconds = duration.groups()
                return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise ValueError("A value in a metric column is invalid.")


def parse(data, first, last):
    if not valid_utc_date(first) or not valid_utc_date(last) or first > last:
        raise ValueError("Enter the UTC date range used for the Vimeo export.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("Choose a UTF-8 Vimeo analytics CSV.") from None
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next((row for row in reader if any(cell.strip() for cell in row)), [])
        recognized = {}
        for index, label in enumerate(header):
            key = _HEADERS.get(re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip())
            if key:
                if key in recognized:
                    raise ValueError("The report repeats a recognized column.")
                recognized[key] = index
        identity_keys = set(_IDENTITIES.values())
        metric_keys = _COUNT_KEYS | set(_DECIMALS.values())
        if not identity_keys.intersection(recognized) or not metric_keys.intersection(recognized):
            raise ValueError("The report needs a video column and at least one supported Vimeo metric.")
        columns = [key for key in recognized if key not in identity_keys or key == "video_title"]
        rows = []
        total = None
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            if len(rows) >= 20000 or len(row) != len(header):
                raise ValueError(f"Row {reader.line_num} exceeds the row limit or has the wrong number of columns.")
            values = {key: row[index].strip() for key, index in recognized.items()}
            try:
                metrics = {key: _metric(key, value) for key, value in values.items()
                           if key in metric_keys and value.casefold() not in ("", "—", "-", "n/a")}
                ids = {key: values[key] for key in identity_keys if values.get(key)}
                if any(value.casefold() in _TOTAL_LABELS for value in ids.values()) and not any(
                    value for key, value in ids.items()
                    if key != "video_title" and value.casefold() not in _TOTAL_LABELS
                ):
                    if total is not None:
                        raise ValueError("The report repeats its total row.")
                    total = metrics
                    continue
                exact = {_source_id(value) for key, value in ids.items() if key != "video_title"}
                if None in exact or len(exact) > 1:
                    raise ValueError("The Vimeo video identifiers are invalid or disagree.")
                if not exact and ids.get("video_title", "").startswith("https://"):
                    source_id = _source_id(ids["video_title"])
                    if source_id:
                        exact.add(source_id)
                dimensions = {key: values[key] for key in _DIMENSIONS.values() if values.get(key)}
                if dimensions.get("date") and (
                    not valid_utc_date(dimensions["date"]) or not first <= dimensions["date"] <= last
                ):
                    raise ValueError("A report date is outside the selected UTC window or invalid.")
                if "source" in dimensions:
                    dimensions["source"] = _source_domain(dimensions["source"])
                title = values.get("video_title", "")
                if title and not title.startswith(("http://", "https://")):
                    dimensions["video_title"] = title
                if any(len(value) > 1000 or "\0" in value for value in dimensions.values()):
                    raise ValueError("A report field is too long or contains an invalid character.")
                rows.append(dict(source_id=next(iter(exact), ""), dimensions=dimensions, metrics=metrics))
            except ValueError as error:
                raise ValueError(f"Row {reader.line_num}. {error}") from None
    except csv.Error:
        raise ValueError(f"Row {reader.line_num} is malformed.") from None
    if not rows and total is None:
        raise ValueError("This report has no analytics rows.")
    return dict(columns=columns, rows=rows, total=total)
