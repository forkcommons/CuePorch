"""Measure actual viewer playback from the installed package."""

import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import time
import uuid

import requests
from playwright.sync_api import expect


def check_analytics(page, browser, base, auth, museum, venue, clip):
    with clip.open("rb") as stream:
        upload = requests.post(
            base + "/api/library/uploads", headers=auth, timeout=30,
            files={"file": ("Analytics rehearsal.mp4", stream, "video/mp4")},
            data=dict(upload_id=str(uuid.uuid4()), privacy=json.dumps({
                "visibility": "public", "embed": "anywhere", "domains": [], "download": False,
            })),
        )
    upload.raise_for_status()
    uid = upload.json()["id"]
    endpoint = f"{base}/api/library/work/{uid}/analytics"
    assert requests.get(endpoint, timeout=10).status_code == 403

    def read():
        response = requests.get(endpoint, headers=auth, timeout=10)
        response.raise_for_status()
        return response.json()

    def wait_for(predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            report = read()
            if predicate(report["totals"]):
                return report
            time.sleep(.2)
        raise AssertionError(f"Expected playback metrics. Last report: {report}")

    guest = browser.new_context()
    previous = museum.embed
    try:
        watched_page = guest.new_page()
        watched_page.goto(f"{base}/watch/work/{uid}")
        site = guest.new_page()
        museum.embed = f"{base}/embed/work/{uid}"
        site.goto(venue)
        expect(site.frame_locator("iframe").locator("video")).to_be_visible()
        initial = wait_for(lambda total: total["impressions"] == 2)
        assert initial["totals"]["views"] == 0 and initial["totals"]["watched_seconds"] == 0
        video = watched_page.locator("video")
        assert video.evaluate("async v => {v.muted=true;await v.play();return v.videoWidth}") == 256
        watched_page.wait_for_function("document.querySelector('video').ended", timeout=9500)
        final = wait_for(lambda total: total["finishes"] == 1 and total["watched_seconds"] >= 1.75)
        metrics = final["totals"]
        assert metrics["impressions"] == 2 and metrics["views"] == 1 and metrics["view_rate"] == .5
        assert final["retention"][0] == {"second": 0, "percent": 1}
        assert {(day["surface"], day["views"]) for day in final["daily"]} == {("watch", 1), ("embed", 0)}

        receiver = base + video.get_attribute("data-analytics")
        replay = {"sequence": 1, "played": False, "completed": False, "watched": 0, "intervals": []}
        assert requests.post(receiver, json=replay, timeout=10).status_code == 204
        assert read()["totals"] == metrics
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        previous_day = requests.get(endpoint, headers=auth, params={"from": yesterday, "to": yesterday}, timeout=10)
        previous_day.raise_for_status()
        assert previous_day.json()["totals"]["impressions"] == 0

        page.goto(f"{base}/operator/videos/{uid}")
        page.get_by_role("link", name="Analytics", exact=True).click()
        expect(page.locator("#impressions")).to_have_text("2")
        expect(page.locator("#views")).to_have_text("1")
        expect(page.locator("#viewRate")).to_have_text("50%")
        expect(page.locator("#retentionPlot")).to_be_visible()
        page.get_by_text("Retention data", exact=True).click()
        expect(page.get_by_role("button", name="1s", exact=True)).to_be_visible()
        preview = page.get_by_label("Video preview")
        page.get_by_role("button", name="1s", exact=True).click()
        page.wait_for_function("document.querySelector('#retentionPreview').currentTime >= .99")
        assert preview.evaluate("v => v.videoWidth") == 256
        page.get_by_label("Playback surface").select_option("embed")
        page.get_by_role("button", name="Apply filters").click()
        expect(page.locator("#impressions")).to_have_text("1")
        expect(page.locator("#views")).to_have_text("0")
        with page.expect_download() as downloaded:
            page.get_by_role("button", name="Download CSV").click()
        csv_text = downloaded.value.path().read_text(encoding="utf8")
        rows = list(csv.DictReader(StringIO(csv_text)))
        assert len(rows) == 1 and rows[0]["surface"] == "embed" and rows[0]["views"] == "0"

        requests.put(f"{base}/api/library/work/{uid}/privacy", headers=auth, timeout=10, json={
            "visibility": "private", "embed": "disabled", "domains": [], "download": False,
        }).raise_for_status()
        assert requests.post(receiver, json=replay, timeout=10).status_code == 404
    finally:
        museum.embed = previous
        guest.close()
    return dict(actual_impressions=2, actual_views=1, view_rate=.5, playback_finished=True,
                time_watched_seconds=metrics["watched_seconds"], replayed_observation_ignored=True,
                dated_surface_report_and_csv=True, retention_preview_seek_and_decode=True,
                denies_old_observation_after_video_private=True)
