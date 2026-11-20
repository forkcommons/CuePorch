"""Exercise the installed launcher or run a fresh headless source rehearsal."""

import argparse, hashlib, json, os, socket, subprocess, sys, tempfile, threading, time
from contextlib import ExitStack
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
from playwright.sync_api import sync_playwright
from demo_source import build_server
from browser import chromium
from installed_library import check_folders
from installed_trash import check_private
from installed_cards import check_cards
from installed_versions import check_versions, generate_replacement
from installed_review_links import check_review_defaults, check_review_links
from installed_upload import check_uploads
from installed_privacy import check_folder_privacy, check_privacy, check_privacy_defaults
from installed_migrated_privacy import check_migrated_privacy
from installed_video_details import check_imported_video_details, check_video_details, check_workspace
from installed_embed_builder import check_embed_builder
from installed_appearance import check_appearance
from installed_languages import check_languages
from installed_audio_tracks import check_audio_tracks
from installed_caption_generation import check_caption_generation, speech_video
from installed_interactivity import check_chapters
from installed_presentation import check_presentation
from installed_ratings import check_ratings
from installed_registration import check_registration
from installed_analytics import check_analytics
from installed_advanced_analytics import check_advanced_analytics
from installed_community_analytics import check_community_analytics
from installed_discovery import check_discovery
from installed_audience_accounts import check_audience_accounts
from installed_audience_collections import check_audience_collections
from installed_audience_comments import check_audience_comments
from installed_team_accounts import check_team_accounts
from installed_regions import PROXY_ADDRESS
from installed_editing import check_editing, fixture as editor_fixture
from installed_events import check_event_calendar, check_event_chat, check_event_engagement
from installed_recording import check_recording
from installed_showcases import check_showcases, check_transferred_showcase
from installed_timeline import check_timeline, timeline_fixtures

p = argparse.ArgumentParser()
p.add_argument("launcher", type=Path)
p.add_argument("baseline", type=Path)
p.add_argument("report", type=Path)
p.add_argument("--source", action="store_true", help="Run desktop_service.py as the launcher for a fresh rehearsal")
p.add_argument("--review-push", action="store_true", help="Use the local encrypted review push receiver")
p.add_argument("--caption-speech", type=Path, help="Synthetic narration for the installed local TextFlowKit runtime")
p.add_argument(
    "--windows-setup",
    type=Path,
    help="Install into the isolated launcher directory and verify the generated Start menu shortcut",
)
p.add_argument(
    "--windows-baseline-setup",
    type=Path,
    help="Exercise an upgrade from the released 0.2.0 Setup executable",
)
p.add_argument("--only", choices=("fresh", "0.1.0", "0.2.0"),
               help="Run just one available installation case")
focused = p.add_mutually_exclusive_group()
focused.add_argument("--analytics-only", action="store_true", help="Test migration and the analytics reports on the installed package")
focused.add_argument("--discovery-only", action="store_true", help="Test migration and public discovery on the installed package")
focused.add_argument("--audience-accounts-only", action="store_true", help="Test migration, discovery and viewer accounts on the installed package")
focused.add_argument("--audio-tracks-only", action="store_true", help="Test migration and alternate audio on the installed package")
focused.add_argument("--team-only", action="store_true", help="Test migration and team access on the installed package")
focused.add_argument("--cutover-only", action="store_true", help="Test Vimeo migration, team access and embed replacement")
p.add_argument("--analytics-region-proxy", action="store_true",
               help="Exercise the trusted region source and tracking opt-outs using a second loopback IP")
p.add_argument("--analytics-bandwidth", action="store_true", help="Measure real ranged and full media responses")
p.add_argument("--analytics-unique", action="store_true", help="Check distinct viewers and browser identity coverage")
p.add_argument("--analytics-captions", action="store_true", help="Check usage of captions actually shown during playback")
p.add_argument("--analytics-community", action="store_true", help="Check audience likes and published comment reporting")
args = p.parse_args()
if args.source and (args.only != "fresh" or args.windows_setup or args.windows_baseline_setup):
    p.error("--source requires --only fresh and runs without an installer")
