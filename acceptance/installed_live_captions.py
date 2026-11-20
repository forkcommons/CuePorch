"""Use a live encoder, real TextFlowKit and browser media-time caption checks."""

from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
import json
import re
import subprocess
import time
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import expect, sync_playwright
from playwright.sync_api import TimeoutError as BrowserTimeout
import requests

from browser import chromium
from installed_events import wait_for_delivery


@contextmanager
def spoken_signal(ffmpeg, encoder, speech, seconds):
    address = encoder["server"].rstrip("/") + "/" + encoder["key"]
    command = [str(ffmpeg), "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "color=navy:s=320x180:r=24",
               "-re", "-stream_loop", "-1", "-i", str(speech), "-map", "0:v:0", "-map", "1:a:0", "-t", str(seconds),
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-tune", "zerolatency", "-g", "48",
               "-c:a", "aac", "-b:a", "96k", "-f", "flv", address]
    with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as signal:
        try:
            yield signal
        finally:
            if signal.poll() is None:
                signal.terminate()
                signal.wait(timeout=8)


def browser_caption_state(video):
    return video.evaluate("""v => ({position:v.currentTime, paused:v.paused, seeking:v.seeking, ready:v.readyState,
        error:v.error ? {code:v.error.code, message:v.error.message} : null, hls:window.captionHlsState?.(),
        buffered:Array.from({length:v.buffered.length}, (_,i)=>[v.buffered.start(i),v.buffered.end(i)]),
        ranges:Array.from({length:v.seekable.length}, (_,i)=>[v.seekable.start(i),v.seekable.end(i)]),
        tracks:[...v.textTracks].map(t=>({label:t.label, mode:t.mode, cues:[...(t.cues || [])].slice(-10)
            .map(c=>[c.startTime,c.endTime,c.text]), active:[...(t.activeCues || [])].map(c=>c.text)}))})""")


def set_caption_policy(base, auth, enabled):
    url = base + "/api/team/ai"
    response = requests.get(url, headers=auth, timeout=10)
    response.raise_for_status()
    policy = response.json()
    policy["settings"]["features"]["live_caption"]["enabled"] = enabled
    policy["settings"]["dictionary"] = [
        dict(language="en", source="City Council", preferred="Fixture City Council"),
        dict(language="en", source="Council", preferred="Fixture Council"),
        dict(language="es", source="City Council", preferred="Unused Spanish term"),
    ]
    requests.put(url, json=policy, headers=auth | {"X-CuePorch-Team": "1"}, timeout=10).raise_for_status()


