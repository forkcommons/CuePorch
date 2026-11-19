"""Verify timed feedback and reported counts from the installed browser player."""

import csv
from io import StringIO
import json
import uuid

import requests
from playwright.sync_api import expect


def check_ratings(page, browser, base, auth, video_file):
    with video_file.open("rb") as stream:
        added = requests.post(
            base + "/api/library/uploads", headers=auth, timeout=25,
            files={"file": ("Rate this video.mp4", stream, "video/mp4")},
            data={"upload_id": str(uuid.uuid4()), "privacy": json.dumps({
                "visibility": "public", "embed": "disabled", "domains": [], "download": False,
            })},
        )
    added.raise_for_status()
    uid = added.json()["id"]
    path = f"{base}/api/library/work/{uid}"
    operator = f"{base}/operator/videos/{uid}/interactivity"
    watch = f"{base}/watch/work/{uid}"

    def configure(mode, prompt=None):
        page.goto(operator)
        settings = page.locator("#ratingEditor")
        menu = settings.get_by_label("Rating type")
        expect(menu).to_be_visible()
        menu.select_option(mode)
        if prompt:
            settings.get_by_label("Rating prompt").fill(prompt)
            settings.get_by_label("Show rating at").fill("00:00:01")
        settings.get_by_role("button", name="Save rating").click()
        expect(page.locator("#ratingStatus")).to_have_text("Rating saved.")

    configure("thumbs", "Was the meeting useful?")
    guest = browser.new_context()
    try:
        view = guest.new_page()

        def vote(label, prompt):
            view.goto(watch)
            widget = view.locator(".video-rating")
            expect(widget).to_be_hidden()
            assert view.locator("video").evaluate("""async v => {
                v.muted = true; await v.play(); v.pause(); v.currentTime = 1.25;
                return v.videoWidth;
            }""") == 256
            expect(widget.get_by_text(prompt, exact=True)).to_be_visible()
            button = widget.get_by_role("button", name=label, exact=True)
            button.click()
            expect(widget.locator("[data-rating-message]")).to_have_text("Rating recorded.")
            expect(button).to_be_disabled()
            return base + widget.get_attribute("data-rate")

        first = vote("Thumbs up", "Was the meeting useful?")
        dup = requests.post(first, json={"score": 0}, timeout=10)
        dup.raise_for_status()
        assert dup.json() == {"recorded": False}
        configure("stars", "How useful was the meeting?")
        assert requests.post(first, json={"score": 1}, timeout=10).status_code == 404
        second = vote("4 stars", "How useful was the meeting?")

        page.reload()
        expect(page.get_by_text("Thumbs up 1, thumbs down 0", exact=True)).to_be_visible()
        expect(page.get_by_text("4.00 out of 5 from 1 rating", exact=True)).to_be_visible()
        assert requests.get(path + "/rating/export", timeout=10).status_code == 403
        report = requests.get(path + "/rating/export", headers=auth, timeout=10)
        report.raise_for_status()
        rows = list(csv.DictReader(StringIO(report.text)))
        assert {(row["rating_type"], row["score"], row["responses"]) for row in rows} == {
            ("stars", "4", "1"), ("thumbs", "1", "1"),
        }

        configure("none")
        view.reload()
        expect(view.locator(".video-rating")).to_have_count(0)
        assert requests.post(second, json={"score": 4}, timeout=10).status_code == 404
        expect(page.get_by_text("4.00 out of 5 from 1 rating", exact=True)).to_be_visible()
    finally:
        guest.close()
    return dict(real_player_trigger=True, requested_ratings_recorded={"thumbs_up":1, "four_stars":1},
                duplicate_session_counted_once=True, earlier_votes_retained_when_off=True,
                authenticated_cut_scoped_csv=True)
