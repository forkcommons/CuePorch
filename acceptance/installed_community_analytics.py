"""Verify audience actions, moderation and reporting through the installed service."""

import csv
import json
import sqlite3
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from io import StringIO

import requests
from playwright.sync_api import expect

from installed_audience_accounts import MUTATION


def check_community_analytics(operator, browser, base, staff, sample, database):
    public = dict(visibility="public", embed="anywhere")
    password = "Community analytics fixture!"
    now = datetime.now(timezone.utc)
    today, yesterday = [value.date().isoformat() for value in (now, now - timedelta(days=1))]
    title = "Community report screening"

    def upload(name):
        with sample.open("rb") as media:
            reply = requests.post(base + "/api/library/uploads", headers=staff, timeout=30,
                                  files={"file": (name + ".mp4", media, "video/mp4")},
                                  data=dict(upload_id=str(uuid.uuid4()), privacy=json.dumps(public)))
        reply.raise_for_status()
        return reply.json()["id"]

    first, second = upload(title), upload("Elsewhere screening")
    likes = base + "/api/viewer/likes/work/"
    comments = base + "/api/viewer/comments/work/" + first
    watch = base + "/watch/work/" + first
    policy = base + "/api/policy/public_comments"
    filters = {"from": today, "to": today, "videos": [first]}

    def report(name="video", **options):
        reply = requests.post(f"{base}/api/analytics/{name}-report", headers=staff, timeout=10,
                              json=filters | options)
        reply.raise_for_status()
        return reply.json()

    def counts(report_row):
        return report_row["likes"], report_row["comments"]

    def required(enabled):
        requests.put(policy, headers=staff, json={"required": enabled}, timeout=10).raise_for_status()

    with ExitStack() as owned:
        def viewer(name):
            ctx = browser.new_context()
            owned.callback(ctx.close)
            reply = ctx.request.post(base + "/api/viewer/signup", headers=MUTATION, data={
                "name": name, "email": name + "@analytics.example.invalid", "password": password,
            })
            assert reply.ok, reply.status
            return ctx

        def post(ctx, name, state, headers=None):
            reply = ctx.request.post(comments, headers=MUTATION | (headers or {}), data={"body": name})
            assert reply.status == 201 and reply.json()["status"] == state

        a, b = viewer("Artist"), viewer("Subscriber")
        for uid in (first, first, second):
            assert a.request.put(likes + uid, headers=MUTATION, data={}).ok
        opted_out = b.request.put(likes + first, headers=MUTATION | {"DNT": "1"}, data={})
        assert opted_out.json()["likes"] == 2
        post(a, "Automatically published", "eligible")
        post(b, "Published without tracking", "eligible", {"Sec-GPC": "1"})
        assert len(b.request.get(comments).json()["comments"]) == 2
        required(True)
        for message in ("Approved on publication day", "Released by policy switch", "Rejected comment"):
            post(a, message, "waiting")

        # A prior-day submission must be reported on the day the operator releases it.
        with sqlite3.connect(database, timeout=30) as connection:
            updated = connection.execute("UPDATE audience_comment SET created=? WHERE body=?",
                                         ((now - timedelta(days=1)).timestamp(), "Approved on publication day"))
            assert updated.rowcount == 1
        pending = report()
        assert counts(pending["totals"]) == (1, 1)
        assert pending["totals"]["views"] == pending["totals"]["impressions"] == 0
        actions = requests.get(base + "/api/actions", headers=staff, timeout=10).json()
        by_body = {action["comment_body"]: action["id"] for action in actions if "comment_body" in action}
        for body, decision in (("Approved on publication day", "approve"), ("Rejected comment", "reject")):
            requests.post(f"{base}/api/actions/{by_body[body]}/decision", headers=staff,
                          json={"decision": decision}, timeout=10).raise_for_status()
        assert counts(report()["totals"]) == (1, 2)
        required(False)
        assert counts(report()["totals"]) == (1, 3)
        assert counts(report(videos=[first, second])["totals"]) == (2, 3)
        assert counts(report(**{"from": yesterday, "to": yesterday})["totals"]) == (0, 0)
        trend = report("date", period="month")
        assert len(trend["rows"]) == len(trend["overview"]) == 1
        assert counts(trend["rows"][0]) == counts(trend["overview"][0]) == (1, 3)
        assert counts(report(surface="embed")["totals"]) == (0, 0)
        assert counts(report(streaming="vod")["totals"]) == (1, 3)
        assert counts(report(streaming="live")["totals"]) == (0, 0)

        guest = browser.new_context()
        owned.callback(guest.close)
        page = guest.new_page()
        page.goto(watch)
        width = page.locator("video").evaluate("async v=>{v.muted=true;await v.play();return v.videoWidth}")
        assert width == 256
        deadline = time.monotonic() + 10
        while report()["totals"]["views"] != 1 and time.monotonic() < deadline:
            time.sleep(.15)
        assert report()["totals"]["views"] == 1
        for dimension in ("device", "source", "region", "caption"):
            breakdown = report(breakdown=dimension)
            assert counts(breakdown["totals"]) == (1, 3)
            assert len(breakdown["rows"]) == 1 and counts(breakdown["rows"][0]) == (None, None)
        for dimension, value in (("device", "Desktop"), ("source", "Unknown"), ("region", "Unknown"),
                                 ("caption_kind", "unknown")):
            assert counts(report(**{dimension: value})["totals"]) == (None, None)
        device = report("device")
        assert device["rows"][0]["category"] == "Desktop" and counts(device["rows"][0]) == (None, None)
        assert device["rows"][0]["views"] == 1

        operator.goto(f"{base}/operator/analytics?video={first}")
        expect(operator.locator("#reportRows")).to_contain_text(title)
        for metric, value in (("likes", "1"), ("comments", "3")):
            operator.get_by_role("combobox", name="Metric", exact=True).select_option(metric)
            expect(operator.locator("#reportTrend td").last).to_have_text(value)
        with operator.expect_download() as downloaded:
            operator.get_by_role("button", name="Download CSV").click()
        rows = list(csv.DictReader(StringIO(downloaded.value.path().read_text(encoding="utf8"))))
        assert [(r["video_id"], r["likes"], r["comments"], r["views"]) for r in rows] == [(first, "1", "3", "1")]
        operator.get_by_label("Report type").select_option("device")
        expect(operator.locator("#communityBreakdownNote")).to_be_visible()
        expect(operator.locator("#reportRows")).to_contain_text("Desktop")
        with operator.expect_download() as downloaded:
            operator.get_by_role("button", name="Download CSV").click()
        rows = list(csv.DictReader(StringIO(downloaded.value.path().read_text(encoding="utf8"))))
        assert [(r["device"], r["likes"], r["comments"], r["views"]) for r in rows] == [("Desktop", "", "", "1")]

        def privacy(settings):
            requests.put(f"{base}/api/library/work/{first}/privacy", headers=staff,
                         json=settings, timeout=10).raise_for_status()

        privacy(dict(visibility="private", embed="disabled"))
        assert guest.request.get(comments).status == 404
        assert counts(report()["totals"]) == (1, 3)
        privacy(public)
        assert a.request.delete(likes + first, headers=MUTATION, data={}).ok
        assert counts(report()["totals"]) == (0, 3)
        assert a.request.put(likes + first, headers=MUTATION, data={}).ok
        published = guest.request.get(comments).json()["comments"]
        uid = next(c["id"] for c in published if c["body"] == "Automatically published")
        requests.delete(f"{base}/api/operator/public-comments/{uid}", headers=staff, timeout=10).raise_for_status()
        assert counts(report()["totals"]) == (1, 2)
        assert a.request.delete(base + "/api/viewer/account", headers=MUTATION, data={"password": password}).ok
        assert counts(report(videos=[first, second])["totals"]) == (0, 0)
        assert guest.request.get(likes + first).json()["likes"] == 1
        assert len(guest.request.get(comments).json()["comments"]) == 1
        return dict(played_width=width, views=1, observed_likes=1, published_comments=3,
                    approved_on_publication_date=True, pending_and_rejected_excluded=True,
                    browser_opt_outs_honored=True, no_views_inferred_from_actions=True,
                    video_date_chart_and_csv=True, unsupported_breakdown_blanks=True,
                    private_video_history_operator_only=True, removal_and_account_cleanup=True)
