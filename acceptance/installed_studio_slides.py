"""Verify distinct slide frames and the camera inset in installed Live Studio."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_events import play_event, wait_broadcast_pixel
from installed_speakers import MEDIA_FIXTURE


def check_studio_slides(base, keys):
    now = datetime.now(timezone.utc)
    schedule = {field: (now + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M")
                for field, offset in (("start", -1), ("end", 16))}
    auth = {"Authorization": "Bearer " + keys["operator"]}
    response = requests.post(base + "/api/live-events", headers=auth, timeout=15, json=dict(
        title="Studio presentation rehearsal", visibility="public", type="webinar", latency="standard",
        schedule=dict(schedule, timezone="UTC", repeat="none"), save_replay=False))
    response.raise_for_status()
    event = response.json()
    (slot,) = event["occurrences"]
    suffix = f"{event['id']}/{slot['id']}"
    settings_url = f"{base}/api/live-events/{suffix}/scenes"
    studio_url = f"{base}/operator/live-events/{suffix}/studio"
    watch_url = f"{base}{event['watch']}?occurrence={slot['id']}"

    with sync_playwright() as pw:
        browser = chromium(pw, ["--autoplay-policy=no-user-gesture-required"])
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.add_init_script(MEDIA_FIXTURE + '("#00ff00", 440)')
            operator.goto(studio_url)
            scenes = operator.locator("#studioScenes")

            def open_scenes():
                scenes.get_by_text("Scenes and graphics", exact=True).click()
                expect(scenes.get_by_role("button", name="Add scene")).to_be_enabled()

            def save(button):
                with operator.expect_response(lambda result: result.url == settings_url and result.request.method == "PUT") as saved:
                    scenes.get_by_role("button", name=button, exact=True).click()
                assert saved.value.ok, saved.value.text()
                return saved.value.json()

            def take(scene):
                scenes.get_by_role("button", name=scene, exact=True).click()
                scenes.get_by_role("button", name="Take scene").click()
                expect(scenes.locator("#studioSceneStatus")).to_have_text("On stage: " + scene)
                expect(slide_number).to_have_text("Slide 1 of 2")
                expect(previous).to_be_disabled()

            open_scenes()
            for extension, scene_name, layout in (("pdf", "Opening slides", "title"),
                                                   ("pptx", "Presentation and camera", "inset")):
                path = Path(__file__).with_name("fixtures") / f"studio-slides.{extension}"
                scenes.get_by_label("Presentation file").set_input_files(path)
                with operator.expect_response(lambda result: result.url == settings_url + "/slides"
                                              and result.request.method == "POST", timeout=145000) as imported:
                    scenes.get_by_role("button", name="Import slides").click()
                assert imported.value.status == 201, imported.value.text()
                deck = imported.value.json()
                assert deck["format"] == extension and deck["pages"] == 2
                page_url = f"{settings_url}/slides/{deck['id']}"
                assert requests.get(page_url + "/0", timeout=10).status_code == 403
                image = requests.get(page_url + "/0", headers=auth, timeout=10)
                assert image.ok and image.headers["Content-Type"] == "image/png"
                assert "no-store" in image.headers["Cache-Control"] and "private" in image.headers["Cache-Control"]
                assert requests.get(page_url + "/2", headers=auth, timeout=10).status_code == 404

                scenes.get_by_label("Scene template").select_option(label="Presenter")
                save("Add scene")
                scenes.get_by_label("Scene name").fill(scene_name)
                scenes.get_by_label("Scene layout").select_option(layout)
                scenes.get_by_label("Slide presentation").select_option(deck["id"])
                scene = next(item for item in save("Save scene")["scenes"] if item["title"] == scene_name)
                assert scene["presentation"] == deck["id"] and scene["layout"] == layout

            operator.reload()
            open_scenes()
            operator.get_by_role("button", name="Connect camera and microphone").click()
            speakers = operator.locator("#studioSpeakers")
            speakers.get_by_text("Speakers", exact=True).click()
            expect(speakers.get_by_label("Approve invited speakers before they connect")).not_to_be_checked()
            speakers.get_by_label("Speaker name").fill("Guest presenter")
            roster_url = f"{base}/api/live-events/{suffix}/speakers"
            with operator.expect_response(lambda result: result.url == roster_url and result.request.method == "POST") as issued:
                speakers.get_by_role("button", name="Create invitation").click()
            assert issued.value.status == 201
            address = speakers.get_by_label("Speaker invitation link").input_value()
            guest = browser.new_page()
            guest.add_init_script(MEDIA_FIXTURE + '("#ff00ff", 550)')
            guest.goto(address)
            guest.get_by_role("button", name="Join with camera and mic").click()
            expect(guest.locator("#guestSpeakerStatus")).to_contain_text("Your feed is off air", timeout=30000)
            guest_slides = guest.locator("#guestPresentation")
            guest_slides.get_by_text("Presentation", exact=True).click()
            guest_program = guest_slides.locator("#guestSlideProgram")
            guest_number = guest_program.locator("#guestSlideNumber")
            expect(guest_program).to_be_hidden()
            guest_endpoint = "/api" + urlsplit(address).path
            guest_url = base + guest_endpoint + "/slides"
            guest_token = guest.evaluate("key => sessionStorage.getItem(key)", f"cueporch-speaker:{guest_endpoint}")
            guest_auth = {"Authorization": "Bearer " + guest_token}
            assert requests.put(guest_url, timeout=10, json={}).status_code == 404
            file = Path(__file__).with_name("fixtures") / "studio-slides.pptx"
            guest_slides.get_by_label("Presentation file").set_input_files(dict(
                name="guest-program.pptx", mimeType="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                buffer=file.read_bytes()))
            with guest.expect_response(lambda result: result.url == guest_url and result.request.method == "POST", timeout=145000) as shared:
                guest_slides.get_by_role("button", name="Share slides with producer").click()
            assert shared.value.status == 201, shared.value.text()
            guest_deck = shared.value.json()
            expect(guest_slides.locator("#guestSlideUploadStatus")).to_have_text("Shared guest-program with the producer.")
            scenes.get_by_role("button", name="Presentation and camera", exact=True).click()
            choice = scenes.get_by_label("Slide presentation")
            expect(choice.locator(f"option[value='{guest_deck['id']}']")).to_have_count(1, timeout=12000)
            choice.select_option(guest_deck["id"])
            save("Save scene")
            slide_number = scenes.locator("#studioSlideNumber")
            previous = scenes.get_by_role("button", name="Previous slide")
            next_slide = scenes.get_by_role("button", name="Next slide")
            take("Opening slides")

            audience = browser.new_page()
            audience.goto(watch_url)
            expect(audience.locator("#eventStatus")).to_have_text("Scheduled")
            operator.get_by_role("button", name="Go live").click()
            expect(operator.locator("#recordStatus")).to_contain_text("Sending the live broadcast", timeout=45000)
            expect(audience.locator("#eventStatus")).to_have_text("Live", timeout=65000)
            _, width, _ = play_event(audience, watch_url, "Live", navigate=False)
            player = audience.locator("#eventPlayer video")
            red, blue, orange, purple, green = ([211, 34, 56], [35, 71, 209], [224, 138, 25], [163, 57, 187], [0, 255, 0])

            def see_slide(color):
                wait_broadcast_pixel(player, .22, .38, color)

            def advance():
                next_slide.click()
                expect(slide_number).to_have_text("Slide 2 of 2")
                expect(next_slide).to_be_disabled()

            see_slide(red)
            advance()
            see_slide(blue)
            previous.click()
            expect(slide_number).to_have_text("Slide 1 of 2")
            see_slide(red)
            print(f"Audience decoded both PDF slides and reverse navigation at {width}px.", flush=True)

            with operator.expect_response(lambda result: result.url == settings_url + "/slides/program"
                                          and result.request.method == "POST") as taken:
                take("Presentation and camera")
            program = taken.value.json()
            first_page = f"{guest_url}/{program['id']}/0"
            advance_request = dict(id=program["id"], revision=0, direction=1)
            assert requests.get(first_page, headers=guest_auth, timeout=10).status_code == 404
            assert requests.put(guest_url, headers=guest_auth, timeout=10, json=advance_request).status_code == 404
            see_slide(orange)
            wait_broadcast_pixel(player, .83, .82, green)

            speakers.get_by_role("button", name="Put on air · Guest presenter").click()
            expect(guest.locator("#guestSpeakerStatus")).to_have_text("You are on air.")
            expect(guest_number).to_have_text("Slide 1 of 2")
            expect(guest_program.get_by_role("img", name="Current slide")).to_be_visible()
            assert requests.get(first_page, timeout=10).status_code == 404
            assert requests.get(first_page, headers=guest_auth, timeout=10).ok
            wait_broadcast_pixel(player, .83, .82, [255, 0, 255])
            guest_program.get_by_role("button", name="Next slide").click()
            expect(guest_number).to_have_text("Slide 2 of 2")
            expect(slide_number).to_have_text("Slide 2 of 2")
            see_slide(purple)
            stale_turn = requests.put(guest_url, headers=guest_auth, timeout=10, json=dict(advance_request, direction=-1))
            assert stale_turn.ok and stale_turn.json()["page"] == 1
            second_page = f"{guest_url}/{program['id']}/1"
            assert requests.get(second_page, headers=guest_auth, timeout=10).ok
            speakers.get_by_role("button", name="Take off air · Guest presenter").click()
            expect(guest_program).to_be_hidden()
            assert requests.get(second_page, headers=guest_auth, timeout=10).status_code == 404
            assert requests.put(guest_url, headers=guest_auth, timeout=10,
                                json=dict(advance_request, revision=1, direction=-1)).status_code == 404
            print("Audience decoded both guest-uploaded PowerPoint slides, the guest inset and a guest-triggered slide turn.", flush=True)
            operator.get_by_role("button", name="Stop", exact=True).click()
            expect(operator.locator("#recordStatus")).to_contain_text("Recording ready.", timeout=25000)
            return dict(width=width, pdf_slides_decoded=2, powerpoint_slides_decoded=2, reverse_navigation=True,
                        camera_inset_decoded=True, scene_and_deck_persist_after_reload=True,
                        slide_assets_private=True, invalid_slide_rejected=True, guest_uploaded_powerpoint=True,
                        guest_admitted_automatically=True, guest_camera_inset_decoded=True, guest_turn_decoded=True,
                        backstage_preview_and_control_denied=True, stale_turn_did_not_move_slide=True)
        finally:
            browser.close()