def check_live_captions(base, keys, ffmpeg, speech, registered=False, protected=False, regional_origins=None,
                        governed=False):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    end = (now + timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M")
    auth = {"Authorization": "Bearer " + keys["operator"]}
    title = "Protected caption commissioning" if protected else "Restricted caption commissioning" if registered else "Live caption commissioning"
    slug = "protected-caption-rehearsal" if protected else "restricted-caption-rehearsal" if registered else "live-caption-rehearsal"
    event = dict(title=title, slug=slug, visibility="public", type="live",
                 latency="standard", save_replay=False, dvr_seconds=120,
                 schedule=dict(timezone="UTC", start=start, end=end, repeat="none"))
    if protected:
        event.update(visibility="password", view_password="fixture-caption-screening",
                     starting_options=dict(method="encoder", registration=registered, preview_encoder=False,
                                           captions=dict(enabled=True, language="en"), chat=dict(enabled=False, interval=0),
                                           qa=dict(enabled=False, anonymous=False, review=False)))
    audience_origin = base
    if regional_origins:
        audience_origin, blocked_origin = regional_origins
        event["regional_policy"] = {"mode": "allow", "areas": ["US-CO"]}
    response = requests.post(base + "/api/live-events", headers=auth, json=event, timeout=12)
    response.raise_for_status()
    entry = response.json()
    (slot,) = entry["occurrences"]
    opens = slot["id"]
    stable = audience_origin + entry["watch"] + f"?occurrence={opens}"
    caption_route = audience_origin + f"/events/{entry['slug']}/{opens}/captions"
    production = base + f"/api/live-events/{entry['id']}/{opens}"
    if registered:
        registration = production.rsplit("/", 1)[0] + "/registration"
        response = requests.get(registration, headers=auth, timeout=10)
        response.raise_for_status()
        form = response.json()
        form["settings"].update(enabled=True, heading="Register for caption commissioning")
        requests.put(registration, json=form, headers=auth, timeout=10).raise_for_status()
    gate = base + "/api/policy/caption"
    assert requests.get(gate.removesuffix("/caption"), headers=auth, timeout=10).json()["caption"] is False
    requests.put(gate, headers=auth, json={"required": True}, timeout=10).raise_for_status()
    assert requests.get(production + "/captions", timeout=10).status_code == 403
    withdrawn = False
    with sync_playwright() as pw:
        browser = chromium(pw)
        try:
            operator = browser.new_page()
            operator.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(keys['operator'])})")
            operator.goto(base + "/operator/live-events")
            operator.get_by_role("button", name=f"Open production for {title}", exact=False).click()
            operator.get_by_text("Live captions", exact=True).click()
            if not protected:
                expect(operator.locator("#productionCaptionStatus")).to_have_text("Automatic captions are off.")
                operator.get_by_label("Generate automatic live captions").check()
                operator.get_by_role("button", name="Save live caption settings").click()
            expect(operator.locator("#productionCaptionStatus")).to_contain_text("Waiting for the event stream")
            if governed:
                set_caption_policy(base, auth, False)
                refused = requests.put(production + "/captions", headers=auth,
                                       json=dict(enabled=True, language="en"), timeout=10)
                assert refused.status_code == 403
                operator.get_by_role("button", name="Refresh caption status").click()
                expect(operator.locator("#productionCaptionStatus")).to_contain_text(
                    "Automatic caption generation is disabled in workspace AI settings")
            setup = requests.post(production + "/encoder", headers=auth, timeout=30)
            setup.raise_for_status()
            audience = browser.new_page()
            audience.add_init_script("""(() => {
                let constructor, player;
                const errors = [];
                Object.defineProperty(window, 'Hls', {
                    configurable:true, get:() => constructor,
                    set:Hls => {
                        constructor = new Proxy(Hls, {construct(target, args, constructor) {
                            player = Reflect.construct(target, args, constructor);
                            player.on(Hls.Events.ERROR, (_, event) => {
                                errors.push({type:event.type, details:event.details, fatal:event.fatal});
                            });
                            return player;
                        }});
                    },
                });
                window.captionHlsState = () => ({errors:errors.slice(-8), date:player?.playingDate?.toISOString()});
            })()""")
            audience.goto(stable)
            viewer = {}
            if protected:
                audience.get_by_label("Event password").fill(event["view_password"])
                audience.get_by_role("button", name="Unlock event").click()
            if registered:
                signup = audience.get_by_role("form", name="Register for caption commissioning")
                signup.get_by_label("Email").fill("viewer@example.invalid")
                signup.get_by_role("button", name="Register and watch").click()
                expect(signup).to_have_count(0)
                viewer["X-CuePorch-Event-Viewer"] = audience.locator("#eventLobby").get_attribute("data-viewer-grant")
                assert viewer["X-CuePorch-Event-Viewer"]
            if protected:
                viewer["X-CuePorch-Event-Access"] = audience.locator("#eventLobby").get_attribute("data-audience-access")
                assert viewer["X-CuePorch-Event-Access"]
            expect(audience.locator("#eventStatus")).to_have_text("Waiting for the encoder")
            with spoken_signal(ffmpeg, setup.json(), speech, 145) as encoder:
                wait_for_delivery(production, auth, "live", 45, encoder)
                expect(audience.locator("#eventStatus")).to_have_text("Live", timeout=22000)
                video = audience.get_by_label("Event video", exact=True)
                try:
                    audience.wait_for_function("document.querySelector('#eventPlayer video')?.readyState >= 2", timeout=30000)
                    video.evaluate("""async v => {
                        v.muted = true;
                        await Promise.race([v.play(), new Promise((_, reject) =>
                            setTimeout(() => reject(Error('The live player did not start')), 10000))]);
                    }""")
                except BrowserTimeout as error:
                    raise AssertionError(f"The live player did not decode a frame: {browser_caption_state(video)}") from error
                width = video.evaluate("v=>v.videoWidth")
                assert width >= 240
                if governed:
                    audience.wait_for_function("document.querySelector('#eventPlayer video')?.currentTime >= 2")
                    held = requests.get(production + "/captions", headers=auth, timeout=10).json()
                    assert (held["status"], held["cue_count"]) == ("blocked", 0), held
                    set_caption_policy(base, auth, True)
                deadline = time.monotonic() + 50
                while time.monotonic() < deadline:
                    status = requests.get(production + "/captions", headers=auth, timeout=10)
                    status.raise_for_status()
                    if status.json()["cue_count"] >= 4:
                        break
                    time.sleep(1)
                else:
                    raise AssertionError(f"No caption sentences appeared in the local engine's output: {status.json()}")
                assert not requests.get(caption_route, headers=viewer, timeout=10).json()["cues"]
                expect(audience.get_by_role("button", name="Show live captions")).to_be_disabled()
                requests.put(gate, headers=auth, json={"required": False}, timeout=10).raise_for_status()
                audience.get_by_role("button", name="Show live captions").click(timeout=15000)
                audience.get_by_text("Live transcript", exact=True).click()
                expected = r"fixture (?:city )?council" if governed else r"city\s*council"
                expect(audience.get_by_label("Caption lines")).to_contain_text(re.compile(expected, re.I), timeout=15000)
                if registered or protected:
                    assert requests.get(caption_route, timeout=10).status_code == 404
                    anonymous = requests.get(stable, headers={"Accept": "application/json"}, timeout=10)
                    if protected:
                        assert anonymous.status_code == 404
                    else:
                        assert not anonymous.json()["address"]
                pick = """() => {
                    const v = document.querySelector('#eventPlayer video');
                    const track = [...v.textTracks].find(item => item.mode === 'showing' && item.label === 'Live captions');
                    const matches = [...(track?.cues || [])].filter(c=>/council/i.test(c.text));
                    const cue = matches.find(c => c.endTime > c.startTime && c.endTime + 1 < v.currentTime &&
                        Array.from({length:v.seekable.length}, (_,i)=>i).some(i=>v.seekable.start(i) <= c.startTime && c.endTime < v.seekable.end(i)));
                    const rows = [...document.querySelectorAll('#eventCaptions li button')];
                    const sameCues = cue ? [...track.cues].filter(c => c.text === cue.text) : [];
                    const duplicates = rows.map((button,i) => button.textContent === cue?.text ? i : -1).filter(i => i >= 0);
                    const row = cue ? duplicates[sameCues.indexOf(cue)] : -1;
                    return row >= 0 ? {row, text:cue.text, start:cue.startTime} : false;
                }"""
                try:
                    candidate = audience.wait_for_function(pick, timeout=30000).json_value()
                except BrowserTimeout as error:
                    raise AssertionError(f"No selectable browser cue at playback state {browser_caption_state(video)}") from error
                before = video.evaluate("v => { v.pause(); return v.currentTime; }")
                audience.get_by_label("Caption lines").get_by_role("button").nth(candidate["row"]).click()
                try:
                    audience.wait_for_function("""expected => {
                        const v = document.querySelector('#eventPlayer video');
                        const track = [...v.textTracks].find(item => item.mode === 'showing' && item.label === 'Live captions');
                        return !v.seeking && v.videoWidth >= 240 && Math.abs(v.currentTime - expected.start) < 0.2 &&
                            [...(track?.activeCues || [])].some(c=>c.text === expected.text);
                    }""", arg=candidate, timeout=15000)
                except BrowserTimeout as error:
                    raise AssertionError(f"Seek to {candidate} did not activate its cue: {browser_caption_state(video)}") from error
                rewind = before - video.evaluate("v => v.currentTime")
                assert rewind >= 1
                assert encoder.poll() is None
                manifest = urljoin(stable, audience.locator("#eventPlayer").get_attribute("data-address"))
                assert requests.get(production + "/health", headers=auth, timeout=10).json()["public"] is False
                if regional_origins:
                    playlist = requests.get(manifest, timeout=10)
                    playlist.raise_for_status()
                    variant = next(line for line in playlist.text.splitlines() if line and not line.startswith("#"))
                    rendition = urljoin(manifest, variant)
                    segments = requests.get(rendition, timeout=10)
                    segments.raise_for_status()
                    chunk = next(line for line in segments.text.splitlines() if line and not line.startswith("#"))
                    segment = urljoin(rendition, chunk)
                    assert len(requests.get(segment, timeout=10).content) > 188
                    for path in (manifest, segment, caption_route):
                        target = urlsplit(path).path
                        assert requests.get(blocked_origin + target, headers=viewer, timeout=10).status_code == 403
                        assert requests.get(base + target, headers=viewer | {"X-CuePorch-Region": "US-CO"},
                                            timeout=10).status_code == 403
                response = requests.delete(production + "/delivery", headers=auth, timeout=30)
                assert response.status_code == 204, response.status_code
                withdrawn = True
                assert requests.get(caption_route, headers=viewer, timeout=10).status_code == 404
                assert requests.get(manifest, timeout=10).status_code == 404
                expect(audience.locator("#eventPlayer video")).to_have_count(0, timeout=14000)
                print(f"Captioned {'registered' if registered else 'public'} playback at {width}px with {rewind:.2f}s rewind. "
                      f"CuePorch links revoked; host withdrawal response {response.status_code}.", flush=True)
            return dict(native_textflowkit_streaming=True, external_encoder_captioned=True, real_playback_width=width,
                        optional_caption_approval_holds_then_releases=True,
                        viewer_decoded_caption_cue_after_transcript_seek=True, withdrawal_revokes_caption_access=True,
                        withdrawal_revokes_cueporch_manifest=True, peer_withdrawal_http=response.status_code,
                        rewind_seconds=round(rewind, 2), requires_registration=registered, requires_password=protected,
                        inherited_caption_defaults=protected, trusted_region_enforced_on_hls_and_captions=bool(regional_origins),
                        workspace_gate_and_language_dictionary=governed)
        finally:
            browser.close()
            if not withdrawn:
                with suppress(requests.RequestException):
                    requests.delete(production + "/delivery", headers=auth, timeout=15)