if args.windows_baseline_setup and not args.windows_setup:
    p.error("--windows-baseline-setup requires --windows-setup")
if args.only == "0.2.0" and not args.windows_baseline_setup:
    p.error("--only 0.2.0 requires --windows-baseline-setup")
results = []


def save_report():
    execution = "source" if args.source else "installed"
    report = [dict(result, execution=execution) for result in results]
    args.report.write_text(json.dumps(report, indent=2), encoding="utf8")


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


def shut_down(proc, base, desktop, home):
    response = requests.post(base + "/desktop/api/quit", timeout=10,
                             headers={"Authorization": "Bearer " + desktop.split("#")[1]})
    response.raise_for_status()
    assert proc.wait(timeout=20) == 0, f"{home.name}: CuePorch reported a shutdown error"


def install_windows(setup, home):
    subprocess.run(
        [
            str(setup.resolve()),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/DIR=" + str(args.launcher.resolve().parent),
            "/LIBRARY=" + str(home),
        ],
        check=True,
    )
    shortcut_script = """
    $link=Join-Path ([Environment]::GetFolderPath('Programs')) 'ForkCommons/CuePorch.lnk'
    $shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut($link)
    [pscustomobject]@{target=$shortcut.TargetPath; arguments=$shortcut.Arguments}|ConvertTo-Json -Compress
    """
    link = json.loads(
        subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command", shortcut_script],
            text=True,
        )
    )
    assert Path(link["target"]).resolve() == args.launcher.resolve(), link[
        "target"
    ]
    assert link["arguments"] == '--data "' + str(home) + '"', link[
        "arguments"
    ]

def wait_for_desktop(proc, home):
    marker = home / "desktop-session.json"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{home.name}: CuePorch service exited with code {proc.returncode}")
        try:
            return json.loads(marker.read_text(encoding="utf8"))["url"]
        except (FileNotFoundError, PermissionError, json.JSONDecodeError):
            time.sleep(0.1)
    service_log = home / "service.log"
    log_size = service_log.stat().st_size if service_log.is_file() else None
    raise TimeoutError(f"{home.name}: no readable desktop session after 60 seconds. Service log bytes: {log_size}")


def start_candidate(home, env):
    launcher = str(args.launcher.resolve())
    if args.source:
        home.mkdir(parents=True, exist_ok=True)
        with (home / "service.log").open("ab") as log:
            return subprocess.Popen([sys.executable, launcher, "--data", str(home)],
                                    stdout=log, stderr=log, env=env)
    return subprocess.Popen([launcher, "--data", str(home), "--headless"], env=env)


