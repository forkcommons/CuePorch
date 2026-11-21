"""Verify regional reporting and playback opt-outs through the installed service."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time
from urllib.parse import urlsplit

import requests
from playwright.sync_api import expect


PROXY_ADDRESS = "127.0.0.2"


@contextmanager
def regional_proxy(base, region="US-CO", city=None, postal_code=None):
    upstream = urlsplit(base)

    class Relay(BaseHTTPRequestHandler):
        def relay(self):
            names = ("Accept", "Content-Type", "Range", "Referer", "User-Agent", "Sec-Fetch-Dest", "DNT", "Sec-GPC",
                     "X-CuePorch-Event-Access", "X-CuePorch-Event-Viewer", "X-CuePorch-Event-Frame",
                     "X-CuePorch-Event-Context", "X-CuePorch-Chat-Embed", "X-CuePorch-Chat-Session",
                     "X-CuePorch-Engagement-Session", "X-CuePorch-Playback")
            headers = {name: self.headers[name] for name in names if name in self.headers}
            if region:
                headers["X-CuePorch-Region"] = region
            if city:
                headers["X-CuePorch-City"] = city
            if postal_code:
                headers["X-CuePorch-Postal-Code"] = postal_code
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))) or None
            with closing(HTTPConnection(upstream.hostname, upstream.port, timeout=10,
                                        source_address=(PROXY_ADDRESS, 0))) as conn:
                conn.request(self.command, self.path, body, headers)
                with conn.getresponse() as result:
                    start = result.status
                    metadata = result.getheaders()
                    data = result.read()
            try:
                self.send_response(start)
                for name, value in metadata:
                    if name.lower() not in ("content-length", "date", "server", "transfer-encoding", "connection"):
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except ConnectionError:
                pass  # The test browser can cancel an obsolete request when navigating.

        do_GET = do_POST = relay

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Relay) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=4)


def check_regions(page, browser, base, auth, museum, venue, video_a, video_b):
    videos = [video_a, video_b]
    former_embed = museum.embed
    today = datetime.now(timezone.utc).date().isoformat()

    def report(kind, **options):
        response = requests.post(f"{base}/api/analytics/{kind}-report", headers=auth, timeout=10,
                                 json={"from": today, "to": today, "videos": videos} | options)
        response.raise_for_status()
        return response.json()

    def play(player):
        return player.evaluate("async v => {v.muted = true; await v.play(); return v.videoWidth;}")

    with regional_proxy(base) as proxy:
        visitor = browser.new_context(extra_http_headers={"X-CuePorch-Region": "CA-ON"})
        try:
            routed = visitor.new_page()
            routed.goto(f"{proxy}/watch/work/{video_a}")
            assert play(routed.locator("video")) == 256
            direct = visitor.new_page()
            direct.goto(f"{base}/watch/work/{video_b}")
            direct_player = direct.locator("video")
            assert play(direct_player) == 256
            measurement = direct_player.get_attribute("data-analytics")
            assert measurement
            deadline = time.monotonic() + 15
            result = report("region")
            while result["totals"]["views"] < 5 and time.monotonic() < deadline:
                time.sleep(.15)
                result = report("region")
            counts = {row["category"]: row["views"] for row in result["rows"]}
            assert counts == {"US-CO": 1, "Unknown": 4}, counts
            assert report("engagement", region="US-CO")["totals"]["views"] == 1
            assert report("device")["filters"]["group"] == "device"
            assert report("source")["filters"]["group"] == "source"
            assert report("date")["rows"][0]["category"] == today

            page.goto(f"{base}/operator/analytics")
            expect(page.locator("#analyticsDashboard")).to_be_visible()
            page.get_by_label("Report type").select_option("region")
            expect(page.locator('select[name="group"]')).to_be_hidden()
            page.get_by_label("Region code").fill("US-CO")
            page.get_by_label("Break down by").select_option("video")
            page.get_by_role("button", name="Apply filters").click()
            expect(page.locator("#reportRows > tr")).to_have_count(1)
            expect(page.locator("#reportRows")).to_contain_text("US-CO")
            with page.expect_download() as exported:
                page.get_by_role("button", name="Download CSV").click()
            assert exported.value.suggested_filename == "cueporch-region-report.csv"
            assert f"US-CO,Advanced report station,{video_a}," in exported.value.path().read_text(encoding="utf-8")

            page.goto(f"{base}/operator/videos/{video_a}")
            page.get_by_label("Disable playback analytics for this embed").check()
            museum.embed = page.locator("#workspaceEmbed").evaluate("""e =>
                new DOMParser().parseFromString(e.value, 'text/html').querySelector('iframe').src
            """)
            assert "dnt=1" in museum.embed
            direct.goto(venue)
            dnt_player = direct.frame_locator("iframe").locator("video")
            assert play(dnt_player) == 256 and dnt_player.get_attribute("data-analytics") is None
            for header in ("DNT", "Sec-GPC"):
                opted_out = requests.get(f"{base}/watch/work/{video_a}", headers={header: "1"}, timeout=10)
                opted_out.raise_for_status()
                assert "<video" in opted_out.text and "data-analytics=" not in opted_out.text
                assert requests.post(base + measurement, headers={header: "1"}, timeout=10).status_code == 204
            assert report("region")["totals"]["views"] == 5
        finally:
            museum.embed = former_embed
            visitor.close()
    return dict(registered_region_views=counts, direct_forged_region_ignored=True,
                dedicated_date_device_source_and_region_reports=True, region_filter_and_csv=True,
                dnt_embedded_decoded_width=256, dnt_and_gpc_observations_suppressed=True)
