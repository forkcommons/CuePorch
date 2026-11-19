"""Verify local registration against real packaged protected playback."""

import csv
from io import StringIO
import json
from urllib.parse import urlsplit
import uuid

import requests
from playwright.sync_api import expect


def check_registration(page, browser, base, auth, museum, venue, video_file):
    password = "cueporch-pilot-password"
    with video_file.open("rb") as stream:
        upload = requests.post(
            base + "/api/library/uploads", headers=auth, timeout=30,
            files={"file": ("Registered screening.mp4", stream, "video/mp4")},
            data=dict(upload_id=str(uuid.uuid4()), privacy=json.dumps({
                "visibility": "password", "embed": "domains", "domains": [urlsplit(venue).netloc],
                "download": False, "password": password,
            })),
        )
    upload.raise_for_status()
    uid = upload.json()["id"]
    endpoint = f"{base}/api/library/work/{uid}/registration"
    settings = f"{base}/operator/videos/{uid}/interactivity"
    address = f"{base}/embed/work/{uid}?utm_source=festival&utm_campaign=november"
    guest = browser.new_context()
    previous = museum.embed

    def password_unlock(frame):
        frame.get_by_label("Video password").fill(password)
        frame.get_by_role("button", name="Watch video", exact=True).click()

    try:
        view = guest.new_page()
        museum.embed = address
        view.goto(venue)
        frame = view.frame_locator("iframe")
        password_unlock(frame)
        player = frame.locator("video")
        expect(player).to_be_visible()
        old_media = base + player.get_attribute("src")
        assert requests.get(old_media, timeout=10).status_code == 200

        page.goto(settings)
        editor = page.locator("#registrationEditor")
        expect(editor).to_be_visible()
        require = editor.get_by_label("Require registration before playback")
        expect(require).not_to_be_checked()
        require.check()
        editor.get_by_label("Form heading").fill("Register for the screening")
        editor.get_by_label("Form description").fill("Tell us which audience you’re joining.")
        editor.get_by_label("Privacy policy URL").fill("https://example.com/privacy")
        editor.get_by_label("Form layout").select_option("steps")
        editor.get_by_label("First name field").select_option("required")
        editor.get_by_label("Last name field").select_option("off")
        editor.get_by_label("Save UTM campaign fields").check()
        editor.get_by_role("button", name="Add field").click()
        row = editor.locator("#registrationFields > li").first
        row.get_by_label("Field type").select_option("select")
        row.get_by_label("Field label").fill("Audience")
        row.get_by_label("Choices, one per line").fill("Local\nRemote")
        row.get_by_label("Required field").check()
        editor.get_by_role("button", name="Add field").click()
        row = editor.locator("#registrationFields > li").last
        row.get_by_label("Field type").select_option("checkbox")
        row.get_by_label("Field label").fill("I accept the screening terms")
        row.get_by_label("Required field").check()
        editor.get_by_label("Save as registration preset").fill("Guest screening form")
        editor.get_by_role("button", name="Save registration").click()
        expect(page.locator("#registrationStatus")).to_have_text("Registration saved.")
        assert requests.get(old_media, timeout=10).status_code == 404
        denied = requests.get(address, timeout=10, headers={
            "Sec-Fetch-Dest": "iframe", "Referer": "http://localhost:2/",
        })
        assert denied.status_code == 403

        view.reload()
        expect(frame.get_by_role("heading", name="Register for the screening")).to_have_count(0)
        password_unlock(frame)
        expect(frame.get_by_role("heading", name="Register for the screening")).to_be_visible()
        expect(player).to_have_count(0)
        assert password not in frame.locator("body").inner_html()
        token = frame.locator("[name=enrollment]").get_attribute("value")
        frame_token = frame.locator("[name=frame]").get_attribute("value")
        data = {"enrollment": token, "frame": frame_token, "email": "invalid-email"}
        invalid = requests.post(address, data=data, headers={"Sec-Fetch-Dest": "iframe"}, timeout=10)
        assert invalid.status_code == 422 and "<video" not in invalid.text
        frame.get_by_label("Email", exact=True).fill("viewer@example.test")
        frame.get_by_label("First name", exact=True).fill("=Visitor")
        frame.get_by_role("button", name="Continue", exact=True).click()
        expect(frame.get_by_role("link", name="Privacy policy")).to_have_attribute("href", "https://example.com/privacy")
        frame.get_by_role("combobox", name="Audience", exact=False).select_option("Remote")
        frame.get_by_label("I accept the screening terms").check()
        frame.get_by_role("button", name="Register and watch").click()
        assert player.evaluate("async v => {v.muted=true;await v.play();return v.videoWidth}") == 256
        new_media = base + player.get_attribute("src")
        assert requests.get(new_media, timeout=10).status_code == 200

        page.reload()
        expect(page.get_by_role("cell", name="viewer@example.test")).to_be_visible()
        assert requests.get(endpoint + "/export", timeout=10).status_code == 403
        result = requests.get(endpoint + "/export", headers=auth, timeout=10)
        result.raise_for_status()
        rows = list(csv.DictReader(StringIO(result.text)))
        assert len(rows) == 1
        assert rows[0]["first_name"] == "'=Visitor" and rows[0]["utm_source"] == "festival"
        assert rows[0]["utm_campaign"] == "november" and {"Remote", "Yes"} <= set(rows[0].values())

        editor.get_by_label("Form heading").fill("Updated screening registration")
        editor.get_by_role("button", name="Save registration").click()
        expect(page.locator("#registrationStatus")).to_have_text("Registration saved.")
        assert requests.get(new_media, timeout=10).status_code == 404
        assert requests.post(address, data=data, headers={"Sec-Fetch-Dest": "iframe"}, timeout=10).status_code == 403
        editor.get_by_label("Require registration before playback").uncheck()
        editor.get_by_role("button", name="Save registration").click()
        expect(page.locator("#registrationStatus")).to_have_text("Registration saved.")
        view.reload()
        password_unlock(frame)
        expect(player).to_be_visible()
        expect(frame.get_by_role("button", name="Register and watch")).to_have_count(0)
        works = requests.get(base + "/api/library", headers=auth, timeout=10).json()
        other = next(work["id"] for work in works if work["title"] == "Rate this video")
        page.goto(f"{base}/operator/videos/{other}/interactivity")
        editor.get_by_label("Saved registration preset").select_option(label="Guest screening form")
        editor.get_by_role("button", name="Save registration").click()
        expect(page.locator("#registrationStatus")).to_have_text("Registration saved.")
        view.goto(f"{base}/watch/work/{other}")
        expect(view.get_by_role("heading", name="Register for the screening")).to_be_visible()
    finally:
        museum.embed = previous
        guest.close()
    return dict(required_fields_and_authorized_csv=True, default_off=True,
                keeps_password_and_domain_allowlist=True, captured_utm_and_safe_csv=True,
                old_enrollment_and_media_revoked=True, decoded_registered_viewer_width=256,
                two_step_form_reusable_across_videos=True)
