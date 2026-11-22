"""CuePorch event occurrences backed by the connected PeerTube live API."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from threading import Lock
from time import monotonic
from urllib.parse import urlencode
import uuid

import requests

from peertube_destination import PeerTube


LATENCY = {"standard": 1, "fail-safe": 2, "low": 3}
_login_lock = Lock()
_login = None


class UnsupportedDvrWindow(ValueError):
    pass


def host_available():
    return all(os.environ.get(name) for name in ("CUEPORCH_PEERTUBE_ORIGIN", "PEERTUBE_USERNAME", "PEERTUBE_PASSWORD"))


def _host_access(expected=None):
    global _login
    credentials = (os.environ.get("CUEPORCH_PEERTUBE_ORIGIN", "").rstrip("/"),
                   os.environ.get("PEERTUBE_USERNAME"), os.environ.get("PEERTUBE_PASSWORD"))
    if not all(credentials) or (expected and expected != credentials[0]):
        raise ValueError("Connect the PeerTube host for this occurrence.")
    with _login_lock:
        if _login is None or _login[0] != credentials or monotonic() >= _login[1][1]:
            peer = PeerTube(*credentials)
            try:
                access = peer.session.headers["Authorization"], peer.expires_at, peer.user
                _login = credentials, access
            finally:
                peer.session.close()
        return _login


def expire_authorization(authorization):
    global _login
    with _login_lock:
        if _login is not None and _login[1][0] == authorization:
            _login = None


def authorization(expected):
    return _host_access(expected)[1][0]


@contextmanager
def connected_host(expected=None):
    credentials, access = _host_access(expected)
    host = PeerTube(*credentials, access=access)
    try:
        yield host
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 401:
            expire_authorization(access[0])
        raise
    finally:
        host.session.close()


def audience_stats(gateway, origin, remote, start, end):
    start_utc = datetime.fromtimestamp(start, timezone.utc)
    end_utc = datetime.fromtimestamp(end, timezone.utc) - timedelta(milliseconds=1)
    query = urlencode({"startDate": start_utc.isoformat(), "endDate": end_utc.isoformat()})
    path = f"/api/v1/videos/{uuid.UUID(remote)}/stats/overall?{query}"
    with gateway.read(origin, path, stream=True) as response:
        body = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            body.extend(chunk)
            if len(body) > 65536:
                raise ValueError("response too large")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("expected object")
    result = {}
    for key in ("totalViewers", "viewersPeak", "totalWatchTime", "averageWatchTime"):
        value = payload.get(key)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"invalid {key}")
            if (isinstance(value, float) and not math.isfinite(value)) or value < 0:
                raise ValueError(f"invalid {key}")
        result[key] = value
    return result


def find_remote(host, marker):
    offset = 0
    matches = []
    while True:
        page = host.call("GET", "/api/v1/users/me/videos", params={"start": offset, "count": 100}).json()
        for row in page["data"]:
            if row["name"] == marker and row["isLive"] and row["privacy"]["id"] == 3:
                matches.append(str(uuid.UUID(row["uuid"])))
        offset += len(page["data"])
        if offset >= page["total"]:
            break
        if not page["data"]:
            raise ValueError("Incomplete PeerTube inventory.")
    if len(matches) > 1:
        raise ValueError("Several private PeerTube lives match the occurrence.")
    return matches[0] if matches else None


def stage_remote(host, marker, event):
    previous = find_remote(host, marker)
    if previous:
        return previous
    channels = host.user.get("videoChannels") or []
    if not channels:
        raise ValueError("The connected account needs a PeerTube channel.")
    body = dict(name=marker, channelId=channels[0]["id"], privacy=3, permanentLive=False,
                saveReplay=event["save_replay"], latencyMode=LATENCY[event["latency"]], downloadEnabled=False)
    if event.get("dvr_seconds") is not None:
        seconds = event["dvr_seconds"]
        if seconds:
            limit = host.call("GET", "/api/v1/config").json()["live"]["dvr"]["maxWindow"]
            if seconds > limit:
                raise UnsupportedDvrWindow(f"The connected PeerTube host allows a DVR window of up to {limit} seconds.")
        body["dvrWindow"] = seconds
    if event["save_replay"]:
        body["replaySettings"] = {"privacy": 3}
    created = host.call("POST", "/api/v1/videos/live", json=body).json()
    return str(uuid.UUID(created["video"]["uuid"]))


def release_remote(host, target, event, public):
    title = event["title"]
    fields = dict(name=title if len(title) >= 3 else "Event: " + title, privacy=1 if public else 3)
    if len(event["description"]) >= 3:
        fields["description"] = event["description"]
    host.call("PUT", "/api/v1/videos/" + target, json=fields)


def encoder(host, target):
    remote = host.call("GET", "/api/v1/videos/live/" + target).json()
    return dict(server=remote["rtmpUrl"], secure_server=remote.get("rtmpsUrl"), key=remote["streamKey"])


def live_settings(host, target):
    remote = host.call("GET", "/api/v1/videos/live/" + target).json()
    latency = next((name for name, value in LATENCY.items() if value == remote.get("latencyMode")), None)
    return dict(latency=latency, dvr_seconds=remote.get("dvrWindow"))


def status(video):
    public = video["privacy"]["id"] == 1
    viewers = video.get("viewers", 0)
    state = video["state"]["id"]
    if not video["isLive"]:
        return dict(state="replay" if state == 1 else "preparing_replay", public=public, viewers=viewers)
    if state == 5:
        return dict(state="ended", public=public, viewers=viewers)
    published = state == 1 and bool(video.get("streamingPlaylists"))
    return dict(state="live" if published else "waiting_for_encoder", public=public, viewers=viewers)


def observe(host, target, *, include_duration=False):
    raw = host.call("GET", "/api/v1/videos/" + target).json()
    result = status(raw)
    if include_duration and result["state"] == "replay":
        result["duration"] = raw.get("duration")
    return result


def public_status(origin, target):
    response = requests.get(f"{origin}/api/v1/videos/{target}", timeout=(2, 5), allow_redirects=False)
    if response.is_redirect:
        raise ValueError("Unexpected redirect from the video host.")
    response.raise_for_status()
    raw = response.json()
    return status(raw) | {"duration": raw.get("duration")}
