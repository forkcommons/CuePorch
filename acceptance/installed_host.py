"""Use the installed desktop to connect a real host and publish an eligible Vimeo import."""

import argparse, json, os, socket, subprocess, tempfile, threading, time
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright
from browser import chromium
from demo_source import build_server
from operator_walkthrough import walkthrough
from installed_trash import check_hosted
import psutil

p = argparse.ArgumentParser()
p.add_argument("launcher", type=Path)
p.add_argument("origin")
p.add_argument("report", type=Path)
a = p.parse_args()
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    home = root / "data"
    with build_server(root / "fixture") as src:
        threading.Thread(target=src.serve_forever, daemon=True).start()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(
            os.environ,
            CUEPORCH_DESKTOP_PORT=str(port),
            CUEPORCH_SIMULATOR=f"http://127.0.0.1:{src.server_port}",
        )
        user = env.pop("PEERTUBE_USERNAME")
        password = env.pop("PEERTUBE_PASSWORD")
        env.pop("CUEPORCH_PEERTUBE_ORIGIN", None)
        started = time.monotonic()
        proc = subprocess.Popen(
            [str(a.launcher.resolve()), "--data", str(home), "--headless"], env=env
        )
        try:
            for _ in range(300):
                if (home / "desktop-session.json").exists():
                    break
                if proc.poll() is not None:
                    raise RuntimeError("Desktop startup failed")
                time.sleep(0.1)
            url = json.loads((home / "desktop-session.json").read_text())["url"]
            base = url.split("/desktop")[0]
            with sync_playwright() as pw:
                browser = chromium(pw)
                pg = browser.new_page()
                pg.goto(url)
                pg.get_by_label("Institution name").fill("Hosted rehearsal")
                pg.locator("summary").click()
                pg.get_by_label("Video host address").fill(a.origin)
                pg.get_by_label("Publishing account").fill(user)
                pg.get_by_label("Password", exact=True).fill(password)
                pg.get_by_role("button", name="Save setup").click()
                pg.get_by_role("link", name="Open operator console").wait_for(
                    timeout=45000
                )
                browser.close()
            keys = json.loads((home / "operator.json").read_text())
            transfer, first = walkthrough(base, keys, started)
            trash_result = check_hosted(base, a.origin, keys)
            response = requests.post(
                base + "/desktop/api/quit",
                headers={"Authorization": "Bearer " + url.split("#")[1]},
            )
            response.raise_for_status()
            proc.wait(timeout=20)
            a.report.write_text(
                json.dumps(
                    dict(
                        real_peertube=True,
                        source="three-item HTTP simulator",
                        inventory=3,
                        private_deliveries=2,
                        public_eligible=1,
                        first_embed_seconds=round(first, 2),
                        transfer_seconds=round(transfer, 2),
                        repeat_preserved_job_and_video_ids=True,
                        recently_deleted=trash_result,
                    ),
                    indent=2,
                )
            )
        finally:
            if proc.poll() is None:
                for child in psutil.Process(proc.pid).children(recursive=True):
                    child.terminate()
                proc.terminate()
                proc.wait()
            src.shutdown()
