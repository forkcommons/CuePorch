"""Verify caption usage from actual selected and decoded browser tracks."""

import csv
from io import StringIO
import time

import requests
from playwright.sync_api import expect


def check_caption_usage(page, browser, base, auth, uid):
    tracks = f"{base}/api/library/work/{uid}/languages"
    response = requests.get(tracks, headers=auth, timeout=10)
    response.raise_for_status()
    version = response.json()["version"]
    for language, kind, cue in (("es", "subtitles", "Subtítulo"), ("en", "captions", "Caption")):
        response = requests.post(tracks, headers=auth, timeout=10,
                                 data=dict(version=version, language=language, kind=kind),
                                 files={"track": (language + ".vtt", f"WEBVTT\n\n00:00:00.000 --> 00:00:04.000\n{cue}\n".encode())})
        response.raise_for_status()
    assert {item["language"]: item["kind"] for item in response.json()["tracks"]} == {"es": "subtitles", "en": "captions"}
    appearance = f"{base}/api/library/work/{uid}/appearance/watch"
    requests.put(appearance, headers=auth, json={"captions": True}, timeout=10).raise_for_status()

    def report(**options):
        response = requests.post(base + "/api/analytics/video-report", headers=auth, timeout=10,
                                 json={"videos": [uid], "group": "video", "breakdown": "caption"} | options)
        response.raise_for_status()
        return response.json()

    for mode in ("switch", "off", "opt-out"):
        context = browser.new_context(extra_http_headers={"DNT": "1"} if mode == "opt-out" else {})
        try:
            view = context.new_page()
            view.goto(f"{base}/watch/work/{uid}")
            player = view.locator("video")
            expect(player.locator('track[srclang="es"]')).to_have_attribute("kind", "subtitles")
            expect(player.locator('track[srclang="en"]')).to_have_attribute("kind", "captions")
            captions = view.locator('[data-control="captions"]')
            if mode in ("switch", "opt-out"):
                captions.select_option(label="es")
                view.wait_for_function("document.querySelector('video').textTracks[0].cues?.length === 1")
            assert player.evaluate("async v => {v.muted = true; await v.play(); return v.videoWidth;}") == 256
            if mode == "switch":
                view.wait_for_function("document.querySelector('video').currentTime >= 1.3")
                captions.select_option(label="en")
                view.wait_for_function("document.querySelector('video').textTracks[1].cues?.length === 1")
            view.wait_for_function("document.querySelector('video').ended", timeout=12000)
        finally:
            context.close()

    deadline = time.monotonic() + 12
    result = report()
    while result["totals"]["finishes"] < 2 and time.monotonic() < deadline:
        time.sleep(.15)
        result = report()
    assert result["totals"]["views"] == result["totals"]["finishes"] == 2, result
    slices = {row["breakdown"]: row for row in result["rows"]}
    assert set(slices) == {"en · Captions", "es · Subtitles", "Off"}, slices
    for label in slices:
        assert slices[label]["views"] == 1 and slices[label]["watched_seconds"] > 0.6, slices
    assert sum(row["watched_seconds"] for row in slices.values()) <= result["totals"]["watched_seconds"] + .01
    spanish = report(caption_language="es", caption_kind="subtitles")
    assert spanish["totals"]["views"] == 1 and spanish["top"]["video"][0]["views"] == 1
    assert spanish["totals"]["watched_seconds"] == slices["es · Subtitles"]["watched_seconds"]
    assert report(caption_language="es", caption_kind="captions")["totals"]["views"] == 0

    page.goto(f"{base}/operator/analytics?video={uid}")
    page.locator('select[name="breakdown"]').select_option("caption")
    page.get_by_label("Text track language").fill("es")
    page.locator('select[name="caption_kind"]').select_option("subtitles")
    page.get_by_role("button", name="Apply filters").click()
    expect(page.locator("#reportRows")).to_contain_text("es · Subtitles")
    expect(page.locator("#reportRows > tr")).to_have_count(1)
    expect(page.locator("#captionBreakdownNote")).to_be_visible()
    with page.expect_download() as downloaded:
        page.get_by_role("button", name="Download CSV").click()
    exported = list(csv.DictReader(StringIO(downloaded.value.path().read_text(encoding="utf-8"))))
    assert [(row["caption"], row["views"]) for row in exported] == [("es · Subtitles", "1")]
    return dict(views=2, decoded_width=256, browser_switch=True, opt_out=True,
                watched_seconds={name: row["watched_seconds"] for name, row in slices.items()}, filtered_csv=True)
