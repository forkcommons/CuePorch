"""Bounded YouTube Data API adapter for a direct YouTube live destination."""

import dataclasses
import re
from urllib.parse import urlsplit
from flask import abort
from werkzeug.exceptions import HTTPException, TooManyRequests
import team_accounts
from workspace_oauth import Connection, response_object
import youtube_upload

OAUTH = dataclasses.replace(youtube_upload.OAUTH, key="youtube-live", name="YouTube Live",
                            scope="https://www.googleapis.com/auth/youtube.force-ssl")
API = "https://www.googleapis.com/youtube/v3"
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")
CURSOR = re.compile(r"[A-Za-z0-9_=+/.-]{1,1024}")
BROADCAST_PARTS = "id,snippet,status,contentDetails"
STREAM_KEY = re.compile(r"[A-Za-z0-9_-]{8,128}")
RTMPS_HOSTS = {"a.rtmps.youtube.com", "b.rtmps.youtube.com"}
MAX_PAGES = 20
PRIVACY = {"private", "unlisted", "public"}
SCHEDULED_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})")
MAX_ADDRESS = 512


class WorkspaceConnection(Connection):
    def __init__(self, tx, operator, access):
        super().__init__(OAUTH, tx, operator)
        self.access = access

    def save_authorization(self, con, tokens, context):
        team_accounts.require_live_connection_manager(con, context, self.access)
        return super().save_authorization(con, tokens, context)


def _fail(response, action):
    if response.status_code == 429:
        raise TooManyRequests(f"YouTube is rate-limiting {action}.", retry_after=youtube_upload.delay(response))
    if response.status_code in (401, 403):
        abort(403, f"YouTube refused this {action}. Check the connected account and its live permissions.")
    if 400 <= response.status_code < 500:
        abort(422, f"YouTube rejected this {action}.")
    abort(502, "The YouTube live service is unavailable.")


def _get(connection, revision, resource, params, action):
    with connection.api(revision, "GET", f"{API}/{resource}", params=params) as response:
        if response.status_code != 200:
            _fail(response, action)
        return response_object(response)


def channels(connection, revision, cursor=None):
    params = {"mine": "true", "part": "id,snippet", "maxResults": "50"}
    if cursor is not None:
        if not isinstance(cursor, str) or not CURSOR.fullmatch(cursor):
            abort(400, "Invalid channel list cursor.")
        params["pageToken"] = cursor
    result = _get(connection, revision, "channels", params, "channel listing")
    items = result.get("items") if isinstance(result, dict) else None
    nxt = result.get("nextPageToken") if isinstance(result, dict) else None
    if (not isinstance(items, list) or len(items) > 50
            or (nxt is not None and (not isinstance(nxt, str) or not CURSOR.fullmatch(nxt)))):
        abort(502, "YouTube returned a malformed channel list or cursor.")
    rows = []
    for item in items:
        snippet = item.get("snippet") if isinstance(item, dict) else None
        snippet = snippet if isinstance(snippet, dict) else None
        ident = item.get("id") if isinstance(item, dict) else None
        title = snippet.get("title") if snippet else None
        if (not isinstance(ident, str) or not IDENTIFIER.fullmatch(ident)
                or not isinstance(title, str) or not title or len(title) > 256):
            abort(502, "YouTube returned a malformed channel list.")
        rows.append({"id": ident, "title": title})
    return {"items": rows, "next": nxt}


def broadcasts(connection, revision, cursor=None):
    params = {"mine": "true", "broadcastType": "event", "part": BROADCAST_PARTS, "maxResults": "50"}
    if cursor is not None:
        if not isinstance(cursor, str) or not CURSOR.fullmatch(cursor):
            abort(400, "Invalid broadcast list cursor.")
        params["pageToken"] = cursor
    result = _get(connection, revision, "liveBroadcasts", params, "broadcast listing")
    items = result.get("items") if isinstance(result, dict) else None
    nxt = result.get("nextPageToken") if isinstance(result, dict) else None
    if (not isinstance(items, list) or len(items) > 50
            or (nxt is not None and (not isinstance(nxt, str) or not CURSOR.fullmatch(nxt)))):
        abort(502, "YouTube returned a malformed broadcast list or cursor.")
    rows = []
    for item in items:
        if not isinstance(item, dict):
            abort(502, "YouTube returned a malformed broadcast list.")
        ident, snippet = item.get("id"), item.get("snippet")
        status, details = item.get("status"), item.get("contentDetails")
        if not isinstance(ident, str) or not IDENTIFIER.fullmatch(ident):
            abort(502, "YouTube returned a malformed broadcast list.")
        if not isinstance(snippet, dict) or not isinstance(status, dict) or not isinstance(details, dict):
            continue
        if (status.get("lifeCycleStatus") != "ready"
                or not isinstance(details.get("boundStreamId"), str) or not details["boundStreamId"]
                or details.get("enableAutoStart") is not True or details.get("enableAutoStop") is not True):
            continue
        title, channel = snippet.get("title"), snippet.get("channelId")
        starts, privacy = snippet.get("scheduledStartTime"), status.get("privacyStatus")
        if (not isinstance(title, str) or not title or title != title.strip() or len(title) > 256
                or not isinstance(channel, str) or not IDENTIFIER.fullmatch(channel)
                or not isinstance(starts, str) or len(starts) > 64 or not SCHEDULED_TIME.fullmatch(starts)
                or not isinstance(privacy, str) or privacy not in PRIVACY):
            abort(502, "YouTube returned a malformed broadcast list.")
        rows.append({"id": ident, "title": title, "channel": channel, "starts": starts, "privacy": privacy})
    return {"items": rows, "next": nxt}


