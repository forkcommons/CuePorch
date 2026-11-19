"""Configure an institution's PeerTube live destination and keep private encoder details in a file."""

import argparse
import os
from pathlib import Path

from cueporch import dump
from peertube_destination import PeerTube


def create_live(peer, name, permanent=False):
    result = peer.call(
        "POST",
        "/api/v1/videos/live",
        json={
            "name": name,
            "channelId": peer.user["videoChannels"][0]["id"],
            "privacy": 1,
            "permanentLive": permanent,
            "saveReplay": False,
        },
    )
    uid = result.json()["video"]["uuid"]
    settings = peer.call("GET", "/api/v1/videos/live/" + uid).json()
    return dict(
        video=uid,
        embed=peer.origin + "/videos/embed/" + uid,
        rtmp=settings["rtmpUrl"],
        key=settings["streamKey"],
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("origin")
    p.add_argument("title")
    p.add_argument("encoder_settings", type=Path)
    p.add_argument("--permanent", action="store_true")
    args = p.parse_args()
    if args.encoder_settings.exists():
        p.error("Choose a new settings file")
    peer = PeerTube(
        args.origin, os.environ["PEERTUBE_USERNAME"], os.environ["PEERTUBE_PASSWORD"]
    )
    settings = create_live(peer, args.title, args.permanent)
    dump(args.encoder_settings, settings)
    os.chmod(args.encoder_settings, 0o600)
    print(settings["embed"])
