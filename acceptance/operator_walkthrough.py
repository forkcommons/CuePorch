"""Drive the normal migration screens on a three-work rehearsal installation."""

import time
import requests
from playwright.sync_api import sync_playwright
from browser import chromium


def walkthrough(site, keys, started):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    with sync_playwright() as pw:
        b = chromium(pw)
        page = b.new_page()
        page.goto(site + "/operator")
        page.get_by_role("link", name="Move from Vimeo").click()
        page.wait_for_function("typeof document.querySelector('#inventory')?.onclick === 'function'")
        page.locator("#key").fill(keys["operator"])
        with page.expect_response(
            lambda event: event.url == site + "/api/migration" and event.request.method == "POST",
            timeout=15000,
        ) as submitted:
            page.get_by_role("button", name="Preview transfer").click()
        assert submitted.value.ok, f"Inventory request returned HTTP {submitted.value.status}"
        page.wait_for_function(
            "/^(inventoried|failed)/.test(document.getElementById('status').textContent)"
        )
        status = page.locator("#status").inner_text()
        assert status.startswith("inventoried"), status
        assert page.locator("#rows tr").count() == 3
        start_transfer = time.monotonic()
        page.get_by_role("button", name="Transfer privately").click()
        page.wait_for_function(
            "document.getElementById('status').textContent.startsWith('transferred')"
        )
        transfer = time.monotonic() - start_transfer
        for _ in range(200):
            jobs = requests.get(site + "/api/delivery", headers=auth).json()
            if any(j["status"] == "failed" for j in jobs):
                raise RuntimeError(
                    "Private delivery failed: "
                    + str([(j["status"], j["error"]) for j in jobs])
                )
            if len(jobs) == 2 and all(j["status"] == "playable" for j in jobs):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Private delivery timed out")
        page.goto(site + "/operator/library")
        page.wait_for_function("typeof document.querySelector('#load')?.onclick === 'function'")
        page.locator("#key").fill(keys["operator"])
        page.get_by_role("button", name="Load library").click()
        page.wait_for_function("document.querySelectorAll('#items tr').length===3")
        assert page.get_by_role("button", name="Publish publicly").count() == 1
        page.get_by_role("button", name="Publish eligible Vimeo imports").click()
        page.get_by_text("Queued eligible imports: 1", exact=True).wait_for()
        for _ in range(100):
            jobs = requests.get(site + "/api/delivery", headers=auth).json()
            if any(j["want_public"] and j["status"] == "playable" for j in jobs):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Public delivery timed out")
        page.get_by_role("button", name="Load library").click()
        page.get_by_role("link", name="Watch", exact=True).click()
        frame = page.frame_locator("iframe")
        frame.get_by_role("button", name="Play Video", exact=True).click(timeout=30000)
        video = frame.locator("video").first
        assert video.evaluate("async v=>{await v.play();return v.videoWidth}") > 0
        first = time.monotonic() - started
        before = {
            j["id"]: j["remote"]
            for j in requests.get(site + "/api/delivery", headers=auth).json()
        }
        response = requests.post(
            site + "/api/migration", headers=auth, json={"dry_run": False}
        )
        response.raise_for_status()
        for _ in range(120):
            result = requests.get(site + "/api/migration", headers=auth).json()["state"]
            if result == "transferred":
                break
            if result == "failed":
                raise RuntimeError("Repeat transfer failed")
            time.sleep(0.5)
        else:
            raise RuntimeError("Repeat transfer timed out")
        after = {
            j["id"]: j["remote"]
            for j in requests.get(site + "/api/delivery", headers=auth).json()
        }
        assert before == after, (
            "A repeated transfer changed the existing publication jobs"
        )
        b.close()
    return transfer, first
