"""Exercise Page selection and the Meta Graph video upload contract in a source browser."""

from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from flask import Flask, Response, redirect, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker
from youtube_rtmps_fixture import check_destination_rtmps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import facebook_upload as facebook
import facebook_live
import facebook_live_job
from institution import create_app
import social_publication


class FacebookPublication(unittest.TestCase):
    def test_selected_page_posts_after_restart_and_recovers_from_rate_limit(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "Porch arts premiere.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=640x360:rate=24", "-t", "5", "-c:v", "libx264", "-preset", "ultrafast",
                            "-crf", "18", "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            video = clip.read_bytes()
            middle = len(video) // 2
            digest = hashlib.sha256(video).hexdigest()
            remote = Flask("facebook-fixture")
            provider = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            origin = f"http://127.0.0.1:{provider.server_port}"
            service = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/facebook/callback"
            live_callback = base + "/integration/oauth/facebook-live/callback"
            profile_callback = base + "/integration/oauth/facebook-profile-live/callback"
            database = root / "workspace.sqlite"
            chunks = {}
            posts = []
            live_writes = []
            live_now_writes = []
            profile_writes = []
            exchanges = []
            checkpoints = dict(rate=0, interrupted=False, live_permission=False)
            operator = secrets.token_urlsafe(32)

            cleanup.enter_context(patch.multiple(facebook, OAUTH=replace(
                facebook.OAUTH, authorization_url=origin + "/oauth/authorize", token_url=origin + "/graph/oauth/access_token",
            ), GRAPH=origin + "/graph", MEDIA=origin + "/media"))
            cleanup.enter_context(patch.object(facebook_live, "OAUTH", replace(
                facebook_live.OAUTH, authorization_url=origin + "/oauth/authorize",
                token_url=origin + "/graph/oauth/access_token",
            )))
            cleanup.enter_context(patch.object(facebook_live, "PROFILE_OAUTH", replace(
                facebook_live.PROFILE_OAUTH, authorization_url=origin + "/oauth/authorize",
                token_url=origin + "/graph/oauth/access_token",
            )))
            cleanup.enter_context(patch.object(social_publication, "RETRY_SECONDS", (3, 3)))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_PUBLIC_URL": "", "CUEPORCH_FACEBOOK_CLIENT_ID": "cueporch",
                "CUEPORCH_FACEBOOK_CLIENT_SECRET": "fixture-secret",
                "CUEPORCH_FACEBOOK_LIVE_CLIENT_ID": "cueporch",
                "CUEPORCH_FACEBOOK_LIVE_CLIENT_SECRET": "fixture-secret",
                "CUEPORCH_FACEBOOK_PROFILE_LIVE_CLIENT_ID": "cueporch",
                "CUEPORCH_FACEBOOK_PROFILE_LIVE_CLIENT_SECRET": "fixture-secret",
            }))

            def start_app():
                with patch("institution.library_worker.start", return_value=lambda: None):
                    app = create_app(root, database, operator, "station")
                stops = [stop for name, stop in app.extensions.items() if name.startswith("stop_")]

                def stop_app():
                    while stops:
                        stops.pop()()

                cleanup.callback(stop_app)
                service.app = app
                return stop_app

            stop = start_app()

            @remote.get("/oauth/authorize")
            def authorize():
                redirect_uri = request.args["redirect_uri"]
                assert request.args["client_id"] == "cueporch" and redirect_uri in (callback, live_callback, profile_callback)
                config = (facebook_live.PROFILE_OAUTH if redirect_uri == profile_callback else
                          facebook_live.OAUTH if redirect_uri == live_callback else facebook.OAUTH)
                assert set(request.args["scope"].split()) == set(config.scope.split())
                return redirect(redirect_uri + "?" + urlencode(dict(state=request.args["state"], code="facebook-code")))

            @remote.get("/graph/oauth/access_token")
            def token():
                assert request.args["client_secret"] == "fixture-secret" and request.args["client_id"] == "cueporch"
                if request.args.get("grant_type") == "fb_exchange_token":
                    assert request.args["fb_exchange_token"] == "fixture-short"
                    exchanges.append("long")
                    return dict(access_token="fixture-long", expires_in=5184000)
                assert request.args["code"] == "facebook-code" and request.args["redirect_uri"] in (callback, live_callback, profile_callback)
                exchanges.append("short")
                return dict(access_token="fixture-short", expires_in=1000)

            def authenticated(token):
                assert request.headers["Authorization"] == "Bearer " + token
                assert request.args["appsecret_proof"] == hmac.digest(b"fixture-secret", token.encode(), "sha256").hex()
                assert "access_token" not in request.args and "access_token" not in request.form

            @remote.get("/graph/me/permissions")
            def permissions():
                authenticated("fixture-long")
                state = "granted" if checkpoints["live_permission"] else "declined"
                return {"data": [{"permission": "publish_video", "status": state}]}

            @remote.get("/graph/me")
            def me():
                authenticated("fixture-long")
                return {"id": "707", "name": "Porch Artist"}

            @remote.get("/graph/me/accounts")
            def pages():
                authenticated("fixture-long")
                if request.args.get("after") == "second-page":
                    return dict(data=[dict(id="202", name="Porch Arts", access_token="fixture-arts",
                                           tasks=["CREATE_CONTENT"])])
                assert request.args.get("after") is None
                return dict(data=[
                    dict(id="101", name="Porch Station", access_token="fixture-station", tasks=["CREATE_CONTENT"]),
                    dict(id="303", name="Read Only", access_token="fixture-read", tasks=["ANALYZE"]),
                ], paging=dict(next="https://invalid.example/untrusted/?access_token=never-forward",
                               cursors=dict(after="second-page")))

            @remote.post("/graph/<page_id>/live_videos")
            def prepare_live(page_id):
                if page_id == "707":
                    authenticated("fixture-long")
                    profile_writes.append(request.get_json())
                    return dict(id=str(1706 + len(profile_writes)), secure_stream_url=facebook_live.INGEST_SERVER + "/fixture-facebook-profile-key")
                assert page_id in {"101", "202"}
                authenticated("fixture-station" if page_id == "101" else "fixture-arts")
                body = request.get_json()
                if body["status"] == "LIVE_NOW":
                    assert body == {"title": "Facebook Live rehearsal", "status": "LIVE_NOW",
                                    "stop_on_delete_stream": True}
                    live_now_writes.append(page_id)
                    return dict(id=str(901 + len(live_now_writes)),
                                secure_stream_url="rtmps://live-api-s.facebook.com:443/rtmp/fixture-facebook-auto")
                live_writes.append(page_id)
                assert set(body) == {"title", "status", "stop_on_delete_stream", "event_params"}
                assert body["title"] == "Facebook Live rehearsal"
                assert body["status"] == "SCHEDULED_UNPUBLISHED" and body["stop_on_delete_stream"] is True
                assert type(body["event_params"]["start_time"]) is int
                assert body["event_params"]["start_time"] > datetime.now(timezone.utc).timestamp()
                if page_id == "202":
                    return {"error": "fixture missing confirmation"}, 503
                return dict(id="901", secure_stream_url="rtmps://live-api-s.facebook.com:443/rtmp/fixture-facebook-live-secret")

            @remote.post("/media/<page_id>/videos")
            def transfer(page_id):
                assert page_id == "202"
                authenticated("fixture-arts")
                phase = request.form["upload_phase"]
                if phase == "start":
                    assert request.form["published"] == "false" and int(request.form["file_size"]) == len(video)
                    assert not chunks
                    return dict(upload_session_id="5001", video_id="6002", start_offset="0", end_offset=str(middle))
                assert request.form["upload_session_id"] == "5001"
                if phase == "transfer":
                    offset = int(request.form["start_offset"])
                    data = request.files["video_file_chunk"].read()
                    assert data == (video[:middle] if offset == 0 else video[middle:])
                    if offset == 0:
                        assert not chunks
                        chunks[0] = data
                        return dict(start_offset=str(middle), end_offset=str(len(video)))
                    assert offset == middle and 0 in chunks
                    if not checkpoints["interrupted"]:
                        checkpoints["interrupted"] = True
                        return "transfer temporarily unavailable", 503
                    chunks[middle] = data
                    return dict(start_offset=str(len(video)), end_offset=str(len(video)))
                assert phase == "finish" and request.form["published"] == "true" and request.form["secret"] == "false"
                assert request.form["title"] == clip.stem and request.form["description"] == "An arts screening."
                assert list(chunks) == [0, middle]
                checkpoints["rate"] += 1
                if checkpoints["rate"] == 1:
                    return dict(error=dict(code=613)), 400, {"Retry-After": "4"}
                posts.append(page_id)
                return dict(success=True)

            @remote.get("/playback")
            def playback():
                if not posts:
                    return "", 404
                return Response(b"".join(chunks.values()), mimetype="video/mp4")

            cleanup.enter_context(service_worker(provider))
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                api = page.context.request
                auth = {"Authorization": "Bearer " + operator}
                page.goto(base + "/operator/integrations")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open integrations").click()
                page.get_by_role("button", name="Connect Facebook Pages", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("Facebook Pages is connected.", timeout=15000)
                self.assertEqual(["short", "long"], exchanges)
                with closing(sqlite3.connect(database)) as db:
                    token_data, = db.execute("SELECT token FROM workspace_integration_connection WHERE provider='facebook'").fetchone()
                    self.assertNotIn(b"fixture-arts", token_data)

                upload = api.post(base + "/api/library/uploads", headers=auth, multipart={
                    "upload_id": str(uuid4()),
                    "file": {"name": clip.name, "mimeType": "video/mp4", "buffer": video},
                }, timeout=30000)
                self.assertEqual(201, upload.status, upload.text())
                uid = upload.json()["id"]
                endpoint = base + f"/api/library/work/{uid}/social/facebook"
                self.assertEqual(403, api.get(endpoint).status)
                page.goto(base + f"/operator/videos/{uid}")
                page.get_by_role("link", name="Publish to Facebook").click()
                form = page.locator("#socialForm")
                expect(form).to_be_visible()
                selector = form.get_by_label("Facebook Page", exact=True)
                expect(selector.locator("option")).to_have_text(["Choose a Page", "Porch Station", "Porch Arts"])
                self.assertNotIn("fixture-", api.get(endpoint, headers=auth).text())
                consent = form.get_by_label("Publish a public video on this Facebook Page")
                expect(consent).not_to_be_checked()
                invalid = api.post(endpoint, headers=auth | {"X-CuePorch-Social": "1"}, data={
                    "id": str(uuid4()), "title": clip.stem, "visibility": "public", "company": "303",
                    "cut": api.get(endpoint, headers=auth).json()["cut"],
                })
                self.assertEqual(400, invalid.status)
                selector.select_option("202")
                consent.check()
                form.get_by_label("Post text").fill("An arts screening.")
                form.get_by_role("button", name="Post to Facebook").click()

                deadline = monotonic() + 15
                checkpoint = None
                while monotonic() < deadline:
                    with closing(sqlite3.connect(database)) as db:
                        checkpoint = db.execute("SELECT session,offset,attempts FROM social_publication").fetchone()
                    if checkpoint and checkpoint[2]:
                        break
                    sleep(.05)
                self.assertEqual((middle, 1), checkpoint[1:])
                self.assertNotIn(b"5001", checkpoint[0])
                self.assertEqual({0}, chunks.keys())
                self.assertEqual([], posts)
                stop()
                start_app()
                page.reload()
                deadline = monotonic() + 15
                while checkpoints["rate"] == 0 and monotonic() < deadline:
                    sleep(.05)
                self.assertEqual(1, checkpoints["rate"])
                self.assertTrue(api.put(base + "/api/policy/publication", headers=auth, data={"required": True}).ok)
                expect(page.locator("#socialJobs")).to_contain_text("waiting for publication approval")
                waiting = api.get(endpoint, headers=auth).json()["jobs"][0]
                self.assertEqual([], posts)
                self.assertTrue(api.post(base + f"/api/actions/{waiting['id']}/decision", headers=auth,
                                        data={"decision": "approve"}).ok)
                expect(page.locator("#socialJobs").get_by_role("link", name="Open on Facebook Pages")).to_have_attribute(
                    "href", "https://www.facebook.com/202/videos/6002/", timeout=15000)
                self.assertEqual(["202"], posts)
                self.assertEqual(digest, hashlib.sha256(b"".join(chunks.values())).hexdigest())
                player = browser.new_page()
                player.goto(origin + "/playback")
                self.assertEqual(640, player.locator("video").evaluate("async v => {v.muted = true; await v.play(); return v.videoWidth}"))
                page.goto(base + "/operator/integrations")
                catalog = base + "/api/integrations/facebook-live/pages"
                self.assertEqual(403, api.get(catalog).status)
                self.assertEqual(409, api.get(catalog, headers=auth).status)
                with page.expect_response(lambda response: response.url.startswith(live_callback)) as response:
                    page.get_by_role("button", name="Connect Facebook Live", exact=True).click()
                self.assertEqual(403, response.value.status)
                self.assertEqual(409, api.get(catalog, headers=auth).status)
                page.goto(base + "/operator/integrations")
                checkpoints["live_permission"] = True
                page.get_by_role("button", name="Connect Facebook Live", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("Facebook Live is connected.", timeout=15000)
                page.get_by_role("button", name="Show Facebook Live Pages", exact=True).click()
                expect(page.get_by_text("Porch Station", exact=True)).to_be_visible()
                expect(page.get_by_text("Porch Arts", exact=True)).to_be_visible()
                listed = api.get(catalog, headers=auth)
                self.assertEqual([{"id": "101", "name": "Porch Station"}, {"id": "202", "name": "Porch Arts"}],
                                 listed.json()["items"])
                self.assertNotIn("fixture-", listed.text() + page.content())

                api.put(base + "/api/policy/publication", headers=auth, data={"required": False})
                starts = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(second=0, microsecond=0)
                event = dict(title="Facebook Live rehearsal", slug="facebook-live-rehearsal", visibility="public",
                             type="live", latency="standard", save_replay=False,
                             schedule=dict(timezone="UTC", repeat="days", count=5,
                                           start=starts.strftime("%Y-%m-%dT%H:%M"),
                                           end=(starts + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")))
                response = api.post(base + "/api/live-events", headers=auth, data=event)
                self.assertTrue(response.ok, response.text())
                created = response.json()
                first, next_date, inherited, skipped, last = created["occurrences"]
                event_path = base + f"/api/live-events/{created['id']}"
                targets = event_path + f"/{first['id']}/destinations"
                next_targets = event_path + f"/{next_date['id']}/destinations"
                self.assertEqual(403, api.post(targets + "/facebook", headers=auth, data={"page": "303"}).status)
                page.goto(base + "/operator/live-events")
                page.get_by_role("button", name="Open production for Facebook Live rehearsal", exact=False).first.click()
                page.get_by_text("Simulcast destinations", exact=True).click()
                page.get_by_role("button", name="Load Facebook Live Pages", exact=True).click()
                page.get_by_role("combobox", name="Facebook Live Page", exact=True).select_option("101")
                auto_control = page.get_by_role("checkbox", name="Start automatically when CuePorch is live")
                expect(auto_control).to_be_checked()
                auto_control.uncheck()
                page.get_by_role("button", name="Prepare Facebook Live", exact=True).click()
                expect(page.locator("#facebookLiveStatus")).to_contain_text("Facebook Live prepared.")
                expect(page.get_by_text("Facebook Live for this date", exact=False)).to_be_visible()
                repeat = api.post(targets + "/facebook", headers=auth, data={"page": "101"})
                self.assertEqual(200, repeat.status, repeat.text())
                destination = repeat.json()["id"]
                rows = api.get(targets, headers=auth).json()["destinations"]
                self.assertEqual([(destination, "facebook-live")], [(row["id"], row["provider"]) for row in rows])
                self.assertEqual([], api.get(next_targets, headers=auth).json()["destinations"])
                with closing(sqlite3.connect(database)) as db:
                    encrypted, = db.execute("SELECT secret FROM live_simulcast_destination WHERE id=?",
                                            (destination,)).fetchone()
                self.assertNotIn(b"fixture-facebook-live-secret", encrypted)
                self.assertNotIn("fixture-facebook-live-secret", page.content() + repeat.text())
                uncertain = api.post(targets + "/facebook", headers=auth, data={"page": "202"})
                self.assertEqual((202, {"state": "unconfirmed"}), (uncertain.status, uncertain.json()))
                stop()
                stop = start_app()
                uncertain = api.post(targets + "/facebook", headers=auth, data={"page": "202"})
                self.assertEqual((202, {"state": "unconfirmed"}), (uncertain.status, uncertain.json()))
                self.assertEqual(["101", "202"], live_writes)

                auto_path = next_targets + "/facebook/auto"
                page.goto(base + "/operator/live-events")
                next_label = next_date["local"].replace("T", " ")
                page.get_by_role("button", name=f"Open production for Facebook Live rehearsal at {next_label}",
                                 exact=True).click()
                page.get_by_text("Simulcast destinations", exact=True).click()
                page.get_by_role("button", name="Load Facebook Live Pages", exact=True).click()
                facebook_pages = page.get_by_role("combobox", name="Facebook Live Page", exact=True)
                facebook_pages.select_option("101")
                expect(page.get_by_role("checkbox", name="Start automatically when CuePorch is live")).to_be_checked()
                expect(page.get_by_role("checkbox", name="Also start future dates automatically", exact=True)).to_be_checked()
                page.get_by_role("button", name="Prepare Facebook Live", exact=True).click()
                expect(page.locator("#facebookLiveStatus")).to_contain_text("will start when this occurrence is ready")
                page.reload()
                page.get_by_role("button", name=f"Open production for Facebook Live rehearsal at {next_label}",
                                 exact=True).click()
                page.get_by_text("Simulcast destinations", exact=True).click()
                page.get_by_role("button", name="Load Facebook Live Pages", exact=True).click()
                expect(page.locator("#facebookLiveStatus")).to_contain_text("will start when this occurrence is ready")
                expect(page.get_by_role("button", name="Cancel automatic start", exact=True)).to_be_enabled()
                self.assertEqual(403, api.get(auto_path + "?page=101").status)
                self.assertEqual(202, api.put(auto_path, headers=auth, data={"page": "202"}).status)
                facebook_pages.select_option("202")
                page.get_by_role("button", name="Cancel automatic start", exact=True).click()
                expect(page.locator("#facebookLiveStatus")).to_contain_text("canceled")
                self.assertEqual({"state": "idle", "series": False},
                                 api.get(auto_path + "?page=202", headers=auth).json())
                opens = int(next_date["id"])
                with closing(sqlite3.connect(database)) as db:
                    self.assertEqual([], facebook_live_job.due(db, opens + 1))
                    db.execute("INSERT INTO live_occurrence VALUES (?,?,'fixture','fixture','ready')",
                               (created["id"], opens))
                    db.commit()
                    self.assertEqual([], facebook_live_job.due(db, opens - 1))
                stop()
                stop = start_app()
                roster = []
                with patch.object(facebook_live_job, "time", lambda: opens + 1):
                    deadline = monotonic() + 8
                    while not roster and monotonic() < deadline:
                        roster = api.get(next_targets, headers=auth).json()["destinations"]
                        if not roster:
                            sleep(.05)
                self.assertEqual(["101"], live_now_writes)
                self.assertEqual(["101", "202"], live_writes)
                self.assertEqual(["facebook-live"], [item["provider"] for item in roster])
                self.assertEqual({"state": "prepared", "series": True},
                                 api.get(auto_path + "?page=101", headers=auth).json())
                auto_result = api.get(next_targets, headers=auth)
                self.assertNotIn("fixture-facebook-auto", auto_result.text())
                check_destination_rtmps(root, database, roster[0]["id"], operator, facebook_live.INGEST_SERVER,
                                        "fixture-facebook-auto")

                inherited_path = f"{event_path}/{inherited['id']}/destinations"
                skipped_path = f"{event_path}/{skipped['id']}/destinations"
                last_path = f"{event_path}/{last['id']}/destinations"
                self.assertEqual({"state": "queued", "series": True},
                                 api.get(inherited_path + "/facebook/auto?page=101", headers=auth).json())
                canceled_date = api.delete(skipped_path + "/facebook/auto", headers=auth, data={"page": "101"})
                self.assertEqual(200, canceled_date.status, canceled_date.text())
                self.assertEqual({"state": "idle", "series": True, "restorable": True},
                                 api.get(skipped_path + "/facebook/auto?page=101", headers=auth).json())
                restored = api.put(skipped_path + "/facebook/auto", headers=auth,
                                   data={"page": "101", "restore": True})
                self.assertEqual(202, restored.status, restored.text())
                self.assertEqual({"state": "queued", "series": True},
                                 api.get(skipped_path + "/facebook/auto?page=101", headers=auth).json())
                restored_date = api.delete(skipped_path + "/facebook/auto", headers=auth, data={"page": "101"})
                self.assertEqual(200, restored_date.status, restored_date.text())
                queued_last = api.put(last_path + "/facebook/auto", headers=auth,
                                      data={"page": "101", "series": True})
                self.assertEqual(202, queued_last.status, queued_last.text())
                self.assertEqual({"state": "queued", "series": True},
                                 api.get(inherited_path + "/facebook/auto?page=101", headers=auth).json())
                self.assertEqual({"state": "idle", "series": True, "restorable": True},
                                 api.get(skipped_path + "/facebook/auto?page=101", headers=auth).json())
                deleted_last = api.delete(last_path + "/facebook/auto", headers=auth, data={"page": "101"})
                self.assertEqual(200, deleted_last.status, deleted_last.text())
                with closing(sqlite3.connect(database)) as db:
                    db.executemany("INSERT INTO live_occurrence VALUES (?,?,'fixture','fixture','ready')",
                                   [(created["id"], int(date["id"])) for date in (inherited, skipped)])
                    db.commit()
                    due = facebook_live_job.due(db, int(skipped["id"]) + 1)
                    self.assertEqual([int(inherited["id"])], [row[1] for row in due])
                inherited_roster = []
                with patch.object(facebook_live_job, "time", lambda: int(inherited["id"]) + 1):
                    deadline = monotonic() + 8
                    while not inherited_roster and monotonic() < deadline:
                        inherited_roster = api.get(inherited_path, headers=auth).json()["destinations"]
                        if not inherited_roster:
                            sleep(.05)
                self.assertEqual(["101", "101"], live_now_writes)
                self.assertEqual(["facebook-live"], [entry["provider"] for entry in inherited_roster])
                self.assertEqual([], api.get(skipped_path, headers=auth).json()["destinations"])

                facebook_pages.select_option("101")
                stop_series = page.get_by_role("button", name="Stop automatic starts for future dates", exact=True)
                expect(stop_series).to_be_enabled()
                stop_series.click()
                expect(stop_series).to_be_disabled()
                self.assertEqual({"state": "idle", "series": False},
                                 api.get(last_path + "/facebook/auto?page=101", headers=auth).json())
                self.assertEqual(inherited_roster, api.get(inherited_path, headers=auth).json()["destinations"])

                page.goto(base + "/operator/integrations")
                profile_catalog = base + "/api/integrations/facebook-profile-live/profiles"
                self.assertEqual(409, api.get(profile_catalog, headers=auth).status)
                page.get_by_role("button", name="Connect Facebook Profile Live", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("Facebook Profile Live is connected.", timeout=15000)
                page.get_by_role("button", name="Show Facebook profiles", exact=True).click()
                expect(page.get_by_text("Porch Artist", exact=True)).to_be_visible()
                profile_list = api.get(profile_catalog, headers=auth)
                self.assertEqual({"items": [{"id": "707", "name": "Porch Artist"}]}, profile_list.json())
                self.assertNotIn("fixture-long", profile_list.text())
                self.assertEqual(403, api.get(profile_catalog).status)
                self.assertEqual(403, api.post(next_targets + "/facebook-profile", headers=auth, data={"page": "101"}).status)
                self.assertEqual(403, api.post(next_targets + "/facebook", headers=auth, data={"page": "707"}).status)

                page.goto(base + "/operator/live-events")
                page.get_by_role("button", name=f"Open production for Facebook Live rehearsal at {next_label}",
                                 exact=True).click()
                page.get_by_text("Simulcast destinations", exact=True).click()
                page.locator("#facebookLiveTarget").select_option("profile")
                page.get_by_role("button", name="Load Facebook profiles", exact=True).click()
                expect(page.get_by_role("combobox", name="Facebook profile", exact=True)).to_have_value("707")
                expect(page.get_by_role("checkbox", name="Also start future dates automatically", exact=True)).to_be_checked()
                page.locator("#facebookLivePrivacy").select_option("ALL_FRIENDS")
                page.get_by_role("button", name="Prepare Facebook Live", exact=True).click()
                expect(page.locator("#facebookLiveStatus")).to_contain_text("will start when this occurrence is ready")
                for occurrence_path in (next_targets, skipped_path):
                    canceled = api.delete(occurrence_path + "/facebook-profile/auto", headers=auth, data={"page": "707"})
                    self.assertEqual(200, canceled.status, canceled.text())
                page.get_by_role("button", name=f"Open production for Facebook Live rehearsal at {skipped['local'].replace('T', ' ')}", exact=True).click()
                page.get_by_text("Simulcast destinations", exact=True).click()
                page.locator("#facebookLiveTarget").select_option("profile")
                page.get_by_role("button", name="Load Facebook profiles", exact=True).click()
                expect(page.locator("#facebookRestoreDate")).to_be_visible()
                page.locator("#facebookRestoreDate").click()
                expect(page.locator("#facebookLivePrivacy")).to_have_value("ALL_FRIENDS")
                expect(page.locator("#facebookRestoreDate")).to_be_hidden()
                expect(page.locator("#facebookLiveStatus")).to_contain_text("will start when this occurrence is ready")
                restored = api.put(skipped_path + "/facebook-profile/auto", headers=auth,
                                   data={"page": "707", "restore": True})
                self.assertEqual(202, restored.status, restored.text())
                page.get_by_role("button", name="Cancel automatic start", exact=True).click()
                expect(page.locator("#facebookRestoreDate")).to_be_visible()
                self.assertEqual([], profile_writes)
                inherited_profile = []
                with patch.object(facebook_live_job, "time", lambda: int(skipped["id"]) + 1):
                    deadline = monotonic() + 8
                    while not inherited_profile and monotonic() < deadline:
                        inherited_profile = [item for item in api.get(inherited_path, headers=auth).json()["destinations"]
                                             if item["provider"] == "facebook-profile-live"]
                        if not inherited_profile:
                            sleep(.05)
                self.assertEqual([{
                    "title": event["title"],
                    "status": "LIVE_NOW",
                    "stop_on_delete_stream": True,
                    "privacy": {"value": "ALL_FRIENDS"},
                }], profile_writes)
                inherited_auto = api.get(inherited_path + "/facebook-profile/auto?page=707", headers=auth)
                self.assertEqual(200, inherited_auto.status, inherited_auto.text())
                self.assertEqual("ALL_FRIENDS", inherited_auto.json()["privacy"])
                self.assertEqual(422, api.put(inherited_path + "/facebook-profile/auto", headers=auth,
                                               data={"page": "707", "privacy": "INVALID"}).status)
                self.assertEqual([], api.get(skipped_path, headers=auth).json()["destinations"])
                self.assertEqual(1, len(inherited_profile))
                check_destination_rtmps(root, database, inherited_profile[0]["id"], operator, facebook_live.INGEST_SERVER,
                                        "fixture-facebook-profile-key")
                stop_profile = api.delete(inherited_path + "/facebook-profile/auto", headers=auth,
                                          data={"page": "707", "series": True})
                self.assertEqual(200, stop_profile.status, stop_profile.text())
                self.assertEqual({"state": "idle", "series": False},
                                 api.get(last_path + "/facebook-profile/auto?page=707", headers=auth).json())

                last_opens, skipped_opens = int(last["id"]), int(skipped["id"])
                profile_auto = "/facebook-profile/auto"
                explicit = api.put(last_path + profile_auto, headers=auth,
                                   data={"page": "707", "privacy": "SELF"})
                self.assertEqual(202, explicit.status, explicit.text())
                with closing(sqlite3.connect(database)) as db:
                    db.execute("UPDATE facebook_live_auto SET event_revision='stale' "
                               "WHERE event=? AND page='profile:707' AND opens IN (?,?)",
                               (created["id"], last_opens, skipped_opens))
                    db.commit()
                enabled = api.put(next_targets + profile_auto, headers=auth,
                                  data={"page": "707", "series": True, "privacy": "ALL_FRIENDS"})
                self.assertEqual(202, enabled.status, enabled.text())
                self.assertEqual({"state": "queued", "series": True, "privacy": "ALL_FRIENDS"},
                                 api.get(last_path + profile_auto + "?page=707", headers=auth).json())
                self.assertEqual({"state": "idle", "series": True, "privacy": "ALL_FRIENDS", "restorable": True},
                                 api.get(skipped_path + profile_auto + "?page=707", headers=auth).json())
                with closing(sqlite3.connect(database)) as db:
                    db.row_factory = sqlite3.Row
                    db.execute("INSERT INTO live_occurrence VALUES (?,?,'fixture','fixture','ready')",
                               (created["id"], last_opens))
                    due = [row for row in facebook_live_job.due(db, last_opens)
                           if row["event"] == created["id"] and row["page"] == "profile:707"
                           and row["opens"] in (skipped_opens, last_opens)]
                    self.assertEqual([(last_opens, "ALL_FRIENDS", 1)],
                                     [(row["opens"], row["privacy"], row["series"]) for row in due])
                    key = (created["id"], last_opens, "profile:707")
                    self.assertTrue(facebook_live_job.claim(db, key, due[0]))
                    self.assertFalse(facebook_live_job.claim(db, key, due[0]))
                    db.rollback()
                restored = api.put(skipped_path + profile_auto, headers=auth, data={"page": "707", "restore": True})
                self.assertEqual(202, restored.status, restored.text())
                self.assertEqual({"state": "queued", "series": True, "privacy": "ALL_FRIENDS"},
                                 api.get(skipped_path + profile_auto + "?page=707", headers=auth).json())
                stopped = api.delete(last_path + profile_auto, headers=auth, data={"page": "707", "series": True})
                self.assertEqual(200, stopped.status, stopped.text())
                manual_profile = api.post(last_path + "/facebook-profile", headers=auth,
                                          data={"page": "707", "privacy": "SELF"})
                self.assertEqual(201, manual_profile.status, manual_profile.text())
                self.assertEqual("SCHEDULED_UNPUBLISHED", profile_writes[-1]["status"])
                self.assertEqual(int(last["id"]), profile_writes[-1]["event_params"]["start_time"])
                self.assertEqual({"value": "SELF"}, profile_writes[-1]["privacy"])
                profile_write_count = len(profile_writes)
                repeated_profile = api.post(last_path + "/facebook-profile", headers=auth,
                                            data={"page": "707", "privacy": "ALL_FRIENDS"})
                self.assertEqual(200, repeated_profile.status, repeated_profile.text())
                self.assertEqual(profile_write_count, len(profile_writes))
                self.assertEqual("SELF", api.get(last_path + "/facebook-profile/auto?page=707",
                                                  headers=auth).json()["privacy"])

                page.goto(base + "/operator/integrations")
                page.get_by_role("button", name="Disconnect Facebook Profile Live", exact=True).click()
                expect(page.get_by_role("button", name="Connect Facebook Profile Live", exact=True)).to_be_visible()
                self.assertEqual(409, api.get(profile_catalog, headers=auth).status)
                self.assertEqual(200, api.get(catalog, headers=auth).status)
                page.get_by_role("button", name="Disconnect Facebook Live", exact=True).click()
                expect(page.get_by_role("button", name="Connect Facebook Live", exact=True)).to_be_visible()
                self.assertEqual(409, api.get(catalog, headers=auth).status)
                self.assertEqual("disconnected", api.get(targets, headers=auth).json()["destinations"][0]["state"])
                self.assertEqual(409, api.put(targets + "/" + destination, headers=auth, data={"enabled": True}).status)
                self.assertEqual(2, len(api.get(endpoint, headers=auth).json()["pages"]))
                page.get_by_role("button", name="Disconnect Facebook Pages", exact=True).click()
                expect(page.get_by_role("button", name="Connect Facebook Pages", exact=True)).to_be_visible()
                self.assertEqual([], api.get(endpoint, headers=auth).json()["pages"])
        print("Facebook Pages OAuth long-lived scope, paginated Page selection, exact multipart restart and playback,"
              " rate-limit retry, optional approval and disconnect passed.")
        print("Facebook Live verified a separate grant, declined permission denial, safe Page list and independent disconnect.")
        print("Facebook Live prepared one occurrence through the browser and preserved an unconfirmed write across restart.")
        print("Facebook Live auto start waited for readiness and the scheduled time, survived restart, honored cancellation,"
              " and delivered decoded RTMPS.")
        print("Future Facebook episodes inherited automatic delivery, skipped an opted-out date, and stopped future"
              " activation without removing prepared broadcasts.")


if __name__ == "__main__":
    unittest.main()
