"""Exercise the actual installed launcher on a new institution and on 0.1.0 data."""

import argparse, hashlib, json, os, socket, subprocess, sys, tempfile, threading, time
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
from installed_review_links import check_review_links

p = argparse.ArgumentParser()
p.add_argument("launcher", type=Path)
p.add_argument("baseline", type=Path)
p.add_argument("report", type=Path)
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
        marker = home / "desktop-session.json"
        for _ in range(200):
            if proc.poll() is not None:
                raise RuntimeError("Released 0.2.0 service exited during setup")
            if marker.exists():
                break
            time.sleep(0.1)
        desktop = json.loads(marker.read_text())["url"]
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


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    replacement = generate_replacement(root / "replacement.mp4")
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
                ),
            )
            try:
                for _ in range(200):
                    if proc.poll() is not None:
                        raise RuntimeError(
                            "Installed service exited: "
                            + (home / "service.log").read_text(errors="replace")[-1200:]
                        )
                    if (home / "desktop-session.json").exists():
                        break
                    time.sleep(0.1)
                desktop = json.loads((home / "desktop-session.json").read_text())["url"]
                with sync_playwright() as pw:
                    browser = chromium(pw)
                    page = browser.new_page()
                    page.goto(desktop)
                    if not upgrade:
                        page.get_by_label("Institution name").fill("New station")
                        page.get_by_role("button", name="Save setup").click()
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
                    review_result = check_review_links(page, base, auth, copy)
                    card_result, download_url = check_cards(
                        page, base, auth, home / "institution.sqlite",
                        next(w for w in works if w["title"] == "Station meeting"),
                    )
                    trash_result = check_private(
                        page, base, auth, home / "institution.sqlite", copy["work"], grant, museum.embed,
                        download_url,
                    )
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
                        )
                    )
                quit_response = requests.post(
                    base + "/desktop/api/quit",
                    headers={"Authorization": "Bearer " + desktop.split("#")[1]},
                )
                quit_response.raise_for_status()
                proc.wait(timeout=20)
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
args.report.write_text(json.dumps(results, indent=2))
