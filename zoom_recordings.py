"""Read Zoom cloud recordings by unique meeting occurrence."""

from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import quote, urlsplit

from flask import abort

from workspace_oauth import check_provider, provider_request, response_object

ORIGIN = "https://zoom.us"
API = "https://api.zoom.us/v2/"
SCOPES = "cloud_recording:read:list_user_recordings cloud_recording:read:list_recording_files"
POLL_SECONDS = 120
MEETING_UUID = re.compile(r"[A-Za-z0-9_+/=-]{1,160}")
FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,160}")
PREFERRED_VIEW = {
    "shared_screen_with_speaker_view": 0, "shared_screen_with_speaker_view(CC)": 0,
    "shared_screen_with_gallery_view": 1, "active_speaker": 2, "gallery_view": 3, "shared_screen": 4,
}


def get(connection, revision, path, params=None):
    with connection.api(revision, "GET", API + path, params=params) as response:
        check_provider(response)
        return response_object(response)


def preferred(row, exact=None):
    files = row.get("recording_files")
    if not isinstance(files, list):
        return None
    allowed = [
        file for file in files if isinstance(file, dict) and file.get("file_type") == "MP4"
        and file.get("status") == "completed" and isinstance(file.get("id"), str) and FILE_ID.fullmatch(file["id"])
        and isinstance(file.get("recording_type"), str) and isinstance(file.get("download_url"), str)
        and type(file.get("file_size")) is int and file["file_size"] > 0
        and (exact is None or file["id"] == exact)
    ]
    return min(allowed, key=lambda file: (PREFERRED_VIEW.get(file["recording_type"], 5), file["id"]), default=None)


def title(row):
    topic = row.get("topic")
    topic = topic.strip()[:100] if isinstance(topic, str) and topic.strip() else "Zoom recording"
    start = row.get("start_time")
    try:
        stamp = datetime.fromisoformat(start).astimezone(timezone.utc).strftime("%Y-%m-%d %H%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        stamp = ""
    return f"{topic} · {stamp}" if stamp else topic


def valid_meeting(row):
    return (isinstance(row, dict) and isinstance(row.get("uuid"), str) and MEETING_UUID.fullmatch(row["uuid"]))


def list_meetings(connection, revision, start, end, cursor):
    params = {"from": start.isoformat(), "to": end.isoformat(), "page_size": 30}
    if cursor:
        params["next_page_token"] = cursor
    result = get(connection, revision, "users/me/recordings", params)
    meetings, following = result.get("meetings", []), result.get("next_page_token")
    if not isinstance(meetings, list) or (following is not None and
                                         (not isinstance(following, str) or len(following) > 2048)):
        abort(502, "Zoom returned an invalid recordings page.")
    return [row for row in meetings if valid_meeting(row)], following or None


def browse(connection, revision, cursor, search):
    now = datetime.now(timezone.utc).date()
    rows, following = list_meetings(connection, revision, now - timedelta(days=29), now, cursor)
    return dict(files=[
        dict(id=row["uuid"], name=title(row)) for row in rows if preferred(row) and search in title(row).casefold()
    ], cursor=following)


def select(connection, revision, occurrence, exact=None):
    if not isinstance(occurrence, str) or not MEETING_UUID.fullmatch(occurrence):
        abort(400, "Choose a Zoom recording by its meeting occurrence.")
    resource = quote(occurrence, safe="")
    if occurrence.startswith("/") or "//" in occurrence:
        resource = quote(resource, safe="")
    row = get(connection, revision, f"meetings/{resource}/recordings")
    if row.get("uuid") != occurrence:
        abort(422, "Zoom returned another meeting occurrence.")
    recording = preferred(row, exact)
    if not recording:
        abort(409, "This Zoom recording is not ready or is no longer available.")
    return title(row) + ".mp4", recording


def media_url(url, signed=False):
    try:
        parsed = urlsplit(url)
        service = r"(?:[a-z0-9-]+\.)*zoom\.us"
        cdn = r"(?:[a-z0-9-]+\.)*zoomcdn\.net"
        name = parsed.hostname or ""
        secure = (parsed.scheme == "https" and parsed.port in (None, 443) and not parsed.username
                  and parsed.password is None and not parsed.fragment)
        vendor = (re.fullmatch(service, name) and (signed or parsed.path.startswith("/rec/"))
                  or signed and re.fullmatch(cdn, name))
        if secure and vendor:
            return url
    except ValueError:
        pass
    abort(502, "Zoom returned an unexpected download address.")


def download(connection, revision, url):
    response = connection.api(revision, "GET", media_url(url), stream=True)
    if response.status_code not in (301, 302, 303, 307, 308):
        return response
    with response:
        target = media_url(response.headers.get("Location", ""), signed=True)
    return provider_request("GET", target, stream=True)


def scan_new_recordings(connection, watch):
    state = json.loads(watch["cursor"]) if watch["cursor"] else {
        "end": int(datetime.now(timezone.utc).timestamp()),
        "cutoff": max(watch["started"], (watch["checked"] or watch["started"]) - 2 * 86400),
    }
    end = datetime.fromtimestamp(state["end"], timezone.utc).date()
    start = datetime.fromtimestamp(state["cutoff"], timezone.utc).date()
    rows, following = list_meetings(connection, watch["connection"], max(start, end - timedelta(days=29)),
                                    end, state.get("page"))
    entries = []
    for row in rows:
        if not (file := preferred(row)):
            continue
        try:
            ended = datetime.fromisoformat(file["recording_end"])
            if ended.tzinfo is None or ended.timestamp() < state["cutoff"]:
                continue
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        entries.append(dict(id=row["uuid"], rev=file["id"], name=title(row)))
    state["page"] = following
    return dict(entries=entries, cursor=json.dumps(state) if following else "", has_more=bool(following))
