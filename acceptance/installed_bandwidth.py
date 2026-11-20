"""Compare the installed aggregate bandwidth report to actual HTTP response bodies."""

import csv
from datetime import datetime, timezone
from html import unescape
from io import StringIO
import re
import time

import requests
from playwright.sync_api import expect


def bandwidth(base, auth, item, **overrides):
    response = requests.post(base + "/api/analytics/bandwidth-report", headers=auth, timeout=10,
                             json={"videos": [item]} | overrides)
    response.raise_for_status()
    return response.json()


def check_bandwidth(page, base, auth, uid):
    endpoint = base + "/api/analytics/bandwidth-report"
    assert requests.post(endpoint, json={}, timeout=10).status_code == 403
    title = "Bandwidth fixture"
    html = requests.get(f"{base}/watch/work/{uid}", timeout=10)
    html.raise_for_status()
    media = base + unescape(re.search(r'<video\b[^>]*\bsrc="([^"]+)"', html.text).group(1))
    assert bandwidth(base, auth, uid)["totals"]["total_bytes"] == 0
    head = requests.head(media, timeout=10)
    head.raise_for_status()
    size = int(head.headers["Content-Length"])
    assert size > 100
    assert requests.get(media, headers={"Range": f"bytes={size}-"}, timeout=10).status_code == 416
    part = requests.get(media, headers={"Range": "bytes=20-99"}, timeout=10)
    assert part.status_code == 206 and len(part.content) == 80
    download = requests.get(media + "?download=1", timeout=10)
    assert download.status_code == 200 and len(download.content) == size
    assert download.headers.get("Content-Disposition", "").startswith("attachment")
    assert part.content == download.content[20:100]

    expected = size + 80
    deadline = time.monotonic() + 10
    result = bandwidth(base, auth, uid, bandwidth_group="video")
    while result["totals"]["total_bytes"] < expected and time.monotonic() < deadline:
        time.sleep(.15)
        result = bandwidth(base, auth, uid, bandwidth_group="video")
    totals = {"live_bytes": 0, "vod_bytes": expected, "total_bytes": expected}
    assert result["totals"] == totals, result
    assert result["rows"] == [totals | {"item": uid, "video": uid, "label": title}]
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert bandwidth(base, auth, uid)["rows"] == [totals | {"item": None, "video": None, "label": month}]
    assert bandwidth(base, auth, uid, **{"from": "2000-01-01", "to": "2000-01-01"})["totals"]["total_bytes"] == 0

    page.goto(f"{base}/operator/analytics?video={uid}")
    page.get_by_label("Report type").select_option("bandwidth")
    expect(page.locator("#bandwidthReport")).to_be_visible()
    expect(page.locator("#bandwidthRows")).to_contain_text(month)
    page.get_by_label("Bandwidth by").select_option("video")
    page.get_by_role("button", name="Apply filters").click()
    expect(page.locator("#bandwidthRows")).to_contain_text(title)
    with page.expect_download() as downloaded:
        page.get_by_role("button", name="Download CSV").click()
    assert downloaded.value.suggested_filename == "cueporch-bandwidth-report.csv"
    rows = list(csv.DictReader(StringIO(downloaded.value.path().read_text(encoding="utf-8"))))
    assert [(row["content_id"], row["live_bytes"], row["on_demand_bytes"], row["total_bytes"]) for row in rows] == [
        ("", "0", str(expected), str(expected)), (uid, "0", str(expected), str(expected)),
    ]
    return dict(ranged_get_bytes=80, download_bytes=size, on_demand_bytes=expected,
                head_and_invalid_range_excluded=True, utc_month_content_and_csv=True)