class YouTubeWriteUnconfirmed(Exception):
    """A remote write may have succeeded, so look it up before proceeding."""


def _create(connection, revision, resource, params, data, channel, action, before_send):
    try:
        with connection.api(revision, "POST", f"{API}/{resource}", params=params,
                            json=data, before_send=before_send) as response:
            if response.status_code >= 500 or response.status_code == 408:
                raise YouTubeWriteUnconfirmed(action)
            if response.status_code not in (200, 201):
                _fail(response, action)
            item = response_object(response)
    except HTTPException as error:
        if error.code != 502:
            raise
        raise YouTubeWriteUnconfirmed(action) from error
    snippet = item.get("snippet")
    item_id = item.get("id")
    if (not isinstance(item_id, str) or not IDENTIFIER.fullmatch(item_id)
            or not isinstance(snippet, dict) or snippet.get("channelId") != channel):
        raise YouTubeWriteUnconfirmed(action)
    return item


def insert_broadcast(connection, revision, channel, title, starts, ends, privacy, marker, before_send=None):
    data = {
        "snippet": {"title": title, "description": marker,
                    "scheduledStartTime": starts},
        "status": {"privacyStatus": privacy},
        "contentDetails": {"enableAutoStart": True, "enableAutoStop": True},
    }
    if ends is not None:
        data["snippet"]["scheduledEndTime"] = ends
    return _create(connection, revision, "liveBroadcasts", {"part": BROADCAST_PARTS},
                   data, channel, "broadcast creation", before_send)


def insert_stream(connection, revision, channel, title, marker, before_send=None):
    data = {
        "snippet": {"title": title, "description": marker},
        "cdn": {"frameRate": "variable", "resolution": "variable", "ingestionType": "rtmp"},
        "contentDetails": {"isReusable": False},
    }
    return _create(connection, revision, "liveStreams", {"part": "id,snippet,cdn,contentDetails"},
                   data, channel, "stream creation", before_send)


def find_created(connection, revision, resource, channel, marker):
    params = {"mine": "true", "maxResults": 50, "part": "id,snippet"}
    if resource == "liveBroadcasts":
        params["broadcastType"] = "event"
    found = None
    cursors = set()
    for _ in range(MAX_PAGES):
        page = _get(connection, revision, resource, params, "created resource lookup")
        items = page.get("items")
        if not isinstance(items, list) or len(items) > 50:
            abort(502, "YouTube returned an invalid list of resources.")
        for item in items:
            snippet = item.get("snippet") if isinstance(item, dict) else None
            if not isinstance(snippet, dict):
                abort(502, "YouTube returned an invalid resource.")
            if snippet.get("description") == marker and snippet.get("channelId") == channel:
                candidate = item.get("id")
                if (not isinstance(candidate, str) or not IDENTIFIER.fullmatch(candidate)
                        or (found is not None and found != candidate)):
                    abort(502, "YouTube returned an ambiguous resource match.")
                found = candidate
        cursor = page.get("nextPageToken")
        if cursor is None:
            return found
        if not isinstance(cursor, str) or not CURSOR.fullmatch(cursor) or cursor in cursors:
            abort(502, "YouTube returned an invalid or repeating resource cursor.")
        cursors.add(cursor)
        params["pageToken"] = cursor
    abort(502, "YouTube resource lookup reached its page limit.")


def bind_created(connection, revision, broadcast, stream, channel, before_send=None):
    params = {"id": broadcast, "streamId": stream, "part": "id,snippet,contentDetails"}
    item = _create(connection, revision, "liveBroadcasts/bind", params,
                   None, channel, "stream binding", before_send)
    details = item.get("contentDetails")
    if item["id"] != broadcast or not isinstance(details, dict) or details.get("boundStreamId") != stream:
        raise YouTubeWriteUnconfirmed("stream binding")


def broadcast_details(connection, revision, broadcast_id, channel_id=None):
    result = _get(connection, revision, "liveBroadcasts",
                  {"id": broadcast_id, "part": BROADCAST_PARTS}, "broadcast lookup")
    items = result.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        abort(404, "That YouTube broadcast is unavailable.")
    item = items[0]
    snippet, status, details = item.get("snippet"), item.get("status"), item.get("contentDetails")
    if (item.get("id") != broadcast_id or not isinstance(snippet, dict)
            or (channel_id is not None and snippet.get("channelId") != channel_id)
            or not isinstance(status, dict) or not isinstance(details, dict)):
        abort(404, "That YouTube broadcast is unavailable.")
    return snippet, status, details


