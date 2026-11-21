"""Resumable video uploads using the YouTube Data API."""

import re
from urllib.parse import parse_qs, urlsplit

from flask import abort
from werkzeug.exceptions import TooManyRequests

from workspace_oauth import Config, response_object


OAUTH = Config(
    "youtube", "YouTube", "https://accounts.google.com/o/oauth2/v2/auth",
    "https://oauth2.googleapis.com/token", "https://oauth2.googleapis.com/revoke",
    "https://www.googleapis.com/auth/youtube.upload", dict(access_type="offline", prompt="consent"),
)
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 8 * 1024 * 1024
PRIVACY = ("private", "unlisted", "public")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


def upload_address(address):
    try:
        target, expected = urlsplit(address), urlsplit(UPLOAD)
        if (len(address) <= 4096 and not any(ord(char) < 33 for char in address) and "\\" not in address
                and (target.scheme, target.netloc, target.path) == (expected.scheme, expected.netloc, expected.path)
                and not target.fragment and parse_qs(target.query, max_num_fields=20).get("upload_id")):
            return address
    except (TypeError, ValueError):
        pass
    abort(502, "YouTube returned an unexpected upload address.")


def delay(response):
    retry = response.headers.get("Retry-After", "")
    return int(retry) if re.fullmatch(r"[0-9]{1,6}", retry) else 0


def provider_error(response):
    if response.status_code == 429:
        raise TooManyRequests("YouTube is rate-limiting uploads.", retry_after=delay(response))
    if response.status_code in (401, 403):
        abort(403, "YouTube refused this upload. Check the connected channel and its upload quota.")
    if 400 <= response.status_code < 500:
        abort(422, "YouTube rejected this video or its upload settings.")
    abort(502, "The YouTube upload service is unavailable.")


def start(connection, revision, metadata, media_type, size):
    params = dict(part="snippet,status", uploadType="resumable", notifySubscribers="false")
    headers = {"X-Upload-Content-Type": media_type, "X-Upload-Content-Length": str(size)}
    with connection.api(revision, "POST", UPLOAD, params=params, headers=headers, json=metadata) as response:
        if response.status_code not in (200, 201):
            provider_error(response)
        return upload_address(response.headers.get("Location", ""))


def transfer(connection, revision, address, media_type, size, offset, body):
    content_range = f"bytes {offset}-{offset + len(body) - 1}/{size}" if body else f"bytes */{size}"
    headers = {"Content-Type": media_type, "Content-Length": str(len(body)), "Content-Range": content_range}
    with connection.api(revision, "PUT", upload_address(address), data=body, headers=headers) as response:
        if response.status_code in (200, 201):
            result = response_object(response)
            remote = result.get("id")
            if not isinstance(remote, str) or not VIDEO_ID.fullmatch(remote):
                abort(502, "YouTube did not include a video ID in its upload receipt.")
            status = result.get("status")
            actual_privacy = status.get("privacyStatus") if isinstance(status, dict) else None
            return {"id": remote, "privacy": actual_privacy if actual_privacy in PRIVACY else None}
        if response.status_code == 404:
            return {"expired": True}
        if response.status_code != 308:
            provider_error(response)
        accepted_range = response.headers.get("Range", "")
        if not accepted_range:
            return {"offset": 0, "delay": delay(response)}
        found = re.fullmatch(r"(?:bytes=)?0-([0-9]{1,12})", accepted_range)
        if not found or int(found[1]) >= size:
            abort(502, "YouTube returned an invalid byte range for this upload.")
        return {"offset": int(found[1]) + 1, "delay": delay(response)}
