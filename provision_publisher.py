"""Create or verify the limited publishing identity on a privately provisioned PeerTube host."""

import argparse
import json
import os
import time
from pathlib import Path
import requests
from peertube_destination import PeerTube


def provision(origin, keys, password):
    for _ in range(180):
        try:
            if requests.get(origin + "/api/v1/config", timeout=2).ok:
                break
        except requests.RequestException:
            pass
        time.sleep(2)
    else:
        raise RuntimeError("PeerTube did not reach readiness")
    username = "cueporch_publisher"
    try:
        PeerTube(origin, username, keys["publisher"])
    except requests.HTTPError as exc:
        if exc.response.status_code not in (400, 401):
            raise
        admin = PeerTube(origin, "root", password)
        admin.call(
            "POST",
            "/api/v1/users",
            json=dict(
                username=username,
                email="cueporch-publisher@example.invalid",
                password=keys["publisher"],
                role=2,
                videoQuota=-1,
                videoQuotaDaily=-1,
            ),
        )
        PeerTube(origin, username, keys["publisher"])
    return username


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("origin")
    p.add_argument("owner_file", type=Path)
    args = p.parse_args()
    provision(
        args.origin,
        json.loads(args.owner_file.read_text()),
        os.environ["PEERTUBE_PASSWORD"],
    )
    print("Publishing identity verified")