def restart_details(connection, revision, broadcast):
    if not isinstance(broadcast, str) or not IDENTIFIER.fullmatch(broadcast):
        abort(400, "Invalid broadcast identifier.")
    snippet, status, _ = broadcast_details(connection, revision, broadcast)
    channel = snippet.get("channelId")
    privacy = status.get("privacyStatus")
    lifecycle = status.get("lifeCycleStatus")
    if (not isinstance(channel, str) or not IDENTIFIER.fullmatch(channel)
            or privacy not in PRIVACY
            or not isinstance(lifecycle, str)):
        abort(409, "That YouTube broadcast is invalid.")
    return channel, privacy, lifecycle


def finish_broadcast(connection, revision, broadcast, channel, before_send=None):
    _, previous, _ = broadcast_details(connection, revision, broadcast, channel)
    lifecycle = previous.get("lifeCycleStatus")
    if lifecycle in ("complete", "revoked", "ready", "created"):
        return lifecycle
    if lifecycle not in ("live", "testing"):
        abort(409, "YouTube is still changing the broadcast status. Try again shortly.")
    params = {"broadcastStatus": "complete", "id": broadcast, "part": "id,snippet,status"}
    try:
        item = _create(connection, revision, "liveBroadcasts/transition", params, None,
                       channel, "broadcast transition", before_send)
        status = item.get("status")
        if (item.get("id") == broadcast and isinstance(status, dict)
                and status.get("lifeCycleStatus") in ("complete", "revoked")):
            return status["lifeCycleStatus"]
    except YouTubeWriteUnconfirmed:
        pass
    except HTTPException as error:
        if error.code != 422:
            raise
    _, status, _ = broadcast_details(connection, revision, broadcast, channel)
    new_lifecycle = status.get("lifeCycleStatus")
    if new_lifecycle not in ("complete", "revoked"):
        abort(409, "That YouTube broadcast is still active.")
    return new_lifecycle


def bound_target(connection, revision, broadcast_id, channel_id):
    if (not isinstance(broadcast_id, str) or not IDENTIFIER.fullmatch(broadcast_id)
            or not isinstance(channel_id, str) or not IDENTIFIER.fullmatch(channel_id)):
        abort(400, "Invalid broadcast or channel identifier.")
    owned, cursor = False, None
    for _ in range(MAX_PAGES):
        page = channels(connection, revision, cursor)
        if any(row["id"] == channel_id for row in page["items"]):
            owned = True
            break
        cursor = page["next"]
        if cursor is None:
            break
    if not owned:
        abort(403, "That channel is not connected to this workspace.")
    snippet, status, details = broadcast_details(connection, revision, broadcast_id, channel_id)
    if status.get("lifeCycleStatus") != "ready":
        abort(409, "That broadcast is not ready for a stream binding.")
    privacy = status.get("privacyStatus")
    stream_id = details.get("boundStreamId")
    if not isinstance(stream_id, str) or not IDENTIFIER.fullmatch(stream_id):
        abort(409, "That broadcast has no bound stream.")
    if details.get("enableAutoStart") is not True or details.get("enableAutoStop") is not True:
        abort(409, "That broadcast is not configured for automatic start and stop.")
    stream = _get(connection, revision, "liveStreams",
                  {"id": stream_id, "part": "id,snippet,cdn,status"}, "stream lookup")
    streams = stream.get("items") if isinstance(stream, dict) else None
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        abort(404, "The bound YouTube stream is unavailable.")
    sitem = streams[0]
    ssnip = sitem.get("snippet") if isinstance(sitem.get("snippet"), dict) else {}
    cdn = sitem.get("cdn") if isinstance(sitem.get("cdn"), dict) else {}
    ing = cdn.get("ingestionInfo") if isinstance(cdn.get("ingestionInfo"), dict) else {}
    if sitem.get("id") != stream_id or ssnip.get("channelId") != channel_id:
        abort(403, "That stream does not match the selected channel.")
    if cdn.get("ingestionType") != "rtmp":
        abort(409, "That stream does not use RTMP ingestion.")
    address, key = ing.get("rtmpsIngestionAddress"), ing.get("streamName")
    title = snippet.get("title")
    if (not isinstance(address, str) or len(address) > MAX_ADDRESS
            or not isinstance(key, str) or not isinstance(title, str)
            or not title or title != title.strip() or len(title) > 256):
        abort(502, "YouTube returned malformed stream details.")
    if not STREAM_KEY.fullmatch(key) or any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in address):
        abort(502, "YouTube returned unsafe stream details.")
    try:
        u = urlsplit(address)
        port = u.port
    except ValueError:
        abort(502, "YouTube returned an invalid RTMPS address.")
    if (u.scheme != "rtmps" or u.hostname not in RTMPS_HOSTS or u.path != "/live2"
            or u.username is not None or u.password is not None or u.query or u.fragment
            or (port is not None and port != 443)):
        abort(502, "YouTube returned an invalid RTMPS address.")
    server = f"rtmps://{u.hostname}:443/live2"
    if len(server) > MAX_ADDRESS:
        abort(502, "YouTube returned an invalid RTMPS address.")
    return {"title": title, "server": server, "key": key, "stream": stream_id, "privacy": privacy}
