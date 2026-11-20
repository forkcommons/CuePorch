"""Exercise public discussion, privacy and operator approval in the installed app."""

import json
import uuid
from contextlib import ExitStack

from playwright.sync_api import expect
import requests

from installed_audience_accounts import MUTATION


def check_audience_comments(browser, base, staff, sample):
    password = "Comments playback fixture!"
    public = dict(visibility="public", embed="anywhere")
    with sample.open("rb") as media:
        uploaded = requests.post(base + "/api/library/uploads", headers=staff, timeout=30,
                                 files={"file": ("Public screening.mp4", media, "video/mp4")},
                                 data={"upload_id": str(uuid.uuid4()), "privacy": json.dumps(public)})
    uploaded.raise_for_status()
    uid = uploaded.json()["id"]
    watch = f"{base}/watch/work/{uid}"
    api = f"{base}/api/viewer/comments/work/{uid}"
    manage = base + "/api/operator/public-comments"
    privacy = f"{base}/api/library/work/{uid}/privacy"

    def signup(page, name):
        form = page.locator("#viewerSignUp")
        form.get_by_label("Display name").fill(name)
        form.get_by_label("Email").fill(name.lower() + "@discussion.example.invalid")
        form.get_by_label("Password").fill(password)
        form.get_by_role("button", name="Create account").click()

    def post(page, text, message):
        field = page.get_by_label("Write a public comment")
        field.fill(text)
        page.get_by_role("button", name="Post comment").click()
        expect(page.locator("[data-comment-status]")).to_have_text(message)
        expect(field).to_have_value("")
        expect(field).to_be_enabled()

    def search(page, text, sort="newest"):
        page.get_by_label("Search comments").fill(text)
        page.get_by_label("Sort comments").select_option(sort)
        page.get_by_role("search", name="Video comments").get_by_role("button", name="Search").click()

    def published(page):
        return page.locator("[data-comment-list] .watch-comment-body")

    with ExitStack() as owned:
        def session():
            ctx = browser.new_context()
            owned.callback(ctx.close)
            return ctx, ctx.new_page()

        author, writer = session()
        other, reader = session()
        guest, visitor = session()
        _, operator = session()
        visitor.goto(watch)
        expect(visitor.get_by_role("link", name="Sign in to comment")).to_be_visible()
        width = visitor.locator("video").evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth;}")
        assert width == 256
        assert guest.request.post(api, headers=MUTATION, data={"body": "Signed out"}).status == 401
        assert guest.request.get(manage).status == 403

        writer.goto(watch)
        writer.get_by_role("link", name="Sign in to comment").click()
        signup(writer, "Artist")
        expect(writer).to_have_url(watch)
        first = "<b>Café screening</b>"
        post(writer, first, "Comment posted.")
        expect(published(writer)).to_have_text([first])
        assert writer.locator("[data-comment-list] b").count() == 0
        first_id = author.request.get(api).json()["comments"][0]["id"]
        assert author.request.post(api, data={"body": "Missing mutation marker"}).status == 403

        reader.goto(watch)
        reader.get_by_role("link", name="Sign in to comment").click()
        signup(reader, "Neighbor")
        post(reader, "Loved the animation", "Comment posted.")
        assert other.request.delete(f"{api}/{first_id}", headers=MUTATION, data={}).status == 404
        visitor.reload()
        expect(published(visitor)).to_have_text(["Loved the animation", first])
        search(visitor, "", "oldest")
        expect(published(visitor)).to_have_text([first, "Loved the animation"])
        search(visitor, "CAFÉ")
        expect(published(visitor)).to_have_text([first])
        search(visitor, "neighbor")
        expect(published(visitor)).to_have_text(["Loved the animation"])
        assert "@discussion" not in guest.request.get(api).text()

        operator.goto(base + "/operator")
        operator.get_by_label("Operator credential").fill(staff["Authorization"].removeprefix("Bearer "))
        load = operator.get_by_role("button", name="Load settings and review queue")
        load.click()
        gate = operator.get_by_label("Require approval for public comments")
        expect(gate).not_to_be_checked()
        gate.check()
        expect(operator.locator("#result")).to_have_text("Saved")
        held, rejected, automatic, withdrawn = ("Held for moderation", "Please decline this", "Release when off", "Withdraw from queue")
        for message in (held, rejected, automatic, withdrawn):
            post(writer, message, "Comment submitted for approval.")
        expect(writer.locator("[data-comment-own-list]").get_by_text("Awaiting approval")).to_have_count(4)
        assert not other.request.get(api).json()["own_unpublished"]
        assert [c["body"] for c in guest.request.get(api).json()["comments"]] == ["Loved the animation", first]
        writer.locator("[data-comment-own-list] li").filter(has_text=withdrawn).get_by_role("button", name="Delete comment").click()
        expect(writer.locator("[data-comment-status]")).to_have_text("Comment deleted.")
        load.click()
        queue = operator.locator("#queue")
        expect(queue.get_by_text(withdrawn, exact=False)).to_have_count(0)
        queue.locator("p").filter(has_text=held).get_by_role("button", name="approve").click()
        expect(queue.get_by_text(held, exact=False)).to_have_count(0)
        queue.locator("p").filter(has_text=rejected).get_by_role("button", name="reject").click()
        expect(queue.get_by_text(rejected, exact=False)).to_have_count(0)
        assert [c["body"] for c in guest.request.get(api).json()["comments"]] == [held, "Loved the animation", first]
        assert [c["status"] for c in author.request.get(api).json()["own_unpublished"]] == ["waiting", "rejected"]
        gate.uncheck()
        expect(operator.locator("#result")).to_have_text("Saved")
        expect(queue.get_by_text(automatic, exact=False)).to_have_count(0)
        assert [c["body"] for c in guest.request.get(api).json()["comments"]] == [automatic, held, "Loved the animation", first]

        moderation = operator.get_by_role("region", name="Public comments")
        moderation.get_by_label("Search published comments").fill(held)
        moderation.get_by_role("button", name="Search").click()
        expect(moderation.get_by_role("listitem")).to_have_count(1)
        moderation.get_by_role("button", name="Remove comment").click()
        expect(moderation.get_by_role("status")).to_have_text("Comment removed.")
        assert held not in guest.request.get(api).text()

        requests.put(privacy, headers=staff, timeout=15, json=dict(visibility="private", embed="disabled")).raise_for_status()
        assert guest.request.get(api).status == author.request.get(api).status == 404
        assert author.request.post(api, headers=MUTATION, data={"body": "Private video"}).status == 404
        requests.put(privacy, headers=staff, timeout=15, json=public).raise_for_status()
        assert guest.request.get(api).ok
        assert author.request.delete(base + "/api/viewer/account", headers=MUTATION, data={"password": password}).status == 204
        assert [c["body"] for c in guest.request.get(api).json()["comments"]] == ["Loved the animation"]
        return dict(playback_width=width, automatic_default=True, search_body_and_author=True, both_sort_orders=True,
                    pending_and_rejected_author_only=True, operator_can_approve_reject_and_remove=True,
                    off_releases_only_pending=True, withdrawn_removes_queue_entry=True, privacy_revokes_discussion=True,
                    ownership_and_account_cleanup=True, escaped_text=True)
