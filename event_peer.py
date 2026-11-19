"""CuePorch event occurrences backed by the connected PeerTube live API."""

from contextlib import contextmanager
import os
import uuid

import requests

from peertube_destination import PeerTube


LATENCY = {"standard": 1, "fail-safe": 2, "low": 3}


def host_available():
    return all(os.environ.get(name) for name in ("CUEPORCH_PEERTUBE_ORIGIN", "PEERTUBE_USERNAME", "PEERTUBE_PASSWORD"))


@contextmanager
def connected_host(expected=None):
    origin = os.environ.get("CUEPORCH_PEERTUBE_ORIGIN", "").rstrip("/")
    if not host_available() or (expected and expected != origin):
        raise ValueError("Connect the PeerTube host for this occurrence.")
    host = PeerTube(origin, os.environ["PEERTUBE_USERNAME"], os.environ["PEERTUBE_PASSWORD"])
    try:
        yield host
    finally:
        host.session.close()


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


def status(video):
    public = video["privacy"]["id"] == 1
    viewers = video.get("viewers", 0)
    state = video["state"]["id"]
    if not video["isLive"]:
        return dict(state="replay" if state == 1 else "preparing_replay", public=public, viewers=viewers)
    published = state == 1 and bool(video.get("streamingPlaylists"))
    return dict(state="live" if published else "waiting_for_encoder", public=public, viewers=viewers)


def observe(host, target):
    return status(host.call("GET", "/api/v1/videos/" + target).json())


def public_status(origin, target):
    response = requests.get(f"{origin}/api/v1/videos/{target}", timeout=(2, 5), allow_redirects=False)
    if response.is_redirect:
        raise ValueError("Unexpected redirect from the video host.")
    response.raise_for_status()
    return status(response.json())
