"""Exercise the actual installed launcher on a new institution and on 0.1.0 data."""

import argparse, hashlib, json, os, socket, subprocess, sys, tempfile, threading, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
from playwright.sync_api import sync_playwright
from demo_source import build_server
from browser import chromium
from installed_library import check_folders

p = argparse.ArgumentParser()
p.add_argument("launcher", type=Path)
p.add_argument("baseline", type=Path)
p.add_argument("report", type=Path)
p.add_argument(
    "--windows-setup",
    type=Path,
    help="Install into the isolated launcher directory and verify the generated Start menu shortcut",
)
args = p.parse_args()
results = []


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


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
    src = build_server(root / "fixture")
    source = serve(src)
    museum = ThreadingHTTPServer(("127.0.0.1", 0), Museum)
    venue = serve(museum)
    try:
        for upgrade in (False, True):
            home = root / ("upgraded" if upgrade else "new")
            if upgrade:
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
                prior_hashes = {
                    f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                    for f in (home / "collection/objects").iterdir()
                }
                prior_keys = (home / "operator.json").read_bytes()
            if args.windows_setup:
                import platform

                assert platform.system() == "Windows"
                subprocess.run(
                    [
                        str(args.windows_setup.resolve()),
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
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            proc = subprocess.Popen(
                [str(args.launcher.resolve()), "--data", str(home), "--headless"],
                env=dict(
                    os.environ,
                    CUEPORCH_DESKTOP_PORT=str(port),
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
                    browser.close()
                    results.append(
                        dict(
                            upgrade=upgrade,
                            inventory=3,
                            missing_media=1,
                            repeat_ids_unique=True,
                            playback_width=width,
                            anonymous_denied=True,
                            library=library_result,
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