def prepare_previous_windows(home, source, venue):
    install_windows(args.windows_baseline_setup, home)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [str(args.launcher.resolve()), "--data", str(home), "--headless"],
        env=dict(os.environ, CUEPORCH_DESKTOP_PORT=str(port), CUEPORCH_SIMULATOR=source),
    )
    try:
        desktop = wait_for_desktop(proc, home)
        with sync_playwright() as pw:
            browser = chromium(pw)
            page = browser.new_page()
            page.goto(desktop)
            page.get_by_label("Institution name").fill("Upgrade from 0.2.0")
            page.get_by_role("button", name="Save setup").click()
            page.get_by_role("link", name="Open operator console").click()
            page.get_by_role("link", name="Move from Vimeo").click()
            page.get_by_role("button", name="Preview transfer").click()
            page.wait_for_function("document.querySelector('#status').textContent.startsWith('inventoried')")
            page.get_by_role("button", name="Transfer privately").click()
            page.wait_for_function("document.querySelector('#status').textContent.startsWith('transferred')")
            browser.close()
        keys = json.loads((home / "operator.json").read_text())
        auth = {"Authorization": "Bearer " + keys["operator"]}
        response = requests.get(base + "/api/screening-copies", headers=auth, timeout=10)
        response.raise_for_status()
        copy = next(work for work in response.json() if work["title"] == "Exhibition cut")
        response = requests.post(
            base + "/api/grants", headers=auth, timeout=10,
            json=dict(work=copy["work"], version=copy["version"], origin=venue,
                      starts=time.time() - 1, ends=time.time() + 900,
                      audience="Members", agreement="0.2.0 upgrade rehearsal"),
        )
        response.raise_for_status()
        grant = response.json()
        destination = "https://station.example/videos/embed/fixture"
        requests.put(
            base + "/api/routes/meeting/baseline", headers=auth, timeout=10,
            json=dict(revision="one", destination=destination, public_authorized=True),
        ).raise_for_status()
        requests.put(
            base + "/api/policy/moderation", headers=auth, timeout=10,
            json={"required": True},
        ).raise_for_status()
        requests.post(
            base + "/desktop/api/quit", timeout=10,
            headers={"Authorization": "Bearer " + desktop.split("#")[1]},
        ).raise_for_status()
        proc.wait(timeout=20)
        return dict(grant=grant, copy=copy, route="/embed/meeting/baseline", destination=destination)
    finally:
        if proc.poll() is None:
            import psutil

            for child in psutil.Process(proc.pid).children(recursive=True):
                child.terminate()
            proc.terminate()
            proc.wait()


