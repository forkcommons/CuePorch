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

from installed_versions import generate_replacement


def check_advanced_analytics(page, browser, base, auth, museum, venue, clip,
                             verify_region=False, verify_bandwidth=False, verify_unique=False):
    endpoint = base + "/api/analytics/video-report"
    today = datetime.now(timezone.utc).date().isoformat()
    title_a, title_b = "Advanced report station", "Advanced report festival"

    def upload(title, video, downloads=False):
        with video.open("rb") as stream:
            response = requests.post(
                base + "/api/library/uploads", headers=auth, timeout=30,
                files={"file": (title + ".mp4", stream, "video/mp4")},
                data=dict(upload_id=str(uuid.uuid4()), privacy=json.dumps({
                    "visibility": "public", "embed": "anywhere", "domains": [], "download": downloads,
                })),
            )
        response.raise_for_status()
        return response.json()["id"]

    longer = generate_replacement(clip.with_name(f"longer-report-{uuid.uuid4().hex}.mp4"), seconds=4)
    a, b = upload(title_a, clip), upload(title_b, longer)

    def configure_rating(uid, mode):
        url = f"{base}/api/library/work/{uid}/rating"
        settings = requests.get(url, headers=auth, timeout=10)
        settings.raise_for_status()
        previous = settings.json()
        requests.put(url, headers=auth, timeout=10, json={
            "previous": previous["settings"], "version": previous["version"], "mode": mode,
            "prompt": "Rate this screening", "start": 0,
        }).raise_for_status()

    configure_rating(a, "thumbs")
    configure_rating(b, "stars")
    assert requests.post(endpoint, json={}, timeout=10).status_code == 403
    filters = {"from": today, "to": today, "group": "video", "breakdown": "device", "videos": [a, b]}
    assert requests.post(endpoint, headers=auth, json=filters | {"breakdown": "video"}, timeout=10).status_code == 422

    def report(**overrides):
        response = requests.post(endpoint, headers=auth, timeout=10, json=filters | overrides)
        response.raise_for_status()
        return response.json()

    def additional_report(kind, **overrides):
        response = requests.post(f"{base}/api/analytics/{kind}-report", headers=auth, timeout=10,
                                 json=filters | overrides)
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
        for surface, label in ((play_a, "Thumbs up"), (play_b.frame_locator("iframe"), "4 stars")):
            widget = surface.locator(".video-rating")
            widget.get_by_role("button", name=label, exact=True).click()
            expect(widget.locator("[data-rating-message]")).to_have_text("Rating recorded.")
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

        engagement = additional_report("engagement")
        totals = engagement["totals"]
        assert totals["views"] == totals["measured"] == totals["finishes"] == 2
        assert totals["completion"] == 1 and totals["points"]["50"] == totals["points"]["100"] == 2
        assert len(engagement["retention"]) == 101 and engagement["retention"][50] == engagement["retention"][100] == 2
        assert {row["title"] for row in engagement["rows"]} == {title_a, title_b}
        assert additional_report("engagement", device="Phone")["totals"]["views"] == 1
        ratings = additional_report("rating")
        assert {row["mode"]: (row["votes"], row["average"], row["scores"]) for row in ratings["totals"]} == {
            "thumbs": (1, 1, [0, 1]), "stars": (1, 4, [0, 0, 0, 1, 0]),
        }
        assert [row["video"] for row in additional_report("rating", videos=[b])["rows"]] == [b]
        for kind in ("rating", "engagement"):
            assert requests.post(f"{base}/api/analytics/{kind}-report", json=filters, timeout=10).status_code == 403

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
        kinds = page.get_by_label("Report type")
        kinds.select_option("engagement")
        expect(page.locator("#engagementReport")).to_be_visible()
        expect(page.locator("#videoReport")).to_be_hidden()
        expect(page.locator("#engagementRows > tr")).to_have_count(3)
        expect(page.locator("#engagementRows > tr").first.get_by_role("cell").last).to_have_text("2")
        with page.expect_download() as downloaded:
            page.get_by_role("button", name="Download CSV").click()
        rows = list(csv.DictReader(StringIO(downloaded.value.path().read_text(encoding="utf8"))))
        assert {row["video"]: row["views_at_50_percent"] for row in rows} == {"Total": "2", title_a: "1", title_b: "1"}

        kinds.select_option("rating")
        expect(page.locator("#ratingReport")).to_be_visible()
        expect(page.locator('input[name="source"]')).to_be_hidden()
        expect(page.locator("#ratingRows > tr")).to_have_count(4)
        expect(page.locator("#ratingRows")).to_contain_text("4 stars 1")
        with page.expect_download() as downloaded:
            page.get_by_role("button", name="Download CSV").click()
        rows = list(csv.DictReader(StringIO(downloaded.value.path().read_text(encoding="utf8"))))
        assert {(row["video_id"], row["rating_type"], row["votes"]) for row in rows if row["video_id"]} == {
            (a, "thumbs", "1"), (b, "stars", "1"),
        }

        dropoff = desktop.new_page()
        dropoff.goto(f"{base}/watch/work/{b}")
        stop = dropoff.locator("video").evaluate("""async v => {
            v.muted = true;
            await v.play();
            return new Promise((resolve, reject) => {
                const timeout = setTimeout(() => reject(new Error('No playing video frames arrived.')), 8000);
                function sample() {
                    if (v.currentTime < 1.1 && !v.ended) {
                        v.requestVideoFrameCallback(sample);
                        return;
                    }
                    clearTimeout(timeout);
                    v.pause();
                    resolve(v.currentTime);
                }
                v.requestVideoFrameCallback(sample);
            });
        }""")
        assert 1.1 <= stop < 2
        deadline = time.monotonic() + 12
        engagement = additional_report("engagement")
        totals = engagement["totals"]
        while totals["points"]["25"] != 3 and time.monotonic() < deadline:
            time.sleep(.15)
            engagement = additional_report("engagement")
            totals = engagement["totals"]
        assert totals["views"] == 3 and totals["finishes"] == 2
        assert totals["completion"] == 2 / 3
        assert engagement["retention"][25] == 3 and engagement["retention"][50] == 2
    finally:
        museum.embed = former_embed
        phone.close()
        desktop.close()
    results = dict(real_watched_views=2, aggregate_library_and_video_picker=True, device_and_referring_origin=True,
                   watch_and_embed_filters=True, daily_weekly_monthly_trends=True, second_dimension_and_csv=True,
                   engagement_curve_and_milestones=True, compared_two_durations=True,
                   partial_playback_seconds=round(stop, 3), retention_at_25_percent=3, retention_at_50_percent=2,
                   captured_two_browser_votes=True, reports_filter_and_export=True)
    if verify_region:
        from installed_regions import check_regions

        results["regions_and_privacy"] = check_regions(page, browser, base, auth, museum, venue, a, b)
    if verify_bandwidth:
        from installed_bandwidth import check_bandwidth

        results["bandwidth"] = check_bandwidth(page, base, auth, upload("Bandwidth fixture", clip, downloads=True))
    if verify_unique:
        from installed_unique_viewers import check_unique_viewers

        results["unique_viewers"] = check_unique_viewers(page, browser, base, auth, upload("Unique viewer fixture", clip))
    return results
