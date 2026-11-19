"""Build an embed in the installed app and use its player API from another origin."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import requests
from playwright.sync_api import expect


class PublisherPage(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(("<!doctype html><title>Publishing fixture</title>" + self.server.code).encode())

    def log_message(self, *args):
        pass


def check_embed_builder(page, browser, base, auth):
    items = requests.get(base + "/api/library", headers=auth, timeout=10).json()
    uid = next(work["id"] for work in items if work["title"] == "Institutional video details")
    with ThreadingHTTPServer(("127.0.0.1", 0), PublisherPage) as fixture:
        address = f"127.0.0.1:{fixture.server_port}"
        server = Thread(target=fixture.serve_forever, daemon=True)
        server.start()
        visitor = browser.new_context()
        try:
            endpoint = f"{base}/api/library/work/{uid}/privacy"
            requests.put(endpoint, headers=auth, timeout=10,
                         json={"visibility": "public", "embed": "domains", "domains": [address], "download": False}
                         ).raise_for_status()
            page.goto(f"{base}/operator/videos/{uid}")
            panel = page.get_by_role("group", name="Embed options")
            expect(panel).to_be_visible()
            page.get_by_label("Width", exact=True).fill("800")
            page.get_by_label("Height", exact=True).fill("450")
            code = page.get_by_label("Embed code")
            assert 'width="800" height="450"' in code.input_value()
            page.get_by_label("Responsive sizing").check()
            page.get_by_label("Autoplay", exact=True).check()
            page.get_by_label("Start muted").check()
            page.get_by_label("Loop video").check()
            page.get_by_label("Include player API script").check()
            fixture.code = code.input_value()
            assert "width:100%;aspect-ratio:800/450" in fixture.code and 'referrerpolicy="origin"' in fixture.code

            parent = visitor.new_page()
            parent.goto("http://" + address + "/")
            frame = parent.frame_locator("iframe")
            video = frame.locator("video")
            expect(video).to_be_visible()
            assert video.evaluate("v => v.autoplay && v.muted && v.loop")
            values = parent.evaluate(
                """async (base) => {
                   const {Player} = await import(base + '/player.js');
                   const player = new Player(document.querySelector('iframe'));
                   await player.ready();
                   await player.pause();
                   const sought = await player.setCurrentTime(.8);
                   const duration = await player.getDuration();
                   const volume = await player.setVolume(.4);
                   const looping = await player.setLoop(false);
                   const played = new Promise(resolve => player.on('play', resolve));
                   await player.play();
                   const event = await played;
                   await player.pause();
                   player.disconnect();
                   return {sought, duration, volume, looping, event};
                }""", base,
            )
            assert abs(values["sought"] - .8) <= .02 and values["duration"] > 1
            assert abs(values["volume"] - .4) <= .01 and not values["looping"] and values["event"]["muted"]
            assert video.evaluate("v => v.videoWidth") == 256
            denied = base + f"/embed/work/{uid}?api=1&autoplay=1"
            assert requests.get(denied, timeout=10, headers={"Sec-Fetch-Dest": "iframe",
                                "Referer": "https://other-site.invalid/"}).status_code == 403
        finally:
            visitor.close()
            fixture.shutdown()
            server.join(timeout=4)
    return dict(responsive_and_fixed_markup=True, native_muted_autoplay_and_loop=True,
                cross_origin_verified_api=True, frame_host_rules_still_enforced=True, video_width=256)
