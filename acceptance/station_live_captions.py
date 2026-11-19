"""Use CivicCast's real portal bundle, subtitle writer and a running HLS software encoder."""

import argparse, json, secrets, shutil, subprocess, sys, tempfile, threading, time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright
from browser import chromium
from werkzeug.serving import make_server
from civiccast.captions.hls import CaptionHlsTrack, write_hls_caption_track
from civiccast.captions.models import CaptionCue
from civiccast.stream.manifest import ManifestRendition, build_multivariant_manifest
from civiccast.stream.config import SLATE_RENDITION
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
from station_caption_clock import align

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("portal_dist", type=Path)
p.add_argument(
    "--lpm-watch-snapshot", type=Path, help="Local copy of the station watch page"
)
p.add_argument(
    "--speech-file",
    type=Path,
    help="Four-second audio fixture with 'Call to order' starting at one second",
)
a = p.parse_args()
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    www = root / "www"
    shutil.copytree(a.portal_dist, www)
    hls = www / "stream"
    hls.mkdir()
    # One spoken phrase every four seconds. The injected source cue has a known clock.
    src = root / "spoken.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=320x180:r=24:d=4",
            *(
                ["-i", str(a.speech_file)]
                if a.speech_file
                else [
                    "-f",
                    "lavfi",
                    "-i",
                    "flite=text='Call to order',adelay=1000,apad",
                ]
            ),
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-g",
            "48",
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            str(src),
        ],
        check=True,
    )
    cues = [
        CaptionCue(
            cue_id=str(i),
            start_seconds=4 * i + 1,
            end_seconds=4 * i + 2.5,
            text="Call to order",
            confidence=1,
        )
        for i in range(18)
    ]
    track = write_hls_caption_track(CaptionHlsTrack(cues), hls, segment_duration=2)
    config = replace(SLATE_RENDITION, name="government")
    master = build_multivariant_manifest(
        [ManifestRendition(config, "video.m3u8")],
        subtitle_tracks=[track.manifest_track],
    )
    (hls / "master.m3u8").write_text(master)
    proc = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(src),
            "-t",
            "72",
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_playlist_type",
            "event",
            "-hls_flags",
            "temp_file",
            str(hls / "video.m3u8"),
        ],
        stderr=subprocess.DEVNULL,
    )

    class Signal(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=www, **kwargs)

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith("/api/public/embed-player/"):
                self.path = "/index.html"
            if self.path == "/api/public/embed/channel/government":
                content = json.dumps(
                    dict(title="Council meeting", manifest_url="/stream/master.m3u8")
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(content)
                return
            super().do_GET()

    sig = ThreadingHTTPServer(("127.0.0.1", 0), Signal)
    st = threading.Thread(target=sig.serve_forever, daemon=True)
    st.start()
    library = root / "library"
    library.mkdir()
    (library / "manifest.json").write_text(
        '{"institution":"LPM","schema":1,"works":[]}'
    )
    key = secrets.token_hex(32)
    app = create_app(library, root / "db", key, "LPM")
    if a.lpm_watch_snapshot:
        import re

        html = a.lpm_watch_snapshot.read_text(encoding="utf8")
        staged, changed = re.subn(
            r"<p[^>]*>Longmont Public Media also provides.*?</p>",
            '<iframe title="Station preview" allow="autoplay; fullscreen" width="960" height="540" src="/embed/channel/government"></iframe>',
            html,
            count=1,
            flags=re.S,
        )
        assert changed == 1, "Station watch-page insertion point changed"

        @app.get("/station-preview")
        def station_preview():
            return (
                staged,
                200,
                {
                    "Content-Security-Policy": "default-src 'none'; frame-src http://127.0.0.1:*; style-src 'unsafe-inline'"
                },
            )

    web = make_server("127.0.0.1", 0, app, threaded=True)
    wt = threading.Thread(target=web.serve_forever, daemon=True)
    wt.start()
    origin = "http://127.0.0.1:" + str(web.server_port)
    try:
        url = f"http://127.0.0.1:{sig.server_port}/api/public/embed-player/channel/government"
        requests.put(
            origin + "/api/routes/channel/government",
            headers={"Authorization": "Bearer " + key},
            json=dict(
                destination=url,
                revision="live-1",
                description="Government live",
                public_authorized=True,
            ),
        ).raise_for_status()
        for _ in range(60):
            if (hls / "video.m3u8").exists() and (hls / "video.m3u8").read_text().count(
                "#EXTINF"
            ) >= 3:
                break
            time.sleep(0.2)
        ticks = align(hls / "captions" / "en", hls / "video0.ts")
        with sync_playwright() as pw:
            browser = chromium(pw)
            page = browser.new_page()
            if a.lpm_watch_snapshot:
                page.goto(origin + "/station-preview")
                frame = page.frame_locator("iframe[title='Station preview']")
            else:
                page.goto(origin + "/watch/channel/government")
                frame = page.frame_locator("iframe")
            english = frame.get_by_role("button", name="English", exact=True)
            english.click(timeout=30000)
            player = frame.locator("video")
            player.evaluate("async v=>{v.muted=true;await v.play()}")
            observed = player.evaluate("""v=>new Promise((resolve,reject)=>{let n=0;const timer=setInterval(()=>{
            const track=Array.from(v.textTracks).find(t=>t.mode==='showing');
            const cue=track?.activeCues?.[0];if(cue){clearInterval(timer);resolve({time:v.currentTime,start:cue.startTime,
            end:cue.endTime,text:cue.text,width:v.videoWidth})}else if(++n>200){clearInterval(timer);reject('no active cue')}},100)})""")
            # Speech starts one second into each four-second program cycle.
            timing_error = min(abs(observed["start"] - (1 + 4 * i)) for i in range(18))
            assert timing_error <= 0.25, observed
            assert observed["width"] > 0 and "Call to order" in observed["text"]
            frame.get_by_role("button", name="Off", exact=True).click()
            assert player.evaluate(
                "v=>Array.from(v.textTracks).every(t=>t.mode!=='showing')"
            )
            assert proc.poll() is None
            browser.close()
        print(
            json.dumps(
                dict(
                    observation=observed,
                    encoder_running=True,
                    clock_anchor_ticks=ticks,
                    cue_start_error_seconds=round(timing_error, 3),
                    caption_source="timed synthetic speech fixture",
                    player="built CivicCast portal",
                    appliance="software receiver",
                    page="local staging copy of LPM watch page"
                    if a.lpm_watch_snapshot
                    else "CuePorch watch page",
                ),
                indent=2,
            )
        )
    finally:
        proc.terminate()
        proc.wait()
        web.shutdown()
        wt.join()
        sig.shutdown()
        st.join()
