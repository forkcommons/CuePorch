"""Exercise showcase catalog, protected access and decoded playlist playback."""

import base64
import csv
from io import StringIO
import json
from urllib.parse import urlsplit
import uuid

import requests
from playwright.sync_api import expect


def check_transferred_showcase(page, base, auth, works):
    transfer = requests.get(base + "/api/migration", headers=auth, timeout=10)
    transfer.raise_for_status()
    record = transfer.json()["showcases"][0]
    assert record["source_id"] == "805" and record["source_visibility"] == "password"
    assert record["missing"] == ["999"] and not record["issues"]
    expect(page.get_by_role("link", name="Open showcase")).to_have_attribute(
        "href", "/operator/showcases?select=" + record["target"],
    )
    listing = requests.get(base + "/api/showcases", headers=auth, timeout=10)
    listing.raise_for_status()
    imported = [item for item in listing.json() if item["settings"]["title"] == "Station and Gallery"]
    assert len(imported) == 1 and imported[0]["id"] == record["target"]
    showcase = imported[0]
    assert showcase["settings"]["visibility"] == "private" and showcase["watch"] is None and showcase["embed"] is None
    source = {str(work["source"]).split("/")[-1]: work["id"] for work in works}
    assert showcase["settings"]["videos"] == [source["72"], source["71"]]
    assert requests.get(base + "/showcases/" + showcase["slug"], timeout=10).status_code == 404
    return dict(vimeo_album_discovered=True, paginated_members_in_order=True, missing_source_reported=True,
                idempotent_private_transfer=True)


