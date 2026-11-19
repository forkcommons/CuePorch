"""Measure a bounded hosted viewing workload; report the assumptions used for cost projection."""

import argparse
import concurrent.futures
import json
import re
import statistics
import time
from urllib.parse import urljoin
import requests

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("origin")
p.add_argument("video")
p.add_argument("--viewers", type=int, default=8)
p.add_argument("--viewer-hours", type=float, default=1000)
p.add_argument(
    "--egress-per-gb", type=float, default=0.05, help="Planning assumption in USD"
)
p.add_argument(
    "--fixed-monthly", type=float, default=60, help="Planning assumption in USD"
)
a = p.parse_args()


# Software rehearsal targets fixed before execution: eight simultaneous clients, all assets
# fetched, each completes one short VOD within fifteen seconds on this local deployment.
def viewer(_):
    start = time.monotonic()
    with requests.Session() as s:

        def get(url, **opts):
            res = s.get(url, timeout=15, **opts)
            res.raise_for_status()
            return res

        data = get(a.origin + "/api/v1/videos/" + a.video).json()
        master = data["streamingPlaylists"][0]["playlistUrl"]
        playlist = get(master).text
        variant = urljoin(
            master,
            next(
                line
                for line in playlist.splitlines()
                if line and not line.startswith("#")
            ),
        )
        lines = get(variant).text.splitlines()
        delivered = 0
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-MAP"):
                uri = re.search(r'URI="([^"]+)"', line)[1]
                region = re.search(r'BYTERANGE="([0-9]+)@([0-9]+)"', line)
            elif line and not line.startswith("#"):
                uri = line
                region = re.search(r"#EXT-X-BYTERANGE:([0-9]+)@([0-9]+)", lines[i - 1])
            else:
                continue
            headers = {}
            if region:
                size, offset = map(int, region.groups())
                headers["Range"] = f"bytes={offset}-{offset + size - 1}"
            response = get(urljoin(variant, uri), headers=headers)
            if region and response.status_code != 206:
                raise ValueError("Byte range was not honored")
            delivered += len(response.content)
        if not delivered:
            raise ValueError("No media bytes received")
        return time.monotonic() - start, delivered, data["duration"]


with concurrent.futures.ThreadPoolExecutor(max_workers=a.viewers) as pool:
    results = list(pool.map(viewer, range(a.viewers)))
times = [r[0] for r in results]
gb_per_hour = results[0][1] * 3600 / results[0][2] / 1e9
print(
    json.dumps(
        {
            "viewers": len(results),
            "median_seconds": round(statistics.median(times), 3),
            "maximum_seconds": round(max(times), 3),
            "media_bytes_each": results[0][1],
            "content_seconds": results[0][2],
            "threshold_seconds": 15,
            "viewer_hours_assumed": a.viewer_hours,
            "egress_GB_per_hour_at_test_bitrate": round(gb_per_hour, 3),
            "monthly_USD_scenario": round(
                a.fixed_monthly + gb_per_hour * a.viewer_hours * a.egress_per_gb, 2
            ),
            "assumed_fixed_USD": a.fixed_monthly,
            "assumed_USD_per_GB": a.egress_per_gb,
            "cost_limit": "Illustrative workload model. Independent local hosting, staff time, storage growth, backups and billing require deployment-specific inputs.",
        },
        indent=2,
    )
)
assert max(times) <= 15
