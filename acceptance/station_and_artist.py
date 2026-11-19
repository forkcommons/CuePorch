"""Exercise a package emitted by CivicCast and a direct artist submission on CuePorch."""

import argparse, json, os, secrets, sys, tempfile, threading, time
from pathlib import Path
import requests
from civiccast.cable.package import build_cable_file_package
from playwright.sync_api import sync_playwright
from browser import chromium
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
from civiccast_destination import submit_package

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("peer")
p.add_argument("media", type=Path)
a = p.parse_args()
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    library = root / "collection"
    (library / "objects").mkdir(parents=True)
    (library / "manifest.json").write_text(
        json.dumps(dict(institution="Station", schema=1, works=[]))
    )
    os.environ["CUEPORCH_PEERTUBE_ORIGIN"] = a.peer
    station = secrets.token_hex(24)
    creator = secrets.token_hex(24)
    operator = secrets.token_hex(24)
    os.environ.update(CUEPORCH_STATION_KEY=station, CUEPORCH_CREATOR_KEY=creator)
    server = make_server(
        "127.0.0.1",
        0,
        create_app(library, root / "state.sqlite", operator, "Station"),
        threaded=True,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    auth = {"Authorization": "Bearer " + operator}

    def package(label):
        cap = root / "meeting.vtt"
        cap.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n" + label + "\n")
        return build_cable_file_package(
            asset_id="station-episode-27",
            title="Community episode",
            media_path=a.media,
            caption_path=cap,
            output_dir=root / "CivicCast",
            portal_url=None,
        ).zip_path

    def until(uid, status):
        for _ in range(180):
            jobs = requests.get(base + "/api/delivery", headers=auth).json()
            job = next(j for j in jobs if j["id"] == uid)
            if job["status"] == status:
                return job
            if job["status"] == "failed":
                raise RuntimeError("Station job failed: " + str(job["error"]))
            time.sleep(0.5)
        raise RuntimeError("Station delivery did not reach " + status)

    try:
        zipfile = package("First caption")
        requests.put(
            base + "/api/policy/caption", headers=auth, json={"required": True}
        ).raise_for_status()
        item = submit_package(base, station, zipfile, True)
        until(item["job"], "waiting")
        assert not requests.get(base + "/api/delivery", headers=auth).json()[0][
            "remote"
        ]
        requests.put(
            base + "/api/policy/caption", headers=auth, json={"required": False}
        ).raise_for_status()
        first = until(item["job"], "playable")
        repeated = submit_package(base, station, zipfile, True)
        assert repeated["job"] == item["job"]
        correction = submit_package(base, station, package("Corrected caption"), True)
        second = until(correction["job"], "playable")
        assert second["remote"] == first["remote"]
        assert (
            requests.get(
                base + "/embed/work/" + item["work"], allow_redirects=False
            ).status_code
            == 302
        )
        for public in (False, True):
            reply = requests.post(
                base + "/api/delivery/" + item["work"],
                headers=auth,
                json={"public": public},
            )
            reply.raise_for_status()
            delivery = until(reply.json()["job"], "playable")
            assert delivery["remote"] == second["remote"]
            observed = requests.get(a.peer + "/api/v1/videos/" + delivery["remote"])
            assert observed.status_code == (200 if public else 401), (
                observed.status_code
            )
        with sync_playwright() as pw:
            browser = chromium(pw)
            page = browser.new_page()
            page.goto(base + "/submit")
            page.locator("#access").fill(creator)
            page.locator("[name=title]").fill("Artist cut")
            page.locator("[name=credits]").fill("Fixture creator")
            page.locator("[name=master]").set_input_files(a.media)
            page.locator("[name=trailer]").set_input_files(a.media)
            page.locator("[name=public_trailer]").check()
            page.locator("[name=caption]").set_input_files(
                dict(
                    name="artist.vtt",
                    mimeType="text/vtt",
                    buffer=b"WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nArtist film caption.\n",
                )
            )
            with page.expect_response("**/api/submissions", timeout=120000) as response:
                page.get_by_role("button", name="Submit", exact=True).click()
            submitted = response.value.json()
            assert response.value.status == 201, submitted
            until(submitted["delivery"], "playable")
            trailer = until(submitted["trailer_delivery"], "playable")
            assert requests.get(a.peer + "/api/v1/videos/" + trailer["remote"]).ok
            browser.close()
        catalog = json.loads((library / "manifest.json").read_text())
        arts = next(w for w in catalog["works"] if w["title"] == "Artist cut")
        assert {f["role"] for f in arts["files"]} == {"original", "playback", "caption"}
        assert arts["source_privacy"] == "private" and len(catalog["works"]) == 3
        assert any(w.get("related_work") == arts["id"] for w in catalog["works"])
        derivative = next(f for f in arts["files"] if f["role"] == "playback")
        grant = requests.post(
            base + "/api/grants",
            headers=auth,
            json=dict(
                work=arts["id"],
                version=derivative["sha256"],
                origin="https://museum.example",
                starts=time.time() - 1,
                ends=time.time() + 900,
                agreement="Preview loan",
                audience="Members",
            ),
        )
        grant.raise_for_status()
        requests.delete(
            base + "/api/grants/" + grant.json()["id"], headers=auth
        ).raise_for_status()
        assert requests.get(a.peer + "/api/v1/videos/" + trailer["remote"]).ok
        assert "Artist cut (trailer)" in requests.get(base + "/").text
        print(
            json.dumps(
                dict(
                    station_identity=item["work"],
                    correction_preserved_remote=second["remote"],
                    upstream="CivicCast cable package builder",
                    approval_hold_released=True,
                    private_then_public_again_same_video=True,
                    public_trailer_separate_from_private_master=True,
                    distinct_works=3,
                    private_artist_master_and_derivative=True,
                ),
                indent=2,
            )
        )
    finally:
        server.shutdown()
        thread.join()