def check_showcases(page, browser, base, auth, museum, venue, clip):
    allowed_site = urlsplit(venue).netloc

    def upload(title, visibility, embed="disabled", domains=None):
        with clip.open("rb") as stream:
            response = requests.post(base + "/api/library/uploads", headers=auth, timeout=30,
                files={"file": (title + ".mp4", stream, "video/mp4")},
                data=dict(upload_id=str(uuid.uuid4()), privacy=json.dumps(dict(
                    visibility=visibility, embed=embed, domains=domains or [], download=False,
                ))))
        response.raise_for_status()
        return response.json()["id"]

    first = upload("Museum Opening", "public", "anywhere")
    second = upload("Theater Encore", "public", "domains", [allowed_site])
    upload("Private Collection Work", "private")
    list_api = base + "/api/showcases"
    initial = requests.get(list_api, headers=auth, timeout=10).json()
    previous_frame = museum.embed
    guest = browser.new_context()
    try:
        page.goto(base + "/operator/showcases")
        page.get_by_role("button", name="New showcase").click()
        expect(page.get_by_role("status").first).to_have_text("Showcase created.")
        listing = requests.get(list_api, headers=auth, timeout=10).json()
        new = next(row for row in listing if row["id"] not in {item["id"] for item in initial})
        endpoint = list_api + "/" + new["id"]
        assert new["settings"]["title"] == "Untitled showcase" and new["watch"] is None
        form = page.locator("#showcaseEdit")
        playlist = form.locator("#showcaseMembers")
        for title in ("Museum Opening", "Theater Encore", "Private Collection Work"):
            form.get_by_label("Add a video").select_option(label=title)
            form.get_by_role("button", name="Add to showcase").click()
        playlist.get_by_role("button", name="Move up Theater Encore").click()
        form.get_by_label("Title", exact=True).fill("November Art Screenings")
        form.get_by_label("Description", exact=True).fill("A season of screenings")
        form.get_by_label("Link name", exact=True).fill("november-art-screenings")
        form.get_by_label("Accent color").fill("#369ba4")
        form.get_by_label("Dark background").check()
        form.get_by_label("Layout", exact=True).select_option("featured")
        form.get_by_label("Featured title").fill("From the collection")
        form.get_by_label("Featured link text").fill("Festival program")
        form.get_by_label("Featured link URL").fill("https://example.test/program")
        form.get_by_label("Play the next video").check()
        form.get_by_label("Loop the playlist").check()
        form.get_by_label("Search title").fill("Browse November screenings")
        form.get_by_label("Search description").fill("Moving image programs")
        form.get_by_label("Allow search engines").check()
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPw4AAAAASUVORK5CYII=")
        form.get_by_label("Logo image").set_input_files(dict(name="gallery.png", mimeType="image/png", buffer=png))
        form.get_by_role("button", name="Save showcase").click()
        expect(page.get_by_role("status").first).to_have_text("Showcase saved.")
        page.get_by_label("Preview width").select_option("mobile")
        page.get_by_role("button", name="Preview saved showcase").click()
        expect(page.locator("#showcaseFrame")).to_have_attribute("data-width", "mobile")
        preview = page.frame_locator("#showcaseFrame")
        expect(preview.get_by_role("button", name="Private Collection Work")).to_be_visible()
        assert preview.get_by_role("img", name="Publisher logo").evaluate("image => image.decode().then(() => image.naturalWidth)") == 1
        assert requests.get(base + "/showcases/november-art-screenings", timeout=10).status_code == 404

        form.get_by_label("Visibility").select_option("public")
        form.get_by_label("Allowed embed sites").fill(allowed_site)
        form.get_by_role("button", name="Save showcase").click()
        expect(page.get_by_label("Share link")).to_be_visible()
        actual = requests.get(endpoint, headers=auth, timeout=10).json()
        watch = base + actual["watch"]
        embed = base + actual["embed"]
        assert watch == page.get_by_label("Share link").input_value()
        assert embed in page.get_by_label("Embed code").input_value()
        assert requests.put(endpoint, headers=auth, json=new, timeout=10).status_code == 409

        viewer = guest.new_page()
        response = viewer.goto(watch)
        assert response.status == 200 and "x-robots-tag" not in response.headers
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "Browse November screenings" in viewer.title()
        expect(viewer.locator('meta[name="description"]')).to_have_attribute("content", "Moving image programs")
        expect(viewer.get_by_role("link", name="Festival program")).to_have_attribute("href", "https://example.test/program")
        expect(viewer.get_by_role("button", name="Private Collection Work")).to_have_count(0)
        expect(viewer.locator("#nowPlaying")).to_have_text("Theater Encore")
        viewer.get_by_label("Search videos").fill("museum")
        expect(viewer.get_by_role("button", name="Theater Encore")).not_to_be_visible()
        viewer.get_by_label("Search videos").fill("")
        video = viewer.locator("video")
        clip_width = video.evaluate("async v => {v.muted=true;await v.play();v.currentTime=v.duration-.12;return v.videoWidth}")
        assert clip_width == 256
        expect(viewer.locator("#nowPlaying")).to_have_text("Museum Opening")
        auto_media = video.get_attribute("src")
        fetched = requests.get(base + auto_media, headers={"Range": "bytes=0-127"}, timeout=10)
        assert fetched.status_code == 206 and b"ftyp" in fetched.content

        museum.embed = embed
        viewer.goto(venue)
        frame = viewer.frame_locator("iframe")
        expect(frame.get_by_role("heading", name="Theater Encore")).to_be_visible()
        assert frame.locator("video").evaluate("async v=>{v.muted=true;await v.play();v.pause();return v.videoWidth}") == 256
        assert requests.get(embed, headers={"Referer": "http://localhost:2/"}, timeout=10).status_code == 403
        change = requests.put(f"{base}/api/library/work/{first}/privacy", headers=auth, timeout=10,
                              json=dict(visibility="private", embed="disabled", domains=[], download=False))
        change.raise_for_status()
        assert requests.get(base + auto_media, timeout=10).status_code == 404
        viewer.reload()
        expect(frame.get_by_role("button", name="Museum Opening")).to_have_count(0)

        form.get_by_label("Visibility").select_option("password")
        password = "screening-test-access"
        form.get_by_label("Showcase password").fill(password)
        form.get_by_role("button", name="Save showcase").click()
        expect(page.get_by_role("status").first).to_have_text("Showcase saved.")
        page.get_by_role("button", name="Registration", exact=True).click()
        registration = page.locator("#registrationEditor")
        expect(registration).to_be_visible()
        expect(registration.get_by_label("Require registration before playback")).not_to_be_checked()
        registration.get_by_label("Require registration before playback").check()
        registration.get_by_label("Form heading").fill("Register for the art program")
        registration.get_by_label("Save UTM campaign fields").check()
        registration.get_by_role("button", name="Save registration").click()
        expect(registration.locator("#registrationStatus")).to_have_text("Registration saved.")
        museum.embed = embed + "?utm_source=festival"
        viewer.reload()
        expect(frame.locator("video")).to_have_count(0)
        frame.get_by_label("Showcase password").fill(password)
        frame.get_by_role("button", name="Unlock showcase").click()
        expect(frame.get_by_role("heading", name="Register for the art program")).to_be_visible()
        assert password not in frame.locator("body").inner_html()
        expect(frame.locator("video")).to_have_count(0)
        frame.get_by_label("Email", exact=True).fill("festival-viewer@example.test")
        frame.get_by_role("button", name="Register and watch").click()
        video = frame.locator("video")
        assert video.evaluate("async v=>{v.muted=true;await v.play();v.pause();return v.videoWidth}") == 256
        registered_media = base + video.get_attribute("src")
        export = endpoint + "/registration/export"
        assert requests.get(export, timeout=10).status_code == 403
        spreadsheet = list(csv.DictReader(StringIO(requests.get(export, headers=auth, timeout=10).text)))
        assert len(spreadsheet) == 1 and spreadsheet[0]["email"] == "festival-viewer@example.test"
        assert spreadsheet[0]["utm_source"] == "festival"
        registration.get_by_label("Require registration before playback").uncheck()
        registration.get_by_role("button", name="Save registration").click()
        expect(registration.locator("#registrationStatus")).to_have_text("Registration saved.")
        assert requests.get(registered_media, timeout=10).status_code == 404

        form.get_by_label("Visibility").select_option("embed_only")
        form.get_by_role("button", name="Save showcase").click()
        expect(page.get_by_label("Share link")).not_to_be_visible()
        assert requests.get(watch, timeout=10).status_code == 404
        viewer.reload()
        expect(frame.get_by_role("heading", name="Theater Encore")).to_be_visible()
        page.get_by_role("button", name="Make a copy").click()
        expect(page.get_by_role("status").first).to_have_text("A private copy was created.")
        copies = requests.get(list_api, headers=auth, timeout=10).json()
        copy = next(row for row in copies if row["settings"]["title"].endswith("(copy)"))
        assert copy["settings"]["visibility"] == "private" and copy["settings"]["videos"][0] == second
        page.get_by_role("button", name="Delete showcase").click()
        expect(page.get_by_role("status").first).to_have_text("Showcase deleted. Its videos remain in the library.")
        assert requests.get(list_api + "/" + copy["id"], headers=auth, timeout=10).status_code == 404
        assert requests.get(f"{base}/api/library/work/{second}", headers=auth, timeout=10).status_code == 200
    finally:
        museum.embed = previous_frame
        guest.close()
    return dict(private_default=True, desktop_mobile_preview=True, branding_and_search_metadata=True,
                ordered_playlist_and_auto_advance=True, iframe_decoded_256=clip_width,
                embeds_respect_both_allowlists=True, old_video_ticket_revoked=True,
                password_and_registration_with_csv=True, embed_only_direct_link_denied=True,
                private_copy_and_delete_preserves_videos=True)
