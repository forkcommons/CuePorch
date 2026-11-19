"""Deliver a migrated collection to PeerTube and write the operator's embed map."""

import argparse
import csv
import json
import os
from pathlib import Path

from filelock import FileLock
from cueporch import dump, verify
from peertube_destination import PeerTube


def deliver(peer, collection, directory, public_eligible=False):
    verify(collection)
    manifest = json.loads((collection / "manifest.json").read_text(encoding="utf8"))
    directory.mkdir(parents=True, exist_ok=True)
    report = []
    with FileLock(str(directory / "delivery.lock"), timeout=0):
        for work in manifest["works"]:
            uid = work["id"]
            result = {
                "work_id": uid,
                "title": work["title"],
                "source_url": work["source_url"],
                "quality": work["disposition"],
                "peer_id": None,
                "visibility": "private",
                "embed": None,
            }
            media = any(
                item["role"] in ("original", "playback") for item in work["files"]
            )
            if media:
                public = (
                    public_eligible
                    and work.get("source_privacy") in ("anybody", "public")
                    and work.get("source_embed_policy") in ("anywhere", "public")
                    and not work.get("unavailable_metadata", {}).get("/privacy/domains")
                )
                try:
                    video = peer.publish(
                        collection, work, directory / (uid + ".json"), public
                    )
                    result.update(
                        peer_id=video,
                        visibility="public" if public else "private",
                        embed=peer.origin + "/videos/embed/" + video
                        if public
                        else None,
                        status="delivered",
                    )
                except Exception as exc:
                    result.update(status="failed", detail=type(exc).__name__)
            else:
                result["status"] = "owner file needed"
            report.append(result)
            dump(
                directory / "delivery.json",
                {
                    "institution": manifest["institution"],
                    "works": report,
                    "total_source_count": len(manifest["works"]),
                },
            )
        with (directory / "embed-map.csv").open("w", encoding="utf8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "work_id",
                    "source_url",
                    "peer_id",
                    "visibility",
                    "embed",
                    "status",
                ],
            )
            writer.writeheader()
            writer.writerows({k: row[k] for k in writer.fieldnames} for row in report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path)
    parser.add_argument("receipts", type=Path)
    parser.add_argument("origin")
    parser.add_argument(
        "--publish-eligible",
        action="store_true",
        help="Publish only openly viewable and embeddable works",
    )
    args = parser.parse_args()
    peer = PeerTube(
        args.origin, os.environ["PEERTUBE_USERNAME"], os.environ["PEERTUBE_PASSWORD"]
    )
    results = deliver(peer, args.collection, args.receipts, args.publish_eligible)
    print(f"{len(results)} items accounted for in {args.receipts}")
    raise SystemExit(1 if any(r["status"] == "failed" for r in results) else 0)
