"""Verify Like, Collections, owner scoping and playback in the installed app."""

import json
import uuid
from contextlib import ExitStack
from urllib.parse import urlsplit

from playwright.sync_api import expect
import requests

from installed_audience_accounts import MUTATION


def check_audience_collections(browser, base, staff, sample):
    public = dict(visibility="public", embed="anywhere")
    password = "Collections playback fixture!"

    def upload(title):
        with sample.open("rb") as media:
            response = requests.post(base + "/api/library/uploads", headers=staff, timeout=30,
                                     files={"file": (title + ".mp4", media, "video/mp4")},
                                     data={"upload_id": str(uuid.uuid4()), "privacy": json.dumps(public)})
        response.raise_for_status()
        return response.json()["id"]

    def watch(uid):
        return f"{base}/watch/work/{uid}"

    artist, discussion = "Paper Animation", "Animation Discussion"
    first, second = upload(artist), upload(discussion)
    like_path = f"{base}/api/viewer/likes/work/{first}"
    entries = base + "/api/viewer/collections"
    collection_title = "Weekend in the studio"
    privacy = f"{base}/api/library/work/{first}/privacy"

    def signup(page, name):
        form = page.locator("#viewerSignUp")
        form.get_by_label("Display name").fill(name)
        form.get_by_label("Email").fill(name.lower() + "@collections.example.invalid")
        form.get_by_label("Password").fill(password)
        form.get_by_role("button", name="Create account").click()

    with ExitStack() as owned:
        def browser_session():
            ctx = browser.new_context()
            owned.callback(ctx.close)
            return ctx, ctx.new_page()

        a, viewer = browser_session()
        b, outsider = browser_session()
        viewer.goto(watch(first))
        like = viewer.get_by_role("button", name="Like video")
        expect(like).to_have_attribute("aria-pressed", "false")
        expect(viewer.locator("[data-like-count]")).to_have_text("0 likes")
        like.click()
        expect(viewer).to_have_url(base + "/account?next=%2Fwatch%2Fwork%2F" + first)
        signup(viewer, "Artist")
        expect(viewer).to_have_url(watch(first))
        like.click()
        expect(like).to_have_attribute("aria-pressed", "true")
        expect(viewer.locator("[data-like-count]")).to_have_text("1 like")
        assert a.request.put(like_path, headers=MUTATION, data={}).json() == {"liked": True, "likes": 1}

        outsider.goto(base + "/account")
        signup(outsider, "Neighbor")
        expect(outsider.locator("#viewerDetails").get_by_label("Display name")).to_have_value("Neighbor")
        assert b.request.get(like_path).json() == {"liked": False, "likes": 1}
        assert b.request.put(like_path, headers=MUTATION, data={}).json() == {"liked": True, "likes": 2}
        assert requests.get(like_path, timeout=15).json() == {"liked": False, "likes": 2}

        viewer.get_by_role("button", name="Add to collection").click()
        dialog = viewer.get_by_role("dialog", name="Add to collection")
        dialog.get_by_label("New collection name").fill(collection_title)
        dialog.get_by_role("button", name="Create collection").click()
        expect(dialog.get_by_role("status")).to_have_text("Collection created.")
        dialog.get_by_role("button", name="Add video", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("Video added to collection.")
        expect(dialog.get_by_role("button", name="Remove video", exact=True)).to_be_visible()
        dialog.get_by_role("button", name="Close", exact=True).click()

        viewer.goto(watch(second))
        viewer.get_by_role("button", name="Add to collection").click()
        collections = a.request.get(entries).json()
        assert len(collections) == 1 and collections[0]["title"] == collection_title
        expect(dialog.locator("[data-collect-options]")).to_have_value(collections[0]["id"])
        dialog.get_by_role("button", name="Add video", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("Video added to collection.")
        viewer.goto(base + "/watch")
        viewer.get_by_role("link", name="Collections", exact=True).click()
        viewer.get_by_role("link", name=collection_title).click()
        collection = viewer.url
        uid = urlsplit(collection).path.rsplit("/", 1)[-1]
        assert uid == collections[0]["id"]
        assert b.request.get(entries).json() == []
        assert b.request.get(collection).status == 404
        assert b.request.delete(f"{entries}/{uid}/videos/work/{first}", headers=MUTATION, data={}).status == 404
        assert requests.get(collection, timeout=15).status_code == 404
        assert "no-store" in a.request.get(collection).headers["cache-control"]

        viewer.get_by_role("link", name=artist).click()
        assert viewer.url == watch(first)
        width = viewer.locator("video").evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth;}")
        assert width == 256
        details_url = f"{base}/api/library/work/{first}/details"
        original = requests.get(details_url, headers=staff, timeout=15).json()
        assert original["allow_viewer_collections"] is True
        requests.put(details_url, headers=staff, timeout=15,
                     json=original | {"allow_viewer_collections": False}).raise_for_status()
        legacy = {key: value for key, value in original.items() if key != "allow_viewer_collections"}
        saved = requests.put(details_url, headers=staff, timeout=15, json=legacy)
        saved.raise_for_status()
        assert saved.json()["allow_viewer_collections"] is False
        viewer.get_by_role("button", name="Add to collection").click()
        dialog.get_by_role("button", name="Remove video", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text("Video removed from collection.")
        dialog.get_by_role("button", name="Add video", exact=True).click()
        expect(dialog.get_by_role("status")).to_have_text(
            "The publisher has disabled adding this video to collections.")
        assert a.request.get(watch(first)).ok
        dialog.get_by_role("button", name="Close", exact=True).click()
        requests.put(details_url, headers=staff, timeout=15, json=original).raise_for_status()
        assert a.request.put(f"{entries}/{uid}/videos/work/{first}", headers=MUTATION, data={}).ok

        requests.put(privacy, headers=staff, timeout=15, json=dict(visibility="private", embed="disabled")).raise_for_status()
        viewer.goto(collection)
        expect(viewer.get_by_role("link", name=artist)).to_have_count(0)
        expect(viewer.get_by_role("link", name=discussion)).to_be_visible()
        assert a.request.get(like_path).status == a.request.get(watch(first)).status == 404
        assert a.request.put(f"{entries}/{uid}/videos/work/{first}", headers=MUTATION, data={}).status == 404
        requests.put(privacy, headers=staff, timeout=15, json=public).raise_for_status()
        viewer.reload()
        expect(viewer.get_by_role("link", name=artist)).to_be_visible()

        viewer.get_by_text("Edit collection", exact=True).click()
        viewer.get_by_label("Name", exact=True).fill("Studio screening")
        viewer.get_by_label("Description", exact=True).fill("Two works for Saturday.")
        viewer.get_by_role("button", name="Save collection").click()
        expect(viewer.get_by_role("heading", name="Studio screening")).to_be_visible()
        viewer.get_by_role("button", name=f"Remove {discussion} from collection").click()
        expect(viewer.get_by_role("link", name=discussion)).to_have_count(0)
        viewer.get_by_text("Delete collection", exact=True).click()
        viewer.get_by_role("button", name="Delete this collection").click()
        expect(viewer).to_have_url(base + "/watch/collections")
        assert a.request.get(collection).status == 404 and a.request.get(watch(second)).ok

        viewer.get_by_role("link", name="Liked videos").click()
        expect(viewer.get_by_role("link", name=artist)).to_be_visible()
        viewer.get_by_role("button", name=f"Unlike {artist}").click()
        expect(viewer.get_by_text("You haven’t liked any videos.")).to_be_visible()
        assert b.request.get(like_path).json() == {"liked": True, "likes": 1}
        deleted = b.request.delete(base + "/api/viewer/account", headers=MUTATION, data={"password": password})
        assert deleted.status == 204
        assert a.request.get(like_path).json() == {"liked": False, "likes": 0}
        return dict(played_collection_video_width=width, repeat_like_idempotent=True,
                    anonymous_like_count_only=True, private_collection_owner_scope=True,
                    like_and_membership_hidden_during_privacy=True, item_restored_when_public=True,
                    create_add_rename_remove_delete=True, unlike_and_account_cleanup=True)
