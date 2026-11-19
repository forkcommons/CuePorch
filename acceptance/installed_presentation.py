"""Play cards and end screens in the real installed archive player."""

from base64 import b64decode
import json
import uuid

import requests
from playwright.sync_api import expect


def check_presentation(page, browser, base, auth, museum, venue, replacement):
    works = requests.get(base + "/api/library", headers=auth, timeout=10).json()
    uid = next(work["id"] for work in works if work["title"] == "Chapters fixture")
    work_api = f"{base}/api/library/work/{uid}"
    endpoint = work_api + "/presentation"

    def settings():
        response = requests.get(endpoint, headers=auth, timeout=10)
        response.raise_for_status()
        return response.json()

    def configure(end, code=200):
        current = settings()
        response = requests.put(endpoint, headers=auth, timeout=10, json=dict(
            version=current["version"], previous=current["settings"],
            cards=current["settings"]["cards"], ending=end,
        ))
        assert response.status_code == code, response.text
        return response

    def finish(locator):
        width = locator.evaluate("""async v => {
            if (v.readyState < 1) await new Promise(resolve => v.addEventListener('loadedmetadata', resolve, {once:true}));
            v.muted = true;
            v.currentTime = v.duration - .18;
            await v.play();
            return v.videoWidth;
        }""")
        assert width == 320

    assert requests.get(endpoint, timeout=10).status_code == 403
    member_name = "Public gallery recommendation"
    with replacement.open("rb") as stream:
        added = requests.post(
            base + "/api/library/uploads", headers=auth, timeout=25,
            files={"file": (member_name + ".mp4", stream, "video/mp4")},
            data={"upload_id": str(uuid.uuid4()), "privacy": json.dumps({
                "visibility": "public", "embed": "disabled", "domains": [], "download": False,
            })},
        )
    added.raise_for_status()
    member = added.json()["id"]
    page.goto(f"{base}/operator/videos/{uid}/interactivity")
    editor = page.locator("#presentationEditor")
    expect(editor).to_be_visible()
    editor.get_by_role("button", name="Add card", exact=True).click()
    editor.get_by_label("Start time").fill("00:00:01")
    editor.get_by_label("Card title").fill("Meeting agenda")
    editor.get_by_label("Card description").fill("See what’s on the agenda.")
    editor.get_by_label("Card link").fill("https://example.com/agenda")
    image = b64decode(page.evaluate("""() => {
        const image = document.createElement('canvas');
        image.width = 40; image.height = 20;
        image.getContext('2d').fillRect(0, 0, 40, 20);
        return image.toDataURL('image/png').split(',')[1];
    }"""))
    editor.get_by_label("Card image").set_input_files({"name": "meeting.png", "mimeType": "image/png", "buffer": image})
    editor.get_by_label("After playback").select_option("action")
    editor.get_by_label("End title").fill("Continue with the minutes")
    editor.get_by_label("Button label").fill("Read the minutes")
    editor.get_by_label("End link").fill("https://example.com/minutes")
    editor.get_by_role("button", name="Save cards and end screen").click()
    expect(page.locator("#presentationStatus")).to_have_text("Cards and end screen saved.")
    data = settings()["settings"]
    digest = data["cards"][0]["image"]
    assert digest and data["cards"][0]["start"] == 1
    thumbnail = requests.post(
        work_api + "/thumbnail", headers=auth, timeout=25,
        files={"image": ("selected-still.png", image, "image/png")},
    )
    thumbnail.raise_for_status()
    configure({"mode": "action", "label": "Invalid", "link": "javascript:invalid"}, code=422)
    assert settings()["settings"] == data

    guest = browser.new_context()
    previous = museum.embed
    try:
        view = guest.new_page()
        museum.embed = base + "/embed/work/" + uid
        view.goto(venue)
        frame = view.frame_locator("iframe")
        card = frame.get_by_role("complementary", name="Meeting agenda")
        video = frame.locator("video")
        expect(card).to_be_hidden()
        assert video.evaluate("async v => {v.muted=true;await v.play();v.currentTime=1.2;return v.videoWidth}") == 320
        expect(card).to_be_visible()
        expect(card.get_by_role("link", name="Open link")).to_have_attribute("href", "https://example.com/agenda")
        artwork = card.locator("img")
        assert artwork.evaluate("async image => {await image.decode();return image.naturalWidth}") == 40
        artwork_url = base + artwork.get_attribute("src")
        card.get_by_label("Dismiss card").click()
        expect(card).to_be_hidden()
        finish(video)
        ending = frame.get_by_role("region", name="End screen")
        expect(ending).to_be_visible()
        expect(ending.get_by_role("link", name="Read the minutes")).to_have_attribute("href", "https://example.com/minutes")
        ending.get_by_role("button", name="Replay").click()
        expect(ending).to_be_hidden()
        assert video.evaluate("v => !v.paused")

        configure({"mode": "more", "videos": [member]})
        watch = base + "/watch/work/" + uid
        view.goto(watch)
        video = view.locator("video")
        finish(video)
        ending = view.get_by_role("region", name="End screen")
        recommendation = ending.get_by_role("link", name=member_name)
        expect(recommendation).to_have_attribute("href", "/watch/work/" + member)
        next_video = guest.new_page()
        response = next_video.goto(base + recommendation.get_attribute("href"))
        assert response.ok
        assert next_video.locator("video").evaluate("async v => {v.muted=true;await v.play();return v.videoWidth}") == 256
        next_video.close()
        requests.put(f"{base}/api/library/work/{member}/privacy", headers=auth, timeout=10, json={
            "visibility": "private", "embed": "disabled", "domains": [], "download": False,
        }).raise_for_status()
        view.reload()
        finish(video)
        expect(ending).to_be_visible()
        expect(ending.get_by_text(member_name)).to_have_count(0)

        for mode, options in (
            ("message", {"text": "Thank you for watching."}),
            ("image", {"image": digest}),
            ("share", {}),
            ("empty", {}),
            ("thumbnail", {}),
        ):
            configure({"mode": mode} | options)
            view.reload()
            finish(video)
            expect(ending).to_have_attribute("data-ending", mode)
            expect(ending).to_be_visible()
            if mode == "message":
                expect(ending.get_by_text(options["text"])).to_be_visible()
            if mode == "image":
                assert ending.get_by_role("img", name="End screen image").evaluate(
                    "async image => {await image.decode();return image.naturalWidth}"
                ) == 40
            if mode == "share":
                expect(ending.get_by_label("Video link")).to_have_value(watch)
            if mode == "thumbnail":
                assert video.evaluate("v => v.currentTime") < .1
                poster_url = base + video.get_attribute("poster")
                response = requests.get(poster_url, timeout=10)
                assert response.ok and response.headers["Content-Type"] == "image/jpeg", (
                    response.status_code, response.headers.get("Content-Type"),
                )
                assert video.evaluate("""async v => {
                    const poster = new Image();
                    poster.src = v.poster;
                    await poster.decode();
                    return poster.naturalWidth;
                }""") == 40
        configure({"mode": "loop"})
        view.reload()
        finish(video)
        assert video.evaluate("v => v.loop")
        assert view.get_by_role("region", name="End screen").count() == 0

        requests.put(work_api + "/privacy", headers=auth, timeout=10, json={
            "visibility": "private", "embed": "disabled", "domains": [], "download": False,
        }).raise_for_status()
        assert requests.get(artwork_url, timeout=10).status_code == 404
        assert requests.get(poster_url, timeout=10).status_code == 404
    finally:
        museum.embed = previous
        guest.close()
    return dict(timed_card_and_decoded_artwork=True, replay=True, end_screen_modes=8,
                excludes_newly_private_recommendations=True, rejects_unsafe_card_link=True,
                revoked_viewer_token_cannot_retrieve_artwork=True)