class Museum(BaseHTTPRequestHandler):
    def do_GET(self):
        data = (
            '<iframe title="Screening" src="' + self.server.embed + '"></iframe>'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


with tempfile.TemporaryDirectory() as tmp, ExitStack() as setup:
    root = Path(tmp)
    push_fixture = None
    if args.review_push:
        from installed_review_push import check_review_push, push_receiver

        push_fixture = setup.enter_context(push_receiver(root))
    replacement = generate_replacement(root / "replacement.mp4")
    if not (args.analytics_only or args.discovery_only or args.audience_accounts_only or args.audio_tracks_only
            or args.team_only or args.cutover_only):
        editor_master = root / "editor-master.mp4"
        editor_fixture(editor_master)
        timeline_green, timeline_music = timeline_fixtures(root)
        review_update = generate_replacement(root / "review-update.mp4", width=192)
        caption_video = speech_video(args.caption_speech, root / "caption-speech.mp4") if args.caption_speech else None
    src = build_server(root / "fixture")
    source = serve(src)
    museum = ThreadingHTTPServer(("127.0.0.1", 0), Museum)
    venue = serve(museum)
    try:
        baselines = [None, "0.1.0"]
        if args.windows_baseline_setup:
            baselines.append("0.2.0")
        for version in baselines:
            if args.only and args.only != (version or "fresh"):
                continue
            target = "source" if args.source else "installed"
            print(f"Running {target} rehearsal ({version or 'fresh'})", flush=True)
            upgrade = version is not None
            home = root / ("upgrade-" + version if upgrade else "new")
            if version == "0.2.0":
                prior = prepare_previous_windows(home, source, venue)
            elif version == "0.1.0":
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("prepare_upgrade.py")),
                        str(args.baseline.resolve()),
                        str(home),
                        source,
                        venue,
                    ],
                    check=True,
                )
                prior = json.loads((root / "upgrade-proof-private.json").read_text())
            if upgrade:
                prior_hashes = {
                    f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                    for f in (home / "collection/objects").iterdir()
                }
                prior_keys = (home / "operator.json").read_bytes()
            if args.windows_setup:
                install_windows(args.windows_setup, home)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            analytics_env = {"CUEPORCH_ANALYTICS_PROXIES": PROXY_ADDRESS} if args.analytics_region_proxy else {}
            candidate_env = dict(
                os.environ,
                CUEPORCH_DESKTOP_PORT=str(port),
                CUEPORCH_OPERATOR_KEYS=json.dumps({"Second operator": "fixture-distinct-operator-key"}),
                CUEPORCH_SIMULATOR=source,
            ) | (push_fixture["env"] if push_fixture else {}) | analytics_env
            proc = start_candidate(home, candidate_env)
            try:
                desktop = wait_for_desktop(proc, home)
                with sync_playwright() as pw:
                    browser = chromium(pw)
                    page = browser.new_page()
                    page.goto(desktop)
                    setup_seconds = None
                    if not upgrade:
                        page.get_by_label("Institution name").fill("New station")
                        started = time.monotonic()
                        with page.expect_response(lambda event: event.url.endswith("/desktop/api/setup")) as setup_result:
                            page.get_by_role("button", name="Save setup").click()
                        response = setup_result.value
                        setup_seconds = round(time.monotonic() - started, 3)
                        if not response.ok:
                            service_log = (home / "service.log").read_text(encoding="utf8", errors="replace")
                            frames = [line for line in service_log.splitlines() if line.startswith(("  File ", "AssertionError", "Traceback"))]
                            raise AssertionError(f"Desktop setup returned {response.status}\n" + "\n".join(frames[-14:]))
                    page.get_by_role("link", name="Open operator console").click()
                    page.get_by_role("link", name="Move from Vimeo").click()
                    page.get_by_role("button", name="Preview transfer").click()
                    page.wait_for_function(
                        "document.querySelector('#status').textContent.startsWith('inventoried')"
                    )
                    assert page.locator("#rows tr").count() == 3
                    for _ in range(2):
                        page.get_by_role("button", name="Transfer privately").click()
                        page.wait_for_function(
                            "document.querySelector('#status').textContent.startsWith('transferred')"
                        )
                    keys = json.loads((home / "operator.json").read_text())
                    auth = {"Authorization": "Bearer " + keys["operator"]}
                    works = requests.get(base + "/api/library", headers=auth).json()
                    assert len(works) == 3 and len({w["id"] for w in works}) == 3
                    showcase_transfer = check_transferred_showcase(page, base, auth, works)
                    assert sum(w["quality"] == "owner file needed" for w in works) == 1
                    library_result = check_folders(page, base, auth, works)
                    copy = next(
                        w
                        for w in requests.get(
                            base + "/api/screening-copies", headers=auth
                        ).json()
                        if w["title"] == "Exhibition cut"
                    )
                    if upgrade:
                        assert (
                            prior["copy"] == copy
                            and prior_keys == (home / "operator.json").read_bytes()
                        )
                        assert all(
                            hashlib.sha256(
                                (home / "collection/objects" / k).read_bytes()
                            ).hexdigest()
                            == v
                            for k, v in prior_hashes.items()
                        )
                        assert (
                            requests.get(
                                base + prior["route"], allow_redirects=False
                            ).headers["Location"]
                            == prior["destination"]
                        )
                        assert requests.get(base + "/api/policy", headers=auth).json()[
                            "moderation"
                        ]
                        grant = prior["grant"]
                    else:
                        response = requests.post(
                            base + "/api/grants",
                            headers=auth,
                            json=dict(
                                work=copy["work"],
                                version=copy["version"],
                                origin=venue,
                                starts=time.time() - 1,
                                ends=time.time() + 900,
                                audience="viewers",
                                agreement="Rehearsal",
                            ),
                        )
                        response.raise_for_status()
                        grant = response.json()
                    url = base + "/api/grants/" + grant["id"] + "/session"
                    assert requests.post(url).status_code == 403
                    sess = requests.post(
                        url,
                        headers={
                            "Authorization": "Bearer " + grant["exhibitor_secret"]
                        },
                    )
                    sess.raise_for_status()
                    museum.embed = base + sess.json()["embed"]
                    page.goto(venue)
                    frame = page.frame_locator("iframe")
                    width = frame.locator("video").evaluate(
                        "async v=>{v.muted=true;await v.play();return v.videoWidth}"
                    )
                    assert width > 0
                    if args.team_only or args.cutover_only:
                        team = check_team_accounts(page, browser, base, auth, replacement, copy)
                        results.append(dict(upgrade=upgrade, baseline=version, inventory=3, missing_media=1,
                                            repeat_ids_unique=True, anonymous_denied=True, playback_width=width,
                                            setup_seconds=setup_seconds, team=team))
                        if args.cutover_only:
                            results[-1]["cutover"] = check_migrated_privacy(page, browser, base, auth, src, museum, venue)
                        browser.close()
                        shut_down(proc, base, desktop, home)
                        save_report()
                        continue
                    if args.discovery_only or args.audience_accounts_only:
                        discovery = check_discovery(page, browser, base, auth, replacement)
                        audience = check_audience_accounts(browser, base, auth, replacement) if args.audience_accounts_only else None
                        results.append(dict(upgrade=upgrade, baseline=version, inventory=3,
                                            missing_media=1, repeat_ids_unique=True, anonymous_denied=True,
                                            playback_width=width, setup_seconds=setup_seconds, discovery=discovery))
                        if audience is not None:
                            results[-1]["audience_accounts"] = audience
                            results[-1]["audience_collections"] = check_audience_collections(browser, base, auth, replacement)
                            results[-1]["audience_comments"] = check_audience_comments(browser, base, auth, replacement)
                        browser.close()
                        shut_down(proc, base, desktop, home)
                        save_report()
                        continue
                    if args.audio_tracks_only:
                        audio = check_audio_tracks(page, browser, base, auth, museum, venue, replacement)
                        results.append(dict(upgrade=upgrade, baseline=version, inventory=3,
                                            missing_media=1, repeat_ids_unique=True, anonymous_denied=True,
                                            playback_width=width, setup_seconds=setup_seconds, audio=audio))
                        browser.close()
                        shut_down(proc, base, desktop, home)
                        save_report()
                        continue
                    if args.analytics_only:
                        if args.analytics_captions:
                            imported = next(work["id"] for work in works if work["title"] == "Station meeting")
                            types = requests.get(f"{base}/api/library/work/{imported}/languages", headers=auth, timeout=10)
                            types.raise_for_status()
                            assert any(track["language"] == "en" and track["kind"] == "subtitles"
                                       for track in types.json()["tracks"])
                        advanced = check_advanced_analytics(page, browser, base, auth, museum, venue, replacement,
                                                            verify_region=args.analytics_region_proxy,
                                                            verify_bandwidth=args.analytics_bandwidth,
                                                            verify_unique=args.analytics_unique,
                                                            verify_captions=args.analytics_captions)
                        if args.analytics_captions:
                            advanced["caption_usage"]["vimeo_track_type_preserved"] = True
                        if args.analytics_community:
                            advanced["community"] = check_community_analytics(page, browser, base, auth, replacement,
                                                                            home / "institution.sqlite")
                        results.append(dict(upgrade=upgrade, baseline=version, inventory=3,
                                            missing_media=1, repeat_ids_unique=True, anonymous_denied=True,
                                            playback_width=width, setup_seconds=setup_seconds,
                                            advanced_analytics=advanced))
                        browser.close()
                        shut_down(proc, base, desktop, home)
                        save_report()
                        continue
                    versions_result = check_versions(
                        page, base, auth, copy, grant, venue, museum.embed, replacement,
                    )
                    review_result = check_review_links(page, base, auth, copy, review_update)
                    review_result.update(check_review_defaults(
                        page, base, auth, copy, keys["creator"], replacement,
                    ))
                    if push_fixture:
                        review_result["device_push"] = check_review_push(browser, base, auth, copy["work"], push_fixture)
                    card_result, download_url = check_cards(
                        page, base, auth, home / "institution.sqlite",
                        next(w for w in works if w["title"] == "Station meeting"),
                    )
                    upload_result = check_uploads(page, base, auth, replacement)
                    privacy_result = check_privacy(page, browser, base, auth, museum, venue)
                    privacy_defaults = check_privacy_defaults(page, browser, base, auth, replacement)
                    folder_privacy = check_folder_privacy(page, browser, base, auth, replacement)
                    details = check_video_details(page, browser, base, auth)
                    workspace = check_workspace(page, browser, base, auth, home / "institution.sqlite")
                    embed_builder = check_embed_builder(page, browser, base, auth)
                    appearance = check_appearance(page, browser, base, auth)
                    languages = check_languages(page, browser, base, auth, home / "collection")
                    generated = check_caption_generation(page, browser, base, auth, caption_video) if caption_video else None
                    chapters = check_chapters(
                        page, browser, base, auth, source, museum, venue, replacement,
                        next(work["id"] for work in works if work["title"] == "Station meeting"),
                    )
                    presentation = check_presentation(page, browser, base, auth, museum, venue, replacement)
                    ratings = check_ratings(page, browser, base, auth, replacement)
                    registration = check_registration(page, browser, base, auth, museum, venue, replacement)
                    analytics = check_analytics(page, browser, base, auth, museum, venue, replacement)
                    advanced_analytics = check_advanced_analytics(page, browser, base, auth, museum, venue, replacement,
                                                                  verify_region=args.analytics_region_proxy,
                                                                  verify_bandwidth=args.analytics_bandwidth,
                                                                  verify_unique=args.analytics_unique,
                                                                  verify_captions=args.analytics_captions)
                    editing = check_editing(page, browser, base, auth, home / "collection", editor_master)
                    recording = check_recording(pw, base, auth)
                    timeline = check_timeline(
                        page, browser, base, auth, editor_master, timeline_green, timeline_music,
                        home / "institution.sqlite",
                    )
                    showcases = check_showcases(page, browser, base, auth, museum, venue, editor_master)
                    event_calendar = check_event_calendar(page, base, auth)
                    event_chat = check_event_chat(page, browser, base, auth)
                    event_engagement = check_event_engagement(page, browser, base, auth)
                    trash_result = check_private(
                        page, base, auth, home / "institution.sqlite", copy["work"], grant, museum.embed,
                        download_url,
                    )
                    migrated_privacy = check_migrated_privacy(page, browser, base, auth, src, museum, venue)
                    details.update(check_imported_video_details(base, auth))
                    browser.close()
                    results.append(
                        dict(
                            upgrade=upgrade,
                            baseline=version,
                            inventory=3,
                            missing_media=1,
                            repeat_ids_unique=True,
                            playback_width=width,
                            anonymous_denied=True,
                            library=library_result,
                            card_actions=card_result,
                            video_versions=versions_result,
                            review_links=review_result,
                            recently_deleted=trash_result,
                            library_uploads=upload_result,
                            library_privacy=privacy_result,
                            privacy_defaults=privacy_defaults,
                            folder_privacy=folder_privacy,
                            migrated_privacy=migrated_privacy,
                            video_details=details, workspace=workspace, embed_builder=embed_builder, appearance=appearance,
                            languages=languages, generated_captions=generated, chapters=chapters,
                            presentation=presentation, ratings=ratings, registration=registration,
                            analytics=analytics,
                            advanced_analytics=advanced_analytics,
                            editing=editing,
                            recording=recording,
                            timeline=timeline,
                            showcases=showcases | showcase_transfer,
                            event_calendar=event_calendar,
                            event_chat=event_chat,
                            event_engagement=event_engagement,
                        )
                    )
                shut_down(proc, base, desktop, home)
                save_report()
            finally:
                if proc.poll() is None:
                    import psutil

                    for child in psutil.Process(proc.pid).children(recursive=True):
                        child.terminate()
                    proc.terminate()
                    proc.wait()
    finally:
        src.shutdown()
        src.server_close()
        museum.shutdown()
        museum.server_close()
