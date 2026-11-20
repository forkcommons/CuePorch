"""Read Webex meeting recordings and fresh signed media links."""

from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import parse_qsl, urlsplit

from flask import abort

from workspace_oauth import check_provider, provider_request, response_object

API = "https://webexapis.com/v1"
SCOPE = "meeting:recordings_read"
POLL_SECONDS = 120
RECORDING = re.compile(r"[A-Za-z0-9_-]{1,200}")


def stamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def label(row):
    topic = row.get("topic")
    topic = topic.strip()[:100] if isinstance(topic, str) and topic.strip() else "Webex recording"
    made = stamp(row.get("createTime"))
    return f"{topic} · {made:%Y-%m-%d %H%M UTC}" if made else topic


def available(row):
    return (isinstance(row, dict) and row.get("format") == "MP4" and isinstance(row.get("id"), str)
            and RECORDING.fullmatch(row["id"]) and row.get("status", "available") == "available"
            and type(row.get("sizeBytes")) is int and row["sizeBytes"] > 0)


def cursor_query(cursor):
    try:
        fields = parse_qsl(cursor, keep_blank_values=True, max_num_fields=20)
    except ValueError:
        abort(400, "The Webex page token is invalid.")
    if not fields or any(key not in {"from", "to", "max", "format", "status", "after", "before", "offset", "cursor"}
                         for key, _ in fields):
        abort(400, "The Webex page token is invalid.")
    return fields


def list_recordings(connection, revision, start, end, cursor=None):
    url = API + "/recordings"
    fields = cursor_query(cursor) if cursor else {
        "from": start.isoformat(), "to": end.isoformat(), "max": 30, "format": "MP4", "status": "available",
    }
    with connection.api(revision, "GET", url, params=fields) as response:
        check_provider(response)
        rows = response_object(response).get("items")
        next_link = response.links.get("next", {}).get("url")
    following = None
    if next_link:
        target = urlsplit(next_link)
        allowed = urlsplit(url)
        if ((target.scheme, target.netloc, target.path) != (allowed.scheme, allowed.netloc, allowed.path)
                or target.fragment or not 1 <= len(target.query) <= 2048):
            abort(502, "Webex returned an unexpected continuation address.")
        following = target.query
    if not isinstance(rows, list):
        abort(502, "Webex returned an invalid recordings page.")
    return [row for row in rows if available(row)], following


def browse(connection, revision, cursor, search):
    now = datetime.now(timezone.utc)
    rows, following = list_recordings(connection, revision, now - timedelta(days=29), now, cursor)
    return dict(files=[
        dict(id=row["id"], name=label(row)) for row in rows if search in label(row).casefold()
    ], cursor=following)


def select(connection, revision, item):
    if not isinstance(item, str) or not RECORDING.fullmatch(item):
        abort(400, "Choose a Webex meeting recording.")
    with connection.api(revision, "GET", API + "/recordings/" + item) as response:
        check_provider(response)
        row = response_object(response)
    if row.get("id") != item or not available(row):
        abort(409, "This Webex recording is not ready or is no longer available.")
    links = row.get("temporaryDirectDownloadLinks")
    url = links.get("recordingDownloadLink") if isinstance(links, dict) else None
    if not isinstance(url, str) or not url:
        abort(403, "Webex did not grant a direct download for this recording.")
    return label(row) + ".mp4", row["sizeBytes"], media_url(url)


def media_url(url):
    try:
        target = urlsplit(url)
        if (target.scheme == "https" and target.port in (None, 443) and target.username is None
                and target.password is None and not target.fragment and target.path.startswith("/")
                and re.fullmatch(r"(?:[a-z0-9-]+\.)+webex\.com", target.hostname or "")):
            return url
    except ValueError:
        pass
    abort(502, "Webex returned an unexpected download address.")


def download(url):
    response = provider_request("GET", url, stream=True)
    if response.status_code not in (301, 302, 303, 307, 308):
        return response
    with response:
        target = media_url(response.headers.get("Location", ""))
    return provider_request("GET", target, stream=True)


def scan_new_recordings(connection, watch):
    state = json.loads(watch["cursor"]) if watch["cursor"] else {
        "end": int(datetime.now(timezone.utc).timestamp()),
        "cutoff": max(watch["started"], (watch["checked"] or watch["started"]) - 2 * 86400),
    }
    end = datetime.fromtimestamp(state["end"] + 1, timezone.utc)
    start = datetime.fromtimestamp(state["cutoff"], timezone.utc)
    rows, following = list_recordings(connection, watch["connection"], max(start, end - timedelta(days=29)),
                                      end, state.get("page"))
    entries = []
    for row in rows:
        made = stamp(row.get("createTime"))
        if made and made.timestamp() >= state["cutoff"] and row.get("shareToMe") is not True:
            entries.append(dict(id=row["id"], rev=row["id"], name=label(row)))
    state["page"] = following
    return dict(entries=entries, cursor=json.dumps(state) if following else "", has_more=bool(following))
