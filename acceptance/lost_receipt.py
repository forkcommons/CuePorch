"""Drop a real completion response after PeerTube accepts the final bytes, then reconcile on retry."""

import argparse, json, os, sys, tempfile
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peertube_destination import PeerTube

p = argparse.ArgumentParser()
p.add_argument("origin")
p.add_argument("collection", type=Path)
a = p.parse_args()
work = json.loads((a.collection / "manifest.json").read_text())["works"][0]
peer = PeerTube(
    a.origin, os.environ["PEERTUBE_USERNAME"], os.environ["PEERTUBE_PASSWORD"]
)
real = peer.call
accepted = []


def interrupted(method, path, **options):
    r = real(method, path, **options)
    if (
        method == "PUT"
        and "upload-resumable" in path
        and r.status_code == 200
        and not accepted
    ):
        accepted.append(r.json()["video"]["uuid"])
        raise requests.ConnectionError(
            "Simulated loss of the successful completion reply"
        )
    return r


with tempfile.TemporaryDirectory() as temp:
    receipt = Path(temp) / "receipt.json"
    peer.call = interrupted
    try:
        peer.publish(a.collection, work, receipt, False)
    except requests.ConnectionError:
        pass
    else:
        raise AssertionError("Completion loss did not run")
    assert "video" not in json.loads(receipt.read_text())
    peer.call = real
    recovered = peer.publish(a.collection, work, receipt, False)
    assert accepted == [recovered]
    print(
        json.dumps(
            {
                "lost_acknowledgement": True,
                "recovered_uuid": recovered,
                "additional_video_created": False,
            },
            indent=2,
        )
    )
