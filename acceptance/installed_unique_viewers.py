"""Prove repeat viewing, load-only impressions, and unavailable viewer identity."""

import csv
from io import StringIO
import time

from playwright.sync_api import expect
import requests


def check_unique_viewers(page, browser, base, auth, uid):
    url = f"{base}/watch/work/{uid}"
    endpoint = base + "/api/analytics/video-report"

    def result():
        response = requests.post(endpoint, headers=auth, timeout=10, json={"videos": [uid], "group": "video"})
        response.raise_for_status()
        return response.json()["totals"]

    def started(context, do_play):
        view = context.new_page()
        with view.expect_response(lambda r: "/viewer-analytics/" in r.url and r.status == 204):
            view.goto(url)
        if do_play:
            player = view.locator("video")
            assert player.evaluate("async v => {v.muted = true; await v.play(); return v.videoWidth;}") == 256
            view.wait_for_function("document.querySelector('video').ended", timeout=10000)
        return view

    repeat = browser.new_context()
    load_only = browser.new_context()
    blocked = browser.new_context()
    blocked.add_init_script("Object.defineProperty(window, 'localStorage', { get() { throw new Error('storage unavailable'); } });")
    try:
        started(repeat, True).close()
        started(repeat, True).close()
        started(load_only, False).close()
        started(blocked, True).close()
        deadline = time.monotonic() + 15
        metrics = result()
        while metrics["finishes"] < 3 and time.monotonic() < deadline:
            time.sleep(.15)
            metrics = result()
        assert metrics["impressions"] == 4 and metrics["views"] == metrics["finishes"] == 3, metrics
        assert metrics["unique_impressions"] == 2 and metrics["unique_viewers"] == 1, metrics
        assert metrics["identified_impressions"] == 3 and metrics["identified_views"] == 2, metrics
        assert .95 <= metrics["average_watched_fraction"] <= 1.1, metrics

        page.goto(f"{base}/operator/analytics?video={uid}")
        expect(page.locator("#reportTotals")).to_contain_text("Unique viewers")
        expect(page.locator("#uniqueCoverage")).to_contain_text("2 of 3 views")
        page.locator("#trendMetric").select_option("average_watched_fraction")
        expect(page.locator("#reportTrend")).to_contain_text("%")
        with page.expect_download() as downloaded:
            page.get_by_role("button", name="Download CSV").click()
        exported = list(csv.DictReader(StringIO(downloaded.value.path().read_text(encoding="utf-8"))))
        assert len(exported) == 1 and exported[0]["unique_viewers"] == "1" and exported[0]["unique_impressions"] == "2"
    finally:
        for context in (repeat, load_only, blocked):
            context.close()
    return dict(impressions=4, views=3, unique_impressions=2, unique_viewers=1,
                identified_impressions=3, identified_views=2,
                average_watched_fraction=metrics["average_watched_fraction"], trend_and_csv=True)
