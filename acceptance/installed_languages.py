"""Play installed subtitle edits from both the regular player and an existing review."""

import hashlib
import json
import time
from urllib.parse import urljoin

import requests
from playwright.sync_api import expect
from installed_versions import wait_delivery


def check_languages(page, browser, base, auth, collection):
    title = "Institutional video details"
    items = requests.get(base + "/api/library", headers=auth, timeout=10).json()
    uid = next(work["id"] for work in items if work["title"] == title)
    work_api = f"{base}/api/library/work/{uid}"
    endpoint = work_api + "/languages"
    review = next(item for item in requests.get(work_api + "/review-links", headers=auth, timeout=10).json()
                  if item["name"] == "Workspace reviewer")
    cut_key = requests.get(work_api + "/versions", headers=auth, timeout=10).json()[0]["key"]
    page.goto(f"{base}/operator/videos/{uid}")
    page.get_by_role("link", name="Languages and subtitles").click()
    expect(page.get_by_role("heading", name="Languages for " + title)).to_be_visible()
    assert requests.get(endpoint, timeout=10).status_code == 403
    page.get_by_label("Original language", exact=True).fill("en")
    page.get_by_role("button", name="Set original language").click()
    expect(page.get_by_role("status")).to_have_text("Original language saved.")
    page.get_by_label("Subtitle language", exact=True).fill("es")
    page.get_by_label("Subtitle file", exact=True).set_input_files(
        {
            "name": "spanish.srt", "mimeType": "application/x-subrip",
            "buffer": b"1\r\n00:00:00,100 --> 00:00:00,900\r\nUna frase inicial\r\n\r\n"
                      b"2\r\n00:00:01,000 --> 00:00:02,000\r\nUna segunda frase\r\n",
        }
    )
    page.get_by_role("button", name="Upload subtitles").click()
    expect(page.get_by_role("status")).to_have_text("Subtitles uploaded.")
    old = requests.get(endpoint + "/es", headers=auth, timeout=10).json()
    page.get_by_label("Search transcript").fill("inicial")
    expect(page.locator("#cueRows > li")).to_have_count(1)
    page.get_by_label("Start", exact=True).fill("00:00:00.200")
    page.get_by_label("Cue text", exact=True).fill("Una frase corregida")
    page.get_by_role("button", name="Save subtitles").click()
    expect(page.get_by_role("status")).to_have_text("Subtitles saved.")
    assert requests.put(endpoint + "/es", headers=auth, json=old, timeout=10).status_code == 409
    response = requests.get(endpoint + "/es?download=1", headers=auth, timeout=10)
    response.raise_for_status()
    assert b"00:00:00.200 --> 00:00:00.900\nUna frase corregida" in response.content
    assert "attachment" in response.headers["Content-Disposition"]
    manifest = json.loads((collection / "manifest.json").read_text(encoding="utf8"))
    portable = next(work for work in manifest["works"] if work["id"] == uid)["language_edits"][cut_key]
    assert portable["original_language"] == "en" and len(portable["tracks"]) == 1
    digest = portable["tracks"][0]["sha256"]
    assert hashlib.sha256((collection / "objects" / digest).read_bytes()).hexdigest() == digest
    assert requests.get(work_api + "/versions", headers=auth, timeout=10).json()[0]["key"] == cut_key

    guest = browser.new_context()
    try:
        viewer = guest.new_page()
        viewer.goto(base + "/watch/work/" + uid)
        expect(viewer.get_by_role("combobox", name="Captions", exact=True)).to_be_visible()
        viewer.get_by_role("combobox", name="Captions", exact=True).select_option("0")
        viewer.wait_for_function("document.querySelector('video').textTracks[0].cues?.length === 2")
        assert viewer.locator("video").evaluate("v=>v.textTracks[0].cues[0].text") == "Una frase corregida"
        assert viewer.locator("video").evaluate("async v=>{v.muted=true;await v.play();return v.videoWidth}") == 256
        viewer.goto(base + review["path"] + "?version=" + cut_key)
        expect(viewer.locator("#reviewStatus")).to_have_text("Approved")
        src = viewer.locator("video track").get_attribute("src")
        assert src and cut_key in src
        attachment = viewer.request.get(base + src)
        assert attachment.ok and "Una frase corregida" in attachment.text()
    finally:
        guest.close()
    return dict(srt_imported_as_vtt=True, original_language_saved=True, search_and_cue_edit=True,
                stale_save_rejected=True, content_addressed_snapshot=True, decoded_browser_cue=True,
                same_review_and_editorial_status=True)


def check_hosted_languages(base, origin, keys):
    auth = {"Authorization": "Bearer " + keys["operator"]}
    delivery = base + "/api/delivery"
    active = next(job for job in requests.get(delivery, headers=auth, timeout=10).json()
                  if job["want_public"] and job["status"] == "playable" and job["kind"] == "work")
    uid, remote = active["work"], active["remote"]
    endpoint = f"{base}/api/library/work/{uid}"
    language_api = endpoint + "/languages"
    cut = requests.get(language_api, headers=auth, timeout=10).json()["version"]
    policy = base + "/api/policy/caption"
    hosted = origin.rstrip("/") + "/api/v1/videos/" + remote

    def english():
        response = requests.get(hosted + "/captions", timeout=10)
        response.raise_for_status()
        entry = next(caption for caption in response.json()["data"] if caption["language"]["id"] == "en")
        return requests.get(urljoin(origin + "/", entry.get("fileUrl") or entry["captionPath"]), timeout=10).text

    original = english()
    requests.put(policy, headers=auth, json={"required": True}, timeout=10).raise_for_status()
    caption = b"WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nCuePorch hosted correction\n"
    try:
        response = requests.post(language_api, headers=auth, data={"language": "en", "version": cut},
                                 files={"track": ("correction.vtt", caption, "text/vtt")}, timeout=12)
        response.raise_for_status()
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            latest = next(job for job in requests.get(delivery, headers=auth, timeout=10).json() if job["work"] == uid)
            if latest["id"] != active["id"] and latest["status"] == "waiting":
                break
            if latest["status"] == "failed":
                raise AssertionError(f"Hosted subtitle update failed: {latest['error']}")
            time.sleep(.4)
        else:
            raise AssertionError("The hosted caption edit did not reach the configured approval gate")
        assert english() == original
    finally:
        requests.put(policy, headers=auth, json={"required": False}, timeout=10).raise_for_status()

    wait_delivery(base, auth, uid, active["id"], remote)
    assert "CuePorch hosted correction" in english()
    identity = requests.get(hosted, timeout=10).json()
    assert identity["uuid"] == remote and identity["duration"] == 4
    assert requests.get(language_api, headers=auth, timeout=10).json()["version"] == cut
    return dict(caption_gate_retained_prior_remote_track=True, disabling_gate_delivered_corrected_track=True,
                same_media_and_peer_id=True)
