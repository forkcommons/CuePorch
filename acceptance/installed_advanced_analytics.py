"""Exercise cross-video reports using browser playback from two devices and origins."""

import csv
from datetime import date, datetime, timezone
from io import StringIO
import json
import time
from urllib.parse import urlsplit
import uuid

import requests
from playwright.sync_api import expect


def check_advanced_analytics(page, browser, base, auth, museum, venue, clip):
    endpoint = base + "/api/analytics/video-report"
    today = datetime.now(timezone.utc).date().isoformat()
    title_a, title_b = "Advanced report station", "Advanced report festival"

    def upload(title):
        with clip.open("rb") as stream:
            response = requests.post(
                base + "/api/library/uploads", headers=auth, timeout=30,
                files={"file": (title + ".mp4", stream, "video/mp4")},
                data=dict(upload_id=str(uuid.uuid4()), privacy=json.dumps({
                    "visibility": "public", "embed": "anywhere", "domains": [], "download": False,
                })),
            )
        response.raise_for_status()
        return response.json()["id"]

    a, b = upload(title_a), upload(title_b)
    assert requests.post(endpoint, json={}, timeout=10).status_code == 403
    filters = {"from": today, "to": today, "group": "video", "breakdown": "device", "videos": [a, b]}
    assert requests.post(endpoint, headers=auth, json=filters | {"breakdown": "video"}, timeout=10).status_code == 422

    def report(**overrides):
        response = requests.post(endpoint, headers=auth, timeout=10, json=filters | overrides)
        response.raise_for_status()
        return response.json()

    desktop = browser.new_context()
    phone = browser.new_context(user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1")
    former_embed = museum.embed
    try:
        play_a = desktop.new_page()
        play_a.goto(f"{base}/watch/work/{a}")
        play_b = phone.new_page()
        museum.embed = f"{base}/embed/work/{b}"
        play_b.goto(venue)
        embedded = play_b.frame_locator("iframe").locator("video")
        watch = play_a.locator("video")
        for video in (embedded, watch):
            assert video.evaluate("async v=>{v.muted=true;await v.play();return v.videoWidth}") == 256
        deadline = time.monotonic() + 15
        result = report()
        while result["totals"]["finishes"] < 2 and time.monotonic() < deadline:
            time.sleep(.2)
            result = report()
        assert result["totals"]["views"] == result["totals"]["finishes"] == 2
        assert {(row["category"], row["breakdown"], row["views"]) for row in result["rows"]} == {
            (title_a, "Desktop", 1), (title_b, "Phone", 1),
        }
        url = urlsplit(venue)
        origin = url.scheme + "://" + url.netloc
        assert report(group="source", breakdown="surface", source=origin)["rows"][0]["category"] == origin
        assert report(videos=[a], surface="embed")["totals"]["views"] == 0
        monthly = report(period="month")["overview"]
        assert len(monthly) == 1 and monthly[0]["period"] == date.fromisoformat(today).replace(day=1).isoformat()

        page.goto(f"{base}/operator/videos/{a}/analytics")
        page.get_by_role("link", name="Advanced report").click()
        expect(page.locator("#reportRows")).to_contain_text(title_a)
        expect(page.locator("#reportRows")).not_to_contain_text(title_b)
        page.get_by_text("1 selected", exact=True).click()
        page.get_by_label("Search library videos").fill("Advanced report")
        page.get_by_role("listbox", name="Videos", exact=True).select_option([a, b])
        page.get_by_label("Group by").select_option("device")
        page.get_by_label("Break down by").select_option("video")
        page.get_by_label("Time scale").select_option("week")
        page.get_by_role("button", name="Apply filters").click()
        expect(page.locator("#reportRows > tr")).to_have_count(2)
        expect(page.locator("#reportRows")).to_contain_text("Phone")
        expect(page.locator("#reportRows")).to_contain_text(title_b)
        expect(page.locator("#reportTrend > tr")).to_have_count(1)
        with page.expect_download() as downloaded:
            page.get_by_role("button", name="Download CSV").click()
        csv_rows = list(csv.DictReader(StringIO(downloaded.value.path().read_text(encoding="utf8"))))
        assert {(row["device"], row["video"], row["video_id"]) for row in csv_rows} == {
            ("Desktop", title_a, a), ("Phone", title_b, b),
        }
    finally:
        museum.embed = former_embed
        phone.close()
        desktop.close()
    return dict(real_watched_views=2, aggregate_library_and_video_picker=True, device_and_referring_origin=True,
                watch_and_embed_filters=True, daily_weekly_monthly_trends=True, second_dimension_and_csv=True)
