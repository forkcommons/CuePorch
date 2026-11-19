"""Create or verify the limited publishing identity on a privately provisioned PeerTube host."""

import argparse
import json
import os
import time
from pathlib import Path
import requests
from peertube_destination import PeerTube

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("origin")
p.add_argument("owner_file", type=Path)
args = p.parse_args()
keys = json.loads(args.owner_file.read_text())
for _ in range(180):
    try:
        if requests.get(args.origin + "/api/v1/config", timeout=2).ok:
            break
    except requests.RequestException:
        pass
    time.sleep(2)
else:
    raise SystemExit("PeerTube did not reach readiness")
username = "cueporch_publisher"
try:
    PeerTube(args.origin, username, keys["publisher"])
except requests.HTTPError as exc:
    if exc.response.status_code not in (400, 401):
        raise
    admin = PeerTube(args.origin, "root", os.environ["PEERTUBE_PASSWORD"])
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
    PeerTube(args.origin, username, keys["publisher"])
print("Publishing identity verified")
