"""Verify customized watch and embed controls on the installed live player."""

from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from threading import Thread

from PIL import Image
from playwright.sync_api import expect, sync_playwright
import requests

from browser import chromium
from installed_embed_builder import PublisherPage
from installed_events import send_signal, wait_for_delivery


def check_event_controls(base, keys, ffmpeg):
    auth = {"Authorization": "Bearer " + keys["operator"]}

    def api(method, path, data=None):
        response = requests.request(method, base + path, headers=auth, timeout=25, json=data)
        response.raise_for_status()
        return response.json()

    now = datetime.now(timezone.utc)
    title = "Live playback controls"
    event = api("POST", "/api/live-events", dict(
        title=title, slug="live-controls", type="live", latency="standard", visibility="public",
        dvr_seconds=120, save_replay=False, schedule=dict(timezone="UTC", repeat="none",
            start=(now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M"),
            end=(now + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M"))))
    opens = event["occurrences"][0]["id"]
    path = f"/api/live-events/{event['id']}"
    watch, embed = (f"{base}{event[surface]}?occurrence={opens}" for surface in ("watch", "embed"))
    config = path + "/player"
    raw = BytesIO()
    Image.new("RGB", (64, 24), "#0d689e").save(raw, format="PNG")
    logo_bytes = raw.getvalue()

    with sync_playwright() as pw, ThreadingHTTPServer(("127.0.0.1", 0), PublisherPage) as publisher:
        publisher.code = f'<iframe title="Station player" src="{embed}" width="800" height="700" allowfullscreen></iframe>'
        thread = Thread(target=publisher.serve_forever, daemon=True)
        thread.start()
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.goto(base + "/operator/live-events")
            operator.get_by_role("button", name=f"Open production for {title}").click()
            panel = operator.locator("#eventPlayerSettings")
            panel.get_by_text("Event player", exact=True).click()
            save = panel.get_by_role("button", name="Save event player")
            customization = panel.get_by_label("Customize viewer controls")
            expect(save).to_be_enabled()
            expect(customization).not_to_be_checked()
            expect(panel.get_by_label("Primary color")).to_be_disabled()
            badge = panel.get_by_label("Show live badge")
            count = panel.get_by_label("Show current viewer count")
            byline = panel.get_by_label("Player byline")
            expect(badge).not_to_be_checked()
            expect(count).not_to_be_checked()
            badge.check()
            count.check()
            byline.fill("Longmont community showcase")
            panel.get_by_label("Player logo").set_input_files(dict(name="station.png", mimeType="image/png", buffer=logo_bytes))
            assert panel.locator("#eventLogoPreview").evaluate("async i => { await i.decode(); return i.naturalWidth }") == 64
            customization.check()
            panel.get_by_label("Play button").select_option("auto")
            panel.get_by_label("Show event title").uncheck()
            panel.get_by_label("Allow picture in picture").uncheck()
            panel.get_by_label("Primary color").fill("#166273")
            panel.get_by_label("Accent color").fill("#e5ab44")
            panel.get_by_label("Icon color").fill("#f0f7ef")

            def persist():
                with operator.expect_response(lambda response: response.url == base + config and response.request.method == "PUT") as saved:
                    save.click()
                response = saved.value
                assert response.ok, (response.status, response.text())
                expect(panel.locator("#eventPlayerStatus")).to_have_text("Event player saved.")

            persist()
            rejected = requests.put(base + config, json={"primary": "red"}, headers=auth, timeout=10)
            assert rejected.status_code == 422 and api("GET", config)["primary"] == "#166273"
            watcher = browser.new_page()
            watcher.goto(watch)
            outside = browser.new_page()
            outside.goto(f"http://127.0.0.1:{publisher.server_port}/")
            frame = outside.get_by_title("Station player").element_handle().content_frame()
            assert frame
            for surface in (watcher, frame):
                expect(surface.locator("#eventStatus")).to_have_text("Scheduled")
                expect(surface.get_by_role("heading", name=title)).to_have_count(0)
                expect(surface.locator("#eventBranding")).to_be_hidden()

            encoder = api("POST", f"{path}/{opens}/encoder")
            with send_signal(ffmpeg, encoder["server"], encoder["key"], 110) as process:
                wait_for_delivery(base + f"{path}/{opens}", auth, "live", 40, process)
                for surface in (watcher, frame):
                    expect(surface.locator("#eventStatus")).to_have_text("Live", timeout=35000)
                    player = surface.locator("#eventPlayer")
                    video = player.locator("video")
                    play = player.get_by_role("button", name="Play", exact=True)
                    expect(play).to_have_css("position", "absolute")
                    expect(play).to_have_css("background-color", "rgb(22, 98, 115)")
                    expect(play).to_have_css("color", "rgb(240, 247, 239)")
                    expect(player.get_by_label("Position")).to_have_css("accent-color", "rgb(229, 171, 68)")
                    assert video.evaluate("v => !v.controls && v.disablePictureInPicture")
                    expect(player.get_by_role("button", name="Picture in picture")).to_have_count(0)
                    play.click()
                    surface.wait_for_function("document.querySelector('#eventPlayer video')?.videoWidth === 320")
                    expect(player.get_by_role("button", name="Pause", exact=True)).to_have_css("position", "static")
                    brand = player.locator("#eventBranding")
                    expect(brand.locator(".event-live-badge")).to_have_text("Live")
                    expect(brand.locator(".event-byline")).to_have_text("Longmont community showcase")
                    logo = brand.get_by_role("img", name="Event logo")
                    assert logo.evaluate("async i => { await i.decode(); return i.naturalWidth }") == 64
                    logo_url = base + logo.get_attribute("src")
                    assert requests.get(logo_url, timeout=10).content == logo_bytes
                for surface in (watcher, frame):
                    expect(surface.locator(".event-viewer-count")).to_have_text("2 watching", timeout=18000)

                player = watcher.locator("#eventPlayer")
                video = player.locator("video")
                player.get_by_role("button", name="Mute", exact=True).click()
                assert video.evaluate("v => v.muted")
                volume = player.get_by_label("Volume")
                volume.press("Home")
                volume.press("ArrowRight")
                assert video.evaluate("v => !v.muted && Math.abs(v.volume - .05) < .001")
                watcher.wait_for_function("""() => {
                    const v = document.querySelector('#eventPlayer video');
                    return v.seekable.length && v.currentTime - v.seekable.start(v.seekable.length - 1) > 7;
                }""", timeout=25000)
                before = video.evaluate("v => v.currentTime")
                player.get_by_label("Position").press("Home")
                watcher.wait_for_function("""t => {
                    const v = document.querySelector('#eventPlayer video');
                    return !v.seeking && v.readyState >= 2 && t - v.currentTime >= 5;
                }""", arg=before, timeout=12000)
                rewind = before - video.evaluate("v => v.currentTime")
                player.get_by_role("button", name="Fullscreen").click()
                watcher.wait_for_function("document.fullscreenElement?.id === 'eventPlayer'")
                player.get_by_role("button", name="Fullscreen").click()
                watcher.wait_for_function("!document.fullscreenElement")
                for key in ("Show volume", "Show playback progress", "Allow fullscreen"):
                    panel.get_by_label(key).uncheck()
                panel.get_by_role("button", name="Remove logo").click()
                badge.uncheck()
                count.uncheck()
                byline.fill("")
                persist()
                assert requests.get(logo_url, timeout=10).status_code == 404
                for surface in (watcher, frame):
                    player = surface.locator("#eventPlayer")
                    expect(player.locator("#eventBranding")).to_be_hidden(timeout=16000)
                    expect(player.get_by_label("Volume")).to_be_hidden(timeout=16000)
                    expect(player.get_by_label("Position")).to_be_hidden(timeout=16000)
                    expect(player.get_by_role("button", name="Fullscreen")).to_have_count(0)
                    assert player.locator("video").evaluate("v => !v.paused && v.videoWidth === 320")
                customization.uncheck()
                persist()
                assert not api("GET", config)["customize"]
                for surface, address in ((watcher, watch), (frame, embed)):
                    expect(surface.get_by_role("heading", name=title)).to_be_visible(timeout=16000)
                    assert surface.locator("#eventPlayer video").evaluate("v => v.controls && !v.disablePictureInPicture")
                    assert surface.url == address

            return dict(decoded_watch_width=320, decoded_embed_width=320, saved_colors=True, automatic_play_button=True,
                        volume_and_mute_applied=True, rewind_decoded_seconds=round(rewind, 2), fullscreen=True,
                        hidden_controls_updated_on_open_links=True, default_controls_restored=True,
                        logo_and_byline_on_both_links=True, live_badge_and_two_viewers=True,
                        removal_revokes_logo_and_clears_branding=True,
                        invalid_color_rejected=True, stable_links=True)
        finally:
            browser.close()
            publisher.shutdown()
            thread.join()
