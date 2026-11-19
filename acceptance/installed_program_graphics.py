"""Exercise on-air audience graphics through installed Live Studio and PeerTube."""

from datetime import datetime, timedelta, timezone
import json

from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_events import play_event, wait_broadcast_pixel
from installed_speakers import MEDIA_FIXTURE


def check_program_graphics(base, keys):
    now = datetime.now(timezone.utc)
    schedule = {name: (now + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M")
                for name, offset in (("start", -1), ("end", 16))}
    auth = {"Authorization": "Bearer " + keys["operator"]}
    response = requests.post(base + "/api/live-events", headers=auth, timeout=15, json=dict(
        title="Audience graphics broadcast", visibility="public", type="webinar", latency="standard",
        schedule=dict(schedule, timezone="UTC", repeat="none"), save_replay=False))
    response.raise_for_status()
    event = response.json()
    (slot,) = event["occurrences"]
    suffix = f"{event['id']}/{slot['id']}"
    settings_url = f"{base}/api/live-events/{suffix}/scenes"
    api = f"{base}/api/live-events/{suffix}/engagement"
    public_api = f"{base}/events/{event['slug']}/{slot['id']}/engagement"
    watch = f"{base}{event['watch']}?occurrence={slot['id']}"
    assert requests.get(api, timeout=10).status_code == 403

    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.add_init_script(MEDIA_FIXTURE + '("#00ff00", 440)')
            operator.add_init_script("""(() => {
              const paint = CanvasRenderingContext2D.prototype.fillText;
              window.studioText = [];
              CanvasRenderingContext2D.prototype.fillText = function(text, ...args) {
                if (this.canvas.id === 'recordCanvas') {
                  window.studioText.push(text);
                  if (window.studioText.length > 150) window.studioText.shift();
                }
                return paint.call(this, text, ...args);
              };
            })()""")
            operator.goto(f"{base}/operator/live-events/{suffix}/studio")
            operator.get_by_role("button", name="Connect camera and microphone").click()
            operator.get_by_text("Scenes and graphics", exact=True).click()
            scenes = operator.locator("#studioScenes")
            expect(scenes.get_by_role("button", name="Add scene")).to_be_enabled()

            def save(name):
                with operator.expect_response(lambda result: result.url == settings_url and result.request.method == "PUT") as saved:
                    scenes.get_by_role("button", name=name, exact=True).click()
                assert saved.value.ok
                return saved.value.json()

            def take(name):
                scenes.get_by_role("button", name=name, exact=True).click()
                scenes.get_by_role("button", name="Take scene").click()
                expect(scenes.locator("#studioSceneStatus")).to_have_text("On stage: " + name)

            scenes.get_by_label("Graphic color", exact=True).fill("#2146e0")
            assert save("Save brand")["brand"]["primary"] == "#2146e0"
            scenes.get_by_label("Scene template").select_option(label="Presenter")
            save("Add scene")
            save("Duplicate scene")
            scenes.get_by_label("Scene name").fill("Second camera scene")
            save("Save scene")
            take("Presenter")
            operator.get_by_text("Q&A and polls", exact=True).click()
            panel = operator.locator("#operatorEngagement")
            policy = panel.locator("#eventQASettings")
            review = policy.get_by_label("Approve questions before publishing")
            expect(review).not_to_be_checked()
            policy.get_by_label("Enable audience Q&A").check()
            review.check()
            policy.get_by_role("button", name="Save Q&A settings").click()
            expect(panel.locator("#operatorEngagementStatus")).to_have_text("Engagement updated.")

            guest = browser.new_page()
            guest.goto(watch)
            question = "How was the film restored?"
            form = guest.locator("#askQuestion")
            form.get_by_label("Display name").fill("Guest filmmaker")
            form.get_by_label("Question", exact=True).fill(question)
            form.get_by_role("button", name="Submit question").click()
            row = panel.locator("#operatorQuestions > li").filter(has_text=question)
            expect(row.get_by_text("Awaiting approval", exact=False)).to_be_visible(timeout=12000)
            expect(row.get_by_role("button", name="Show question on scene")).to_have_count(0)
            quiz = "What was the camera format?"
            editor = panel.locator("#eventPollEditor")
            editor.get_by_label("Poll question").fill(quiz)
            editor.get_by_label("Option 1", exact=True).fill("16 mm")
            editor.get_by_label("Option 2", exact=True).fill("35 mm")
            editor.get_by_label("Correct answer").select_option("1")
            editor.get_by_role("button", name="Save poll draft").click()
            poll = panel.locator("#operatorPolls > li").filter(has_text=quiz)
            expect(poll.get_by_role("button", name="Open poll")).to_be_visible()
            expect(poll.get_by_role("button", name="Show poll on scene")).to_have_count(0)

            operator.get_by_role("button", name="Go live").click()
            expect(operator.locator("#recordStatus")).to_contain_text("Sending the live broadcast", timeout=45000)
            expect(guest.locator("#eventStatus")).to_have_text("Live", timeout=65000)
            _, width, _ = play_event(guest, watch, "Live", navigate=False)
            player = guest.locator("#eventPlayer video")
            base_green, brand_blue = [0, 255, 0], [33, 70, 224]
            wait_broadcast_pixel(player, .046, .84, base_green)
            review.uncheck()
            policy.get_by_role("button", name="Save Q&A settings").click()
            row.get_by_role("button", name="Show question on scene").click()
            operator.wait_for_function("window.studioText.includes(" + json.dumps(question) + ")")
            operator.wait_for_function("""rgb => {
              const canvas = document.getElementById('recordCanvas');
              const pixel = [...canvas.getContext('2d').getImageData(canvas.width * .046, canvas.height * .84, 1, 1).data];
              return rgb.every((value, i) => Math.abs(pixel[i] - value) < 10);
            }""", arg=brand_blue, timeout=12000)
            wait_broadcast_pixel(player, .046, .84, brand_blue)
            take("Second camera scene")
            expect(row.get_by_role("button", name="Show question on scene")).to_be_visible()
            wait_broadcast_pixel(player, .046, .84, base_green)
            take("Presenter")
            expect(row.get_by_role("button", name="Hide question from scene")).to_be_visible()
            wait_broadcast_pixel(player, .046, .84, brand_blue)
            print(f"Audience decoded a question on the selected scene at {width}px and saw it return with that scene.", flush=True)

            poll.get_by_role("button", name="Open poll").click()
            poll.get_by_role("button", name="Show poll on scene").click()
            expect(row.get_by_role("button", name="Show question on scene")).to_be_visible()
            operator.wait_for_function("window.studioText.includes('Audience poll')")
            guest.locator("#viewerPoll").get_by_role("button", name="35 mm").click()
            open_poll = requests.get(public_api, timeout=10).json()["polls"][0]
            assert open_poll["correct"] is None and open_poll["counts"] is None
            assert not operator.evaluate("window.studioText.some(text => text.includes('Correct') || text.endsWith('%'))")
            wait_broadcast_pixel(player, .55, .84, brand_blue, tolerance=22)
            poll.get_by_role("button", name="Publish results").click()
            results = guest.locator("#viewerPoll").get_by_text("35 mm · 1 votes (100%) · Correct answer")
            expect(results).to_be_visible(timeout=12000)
            operator.wait_for_function("window.studioText.includes('2. 35 mm · Correct') && window.studioText.includes('100%')")
            wait_broadcast_pixel(player, .55, .84, [95, 122, 233], tolerance=23)
            panel.locator("summary").click()
            archived = requests.post(api, headers=auth, timeout=10, json=dict(feature="poll", id=open_poll["id"], type="archive"))
            archived.raise_for_status()
            wait_broadcast_pixel(player, .55, .84, base_green)
            operator.get_by_role("button", name="Stop", exact=True).click()
            expect(operator.locator("#recordStatus")).to_contain_text("Recording ready.", timeout=25000)
            return dict(width=width, optional_question_approval_blocks_graphic=True, draft_poll_cannot_appear=True,
                        live_question_graphic=True, scene_owns_its_graphic=True, only_one_graphic_per_scene=True,
                        no_counts_or_answer_before_publish=True, audience_decoded_result_bar=True,
                        correct_answer_and_precise_votes_composed=True, archive_removed_graphic_with_panel_closed=True)
        finally:
            browser.close()
