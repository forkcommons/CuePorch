"""Exercise the actual installed launcher on a new institution and on 0.1.0 data."""

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

p = argparse.ArgumentParser()
p.add_argument("launcher", type=Path)
p.add_argument("baseline", type=Path)
p.add_argument("report", type=Path)
p.add_argument("--review-push", action="store_true", help="Use the local encrypted review push receiver")
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
args = p.parse_args()
if args.windows_baseline_setup and not args.windows_setup:
    p.error("--windows-baseline-setup requires --windows-setup")
results = []


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


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
    for _ in range(200):
        if proc.poll() is not None:
            raise RuntimeError(f"Installed service exited with code {proc.returncode}")
        try:
            return json.loads(marker.read_text(encoding="utf8"))["url"]
        except (FileNotFoundError, PermissionError, json.JSONDecodeError):
            time.sleep(0.1)
    raise TimeoutError("The installed service did not publish a readable session file")


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
    review_update = generate_replacement(root / "review-update.mp4", width=192)
    src = build_server(root / "fixture")
    source = serve(src)
    museum = ThreadingHTTPServer(("127.0.0.1", 0), Museum)
    venue = serve(museum)
    try:
        baselines = [None, "0.1.0"]
        if args.windows_baseline_setup:
            baselines.append("0.2.0")
        for version in baselines:
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
            proc = subprocess.Popen(
                [str(args.launcher.resolve()), "--data", str(home), "--headless"],
                env=dict(
                    os.environ,
                    CUEPORCH_DESKTOP_PORT=str(port),
                    CUEPORCH_OPERATOR_KEYS=json.dumps({"Second operator": "fixture-distinct-operator-key"}),
                    CUEPORCH_SIMULATOR=source,
                ) | (push_fixture["env"] if push_fixture else {}),
            )
            try:
                desktop = wait_for_desktop(proc, home)
                with sync_playwright() as pw:
                    browser = chromium(pw)
                    page = browser.new_page()
                    page.goto(desktop)
                    if not upgrade:
                        page.get_by_label("Institution name").fill("New station")
                        with page.expect_response(lambda event: event.url.endswith("/desktop/api/setup")) as setup_result:
                            page.get_by_role("button", name="Save setup").click()
                        response = setup_result.value
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
                        )
                    )
                quit_response = requests.post(
                    base + "/desktop/api/quit",
                    headers={"Authorization": "Bearer " + desktop.split("#")[1]},
                )
                quit_response.raise_for_status()
                proc.wait(timeout=20)
                args.report.write_text(json.dumps(results, indent=2), encoding="utf8")
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
