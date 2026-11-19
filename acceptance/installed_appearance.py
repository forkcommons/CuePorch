"""Verify applied appearance settings render and play on independent browser surfaces."""

from base64 import b64decode
from http.server import ThreadingHTTPServer
from threading import Thread

import requests
from playwright.sync_api import expect

from installed_embed_builder import PublisherPage


def check_appearance(page, browser, base, auth):
    title = "Institutional video details"
    items = requests.get(base + "/api/library", headers=auth, timeout=10).json()
    uid = next(work["id"] for work in items if work["title"] == title)
    other = next(work["id"] for work in items if work["id"] != uid and work["asset_type"] == "video")
    work_api = f"{base}/api/library/work/{uid}"
    assert requests.get(work_api + "/appearance/watch", timeout=10).status_code == 403
    page.get_by_role("link", name="Player and page appearance").click()
    expect(page.get_by_role("heading", name="Appearance for " + title)).to_be_visible()
    page.get_by_label("Color theme").select_option("dark")
    page.get_by_label("Title font").select_option("mono")
    page.get_by_label("Background", exact=True).fill("#182331")
    page.get_by_label("Ambient backlight").check()
    page.get_by_label("Description", exact=True).uncheck()
    page.get_by_label("Speed", exact=True).uncheck()
    page.get_by_label("Save as a named preset").fill("Late screening")
    sample = page.evaluate("""() => {
        const logo = document.createElement('canvas');
        logo.width = 40; logo.height = 20;
        logo.getContext('2d').fillRect(0, 0, 40, 20);
        return logo.toDataURL().split(',')[1];
    }""")
    page.get_by_label("Logo image").set_input_files(
        {"name": "station.png", "mimeType": "image/png", "buffer": b64decode(sample)}
    )
    expect(page.get_by_role("img", name="Publisher logo")).to_be_visible()
    sample = page.locator(".appearance-preview")
    expect(sample).to_have_attribute("data-theme", "dark")
    expect(sample).to_have_css("background-color", "rgb(24, 35, 49)")
    page.get_by_label("Apply this theme to all existing videos").check()
    page.get_by_role("button", name="Save appearance").click()
    expect(page.get_by_role("status")).to_have_text("Appearance saved.")

    copied = requests.get(f"{base}/api/library/work/{other}/appearance/watch", headers=auth, timeout=10)
    copied.raise_for_status()
    values = copied.json()["settings"]
    assert values["background"] == "#182331" and values["font"] == "mono" and values["logo"]
    assert values["speed"] and values["description"]
    page.get_by_label("Saved preset").select_option("Late screening")
    expect(page.get_by_label("Color theme")).to_have_value("dark")
    page.get_by_label("Player surface").select_option("embed")
    expect(page.get_by_label("Color theme")).to_have_value("light")
    page.locator("#selectedPreset option", has_text="Late screening").wait_for(state="detached")
    page.get_by_label("Show video title").check()
    page.get_by_label("Start muted").check()
    page.get_by_role("button", name="Save appearance").click()
    expect(page.get_by_role("status")).to_have_text("Appearance saved.")

    with ThreadingHTTPServer(("127.0.0.1", 0), PublisherPage) as fixture:
        address = f"127.0.0.1:{fixture.server_port}"
        fixture.code = f'<iframe title="Published video" src="{base}/embed/work/{uid}"></iframe>'
        server = Thread(target=fixture.serve_forever, daemon=True)
        server.start()
        guest = browser.new_context()
        try:
            browser_page = guest.new_page()
            browser_page.goto(base + "/watch/work/" + uid)
            expect(browser_page.locator("body")).to_have_css("background-color", "rgb(24, 35, 49)")
            expect(browser_page.get_by_text("Contains advertisement", exact=True)).to_be_visible()
            expect(browser_page.get_by_text("A recording with", exact=False)).to_have_count(0)
            expect(browser_page.get_by_role("combobox", name="Speed", exact=True)).to_have_count(0)
            logo = browser_page.get_by_role("img", name="Publisher logo")
            expect(logo).to_be_visible()
            assert logo.evaluate("logo=>logo.decode().then(()=>logo.naturalWidth)") == 40
            browser_page.get_by_role("button", name="Mute", exact=True).click()
            browser_page.get_by_role("button", name="Play", exact=True).click()
            browser_page.wait_for_function("document.querySelector('video').videoWidth === 256")
            expect(browser_page.get_by_role("button", name="Pause", exact=True)).to_be_visible()
            requests.put(work_api + "/privacy", headers=auth, timeout=10,
                         json={"visibility": "public", "embed": "domains", "domains": [address], "download": False}
                         ).raise_for_status()
            browser_page.goto("http://" + address + "/")
            frame = browser_page.frame_locator("iframe")
            expect(frame.locator("body")).to_have_attribute("data-theme", "light")
            expect(frame.get_by_text(title, exact=True)).to_be_visible()
            expect(frame.get_by_role("combobox", name="Speed", exact=True)).to_be_visible()
            expect(frame.get_by_role("img", name="Publisher logo")).to_have_count(0)
            assert frame.locator("video").evaluate("async v=>{await v.play();return v.muted && v.videoWidth===256}")
        finally:
            guest.close()
            fixture.shutdown()
            server.join(timeout=4)
    return dict(separate_watch_and_embed_presets=True, live_appearance_preview=True, logo_decoded=True,
                custom_playback_controls=True, independent_theme_bulk_apply=True,
                editorial_disclosures_visible_when_description_hidden=True, played_both_surfaces=True)
