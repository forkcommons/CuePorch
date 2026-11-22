"""Browser and worker rehearsal against a controlled LinkedIn API implementation."""

from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep, time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from flask import Flask, Response, redirect, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker
from youtube_rtmps_fixture import check_destination_rtmps

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
import event_simulcast
import linkedin_live
import linkedin_live_job
import linkedin_upload as linkedin
import live_destination_series
import social_publication
from workspace_oauth import provider_request


class LinkedInPublication(unittest.TestCase):
    def test_public_company_post_survives_interruption_without_duplicates(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            clip = root / "Porch Company Page.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=640x360:rate=24", "-t", "5", "-c:v", "libx264", "-preset", "ultrafast",
                            "-crf", "18", "-movflags", "+faststart", str(clip)], check=True, capture_output=True)
            video = clip.read_bytes()
            middle = len(video) // 2
            digest = hashlib.sha256(video).hexdigest()
            self.assertGreaterEqual(len(video), linkedin.MIN_BYTES)
            remote = Flask("linkedin-fixture")
            provider = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            origin = f"http://127.0.0.1:{provider.server_port}"
            service = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/linkedin/callback"
            live_callback = base + "/integration/oauth/linkedin-live/callback"
            member_callback = base + "/integration/oauth/linkedin-member-live/callback"
            database = root / "workspace.sqlite"
            owner = "urn:li:organization:12345"
            received = {}
            posts = []
            failed_once = set()
            ready = set()
            approved_live_pages = set()
            member_id = ["FixtureMember1"]
            member_owner = "urn:li:person:FixtureMember1"
            member_asset = "urn:li:digitalmediaAsset:Member5500"
            member_writes = []
            member_scheduled_starts = []
            page_offsets = []
            scheduled_videos = []
            announcement_writes = []
            ingests = []
            links = []
            ends = []
            sending = set()
            ready_to_link = set()
            status_requests = []
            catalog_time = []
            catalog_offsets = []
            asset = "urn:li:digitalmediaAsset:Fixture123"
            operator = secrets.token_urlsafe(32)

            def signed_request(method, url, **kwargs):
                parsed = urlsplit(url)
                self.assertEqual(("PUT", "https", "www.linkedin.com"), (method, parsed.scheme, parsed.netloc))
                return provider_request(method, origin + parsed.path + "?" + parsed.query, **kwargs)

            cleanup.enter_context(patch.multiple(linkedin, OAUTH=replace(
                linkedin.OAUTH, authorization_url=origin + "/oauth/authorize", token_url=origin + "/oauth/token",
            ), API=origin + "/rest", POLL_SECONDS=1, provider_request=signed_request))
            cleanup.enter_context(patch.multiple(linkedin_live, OAUTH=replace(
                linkedin_live.OAUTH, authorization_url=origin + "/oauth/authorize",
                token_url=origin + "/oauth/token",
            ), MEMBER_OAUTH=replace(
                linkedin_live.MEMBER_OAUTH, authorization_url=origin + "/oauth/authorize",
                token_url=origin + "/oauth/token",
            ), API=origin + "/rest", V2_API=origin + "/v2"))
            cleanup.enter_context(patch.object(social_publication, "RETRY_SECONDS", (3, 3)))
            cleanup.enter_context(patch.object(live_destination_series, "SCAN_INTERVAL", 0.2))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_PUBLIC_URL": "", "CUEPORCH_LINKEDIN_CLIENT_ID": "cueporch",
                "CUEPORCH_LINKEDIN_CLIENT_SECRET": "fixture-secret", "CUEPORCH_LINKEDIN_COMPANY_ID": "12345",
                "CUEPORCH_LINKEDIN_LIVE_CLIENT_ID": "cueporch-live",
                "CUEPORCH_LINKEDIN_LIVE_CLIENT_SECRET": "fixture-live-secret",
                "CUEPORCH_LINKEDIN_LIVE_COMPANY_ID": "12345", "CUEPORCH_LINKEDIN_LIVE_REGION": "WEST_US",
                "CUEPORCH_LINKEDIN_MEMBER_LIVE_CLIENT_ID": "cueporch-member-live",
                "CUEPORCH_LINKEDIN_MEMBER_LIVE_CLIENT_SECRET": "fixture-member-secret",
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
                if request.args["client_id"] == "cueporch-member-live":
                    assert request.args["redirect_uri"] == member_callback
                    assert request.args["scope"] == "r_member_live w_member_live r_liteprofile"
                    return redirect(member_callback + "?" + urlencode(
                        dict(state=request.args["state"], code="linkedin-member-code")))
                if request.args["client_id"] == "cueporch-live":
                    assert request.args["redirect_uri"] == live_callback
                    assert request.args["scope"] == linkedin_live.OAUTH.scope
                    return redirect(live_callback + "?" + urlencode(
                        dict(state=request.args["state"], code="linkedin-live-code")))
                assert request.args["client_id"] == "cueporch" and request.args["redirect_uri"] == callback
                assert request.args["scope"] == "w_organization_social" and "code_challenge" not in request.args
                return redirect(callback + "?" + urlencode(dict(state=request.args["state"], code="linkedin-code")))

            @remote.post("/oauth/token")
            def token():
                if request.form["code"] == "linkedin-member-code":
                    assert request.form["client_secret"] == "fixture-member-secret"
                    assert request.form["redirect_uri"] == member_callback
                    assert request.form["grant_type"] == "authorization_code"
                    return dict(access_token="fixture-linkedin-member-live", scope=linkedin_live.MEMBER_OAUTH.scope,
                                expires_in=5184000)
                if request.form["code"] == "linkedin-live-code":
                    assert request.form["client_secret"] == "fixture-live-secret"
                    assert request.form["redirect_uri"] == live_callback
                    assert request.form["grant_type"] == "authorization_code"
                    return dict(access_token="fixture-linkedin-live", scope=linkedin_live.OAUTH.scope,
                                expires_in=5184000)
                assert request.form["client_secret"] == "fixture-secret" and request.form["code"] == "linkedin-code"
                assert request.form["redirect_uri"] == callback and request.form["grant_type"] == "authorization_code"
                return dict(access_token="fixture-linkedin", scope="w_organization_social", expires_in=5184000)

            @remote.before_request
            def check_headers():
                if request.path.startswith("/rest/"):
                    token = ("fixture-linkedin-live" if request.path == "/rest/organizationAcls"
                             else "fixture-linkedin")
                    assert request.headers["Authorization"] == "Bearer " + token
                    for key, value in linkedin.HEADERS.items():
                        assert request.headers[key] == value

            @remote.get("/v2/me")
            def live_member_profile():
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-member-live"
                return dict(id=member_id[0])

            @remote.get("/rest/organizationAcls")
            def organization_acls():
                offset = int(request.args["start"])
                assert request.args.to_dict() == dict(
                    q="roleAssignee", role="ADMINISTRATOR", state="APPROVED", count="10", start=str(offset))
                page_offsets.append(offset)
                if offset == 0:
                    return dict(elements=[
                        dict(role="ADMINISTRATOR", state="APPROVED", organization="urn:li:organization:999"),
                        dict(role="ADMINISTRATOR", state="REQUESTED", organization=owner),
                    ], paging=dict(links=[dict(rel="next", href="https://invalid.example/ignored")]))
                assert offset == 10
                rows = [dict(role="ADMINISTRATOR", state="APPROVED", organizationTarget=owner)]
                return dict(elements=rows if owner in approved_live_pages else [], paging=dict(links=[]))

            @remote.get("/v2/liveVideos")
            def existing_events():
                if request.headers.get("Authorization") == "Bearer fixture-linkedin-member-live":
                    assert request.args.to_dict() == dict(q="author", author=member_owner, count="15", start="0")
                    return dict(elements=[
                        dict(id=5556, name="Existing profile broadcast", scheduledAt=catalog_time[0],
                             author={"member": member_owner}, ugcPost="urn:li:ugcPost:5557"),
                        dict(id=5558, scheduledAt=catalog_time[0], author={"organization": owner}),
                    ], paging=dict(total=2))
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-live"
                offset = int(request.args["start"])
                assert request.args.to_dict() == dict(q="author", author=owner, count="15", start=str(offset))
                catalog_offsets.append(offset)
                starts_ms = catalog_time[0]
                candidate = dict(id=3456, name="Existing LinkedIn broadcast", state="PRE_LIVE",
                                 scheduledAt=starts_ms, ugcPost="urn:li:ugcPost:7654")
                if offset == 0:
                    return dict(elements=[
                        candidate | dict(id=1, scheduledAt=starts_ms - 86_400_000),
                        candidate | dict(id=2, liveVideoAsset={"media": asset}),
                        candidate | dict(id=3, author={"organization": "urn:li:organization:999"}),
                    ], paging=dict(total=16, links=[dict(rel="next", href="https://invalid.example/ignore")]))
                assert offset == 15
                return dict(elements=[
                    candidate,
                    candidate | dict(id=3457, name="Manual delivery broadcast", ugcPost="urn:li:ugcPost:7655"),
                ], paging=dict(total=17))

            @remote.post("/v2/liveVideos")
            def live_videos():
                if request.headers.get("Authorization") == "Bearer fixture-linkedin-member-live":
                    assert request.json["author"] == {"member": member_owner}
                    assert request.json["scheduledAt"] > int(time() * 1000)
                    if len(member_scheduled_starts) == 2:
                        assert "end" in member_writes
                        restart_at = first["id"] * 1000 + 60_000
                        assert restart_at <= request.json["scheduledAt"] < restart_at + 5000
                    member_writes.append("schedule")
                    member_scheduled_starts.append(request.json["scheduledAt"])
                    return "", 201, {"x-restli-id": str(5499 + len(member_scheduled_starts))}
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-live"
                assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
                incoming = request.json
                assert incoming["author"] == {"organization": owner}
                assert incoming["name"] == "LinkedIn Live rehearsal"
                assert incoming["scheduledAt"] > int(time() * 1000)
                scheduled_videos.append(incoming["scheduledAt"])
                return "", 201, {"x-restli-id": str(1110 + len(scheduled_videos))}

            @remote.post("/v2/ugcPosts")
            def announcements():
                if request.headers.get("Authorization") == "Bearer fixture-linkedin-member-live":
                    body = request.json
                    assert body["author"] == member_owner
                    media = body["specificContent"]["com.linkedin.ugc.ShareContent"]["media"]
                    assert media == [{"media": f"urn:li:liveVideo:{5499 + len(member_scheduled_starts)}", "status": "READY"}]
                    member_writes.append("announce")
                    return "", 201, {"x-restli-id": f"urn:li:ugcPost:{5554 + member_writes.count('announce')}"}
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-live"
                assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
                video = f"urn:li:liveVideo:{1110 + len(scheduled_videos)}"
                body = request.json
                assert body == {
                    "author": owner, "lifecycleState": "PUBLISHED",
                    "specificContent": {"com.linkedin.ugc.ShareContent": {
                        "media": [{"media": video, "status": "READY"}],
                        "shareCommentary": {"text": "LinkedIn Live rehearsal"},
                        "shareMediaCategory": "URN_REFERENCE",
                    }},
                    "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
                    "distribution": {"feedDistribution": "MAIN_FEED", "externalDistributionChannels": [],
                                     "distributedViaFollowFeed": True},
                }
                announcement_writes.append(video)
                if len(announcement_writes) == 2:
                    return "", 503
                return "", 201, {"x-restli-id": "urn:li:ugcPost:2222"}

            @remote.post("/v2/liveAssetActions")
            def live_ingest():
                action = request.args["action"]
                if request.headers.get("Authorization") == "Bearer fixture-linkedin-member-live":
                    if action == "end":
                        assert request.json == {"asset": member_asset}
                        member_writes.append("end")
                        return "", 200
                    assert action == "register"
                    assert request.json == {"registerLiveEventRequest": {
                        "owner": member_owner, "recipes": [linkedin_live.LIVE_RECIPE], "region": "WEST_US",
                    }}
                    member_writes.append("register")
                    asset_urn, key = (member_asset, "fixture-member-live-key") if len(member_scheduled_starts) == 1 else (
                        "urn:li:digitalmediaAsset:Member5502", "fixture-member-restart-key")
                    return dict(value=dict(asset=asset_urn, ingestUrls=[
                        dict(ingestProtocol="RTMPS", url="rtmps://live-ingest.linkedin.com:443/live/" + key),
                    ]))
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-live"
                if action == "register":
                    assert request.json == {"registerLiveEventRequest": {
                        "owner": owner, "recipes": [linkedin_live.LIVE_RECIPE], "region": "WEST_US",
                    }}
                    ingests.append(asset)
                    return dict(value=dict(asset=asset, ingestUrls=[
                        dict(ingestProtocol="RTMPS", url="rtmps://127.0.0.1/live/invalid-provider-url"),
                        dict(ingestProtocol="RTMPS", url="rtmps://live-ingest.linkedin.com:443/live/fixture-linkedin-key"),
                    ]))
                assert action == "end" and request.json == {"asset": asset}
                ends.append(asset)
                return "", 200

            @remote.get("/v2/assets/Fixture123")
            def ingest_status():
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-live"
                status_requests.append(asset)
                return dict(recipes=[dict(recipe=linkedin_live.LIVE_RECIPE,
                                          status="AVAILABLE" if ready_to_link else "PROCESSING")])

            @remote.get("/v2/liveVideos/1111")
            def current_ingest():
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-live"
                return dict(id=1111, liveVideoAsset={"media": asset} if links else None)

            @remote.post("/v2/liveVideos/1111")
            def link_ingest():
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-live"
                assert request.headers["X-Restli-Method"] == "PARTIAL_UPDATE"
                assert request.json == {"patch": {"$set": {"liveVideoAsset": {"media": asset}}}}
                assert sending and ready_to_link
                links.append(asset)
                return "", 503

            @remote.get("/v2/assets/Member5500")
            def profile_ingest_status():
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-member-live"
                return dict(recipes=[dict(recipe=linkedin_live.LIVE_RECIPE, status="AVAILABLE")])

            @remote.route("/v2/liveVideos/5500", methods=["GET", "POST"])
            def profile_broadcast_asset():
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-member-live"
                if request.method == "GET":
                    return dict(id=5500, liveVideoAsset={"media": member_asset} if "bind" in member_writes else None)
                assert request.json == {"patch": {"$set": {"liveVideoAsset": {"media": member_asset}}}}
                member_writes.append("bind")
                return "", 204

            @remote.get("/v2/assets/Member5502")
            def restarted_profile_ingest_status():
                return profile_ingest_status()

            @remote.route("/v2/liveVideos/5502", methods=["GET", "POST"])
            def restarted_profile_broadcast_asset():
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-member-live"
                asset = "urn:li:digitalmediaAsset:Member5502"
                if request.method == "GET":
                    return dict(id=5502, liveVideoAsset={"media": asset} if "restart_bind" in member_writes else None)
                assert request.json == {"patch": {"$set": {"liveVideoAsset": {"media": asset}}}}
                member_writes.append("restart_bind")
                return "", 204

            @remote.post("/rest/videos")
            def videos():
                if request.args["action"] == "initializeUpload":
                    assert request.json["initializeUploadRequest"] == dict(
                        owner=owner, fileSizeBytes=len(video), uploadCaptions=False, uploadThumbnail=False)
                    uid = f"Fixture{len(received) + 1}"
                    received[uid] = dict(parts={}, finalized=False)
                    parts = [(0, middle - 1), (middle, len(video) - 1)]
                    return dict(value=dict(
                        video="urn:li:video:" + uid, uploadToken="fixture-token-" + uid,
                        uploadUrlsExpireAt=int((time() + 500) * 1000),
                        uploadInstructions=[dict(firstByte=first, lastByte=last,
                            uploadUrl=f"https://www.linkedin.com/dms-uploads/{uid}/uploadedVideo?sau={index}")
                            for index, (first, last) in enumerate(parts)],
                    ))
                assert request.args["action"] == "finalizeUpload"
                incoming = request.json["finalizeUploadRequest"]
                uid = incoming["video"].split(":")[-1]
                record = received[uid]
                assert not record["finalized"] and incoming["uploadToken"] == "fixture-token-" + uid
                assert incoming["uploadedPartIds"] == [f"tag-{uid}-0", f"tag-{uid}-1"]
                record["finalized"] = True
                return "", 200

            @remote.put("/dms-uploads/<uid>/uploadedVideo")
            def media_part(uid):
                assert "Authorization" not in request.headers and request.content_type == "application/octet-stream"
                index = int(request.args["sau"])
                data = request.get_data()
                assert data == (video[:middle] if index == 0 else video[middle:])
                if uid == "Fixture1" and index == 1 and uid not in failed_once:
                    failed_once.add(uid)
                    return "transfer interrupted", 503
                assert index not in received[uid]["parts"]
                received[uid]["parts"][index] = data
                return "", 201, {"ETag": f'"tag-{uid}-{index}"'}

            @remote.get("/rest/videos/<video_id>")
            def status(video_id):
                uid = video_id.split(":")[-1]
                state = "WAITING_UPLOAD"
                if received[uid]["finalized"]:
                    state = "AVAILABLE" if uid in ready else "PROCESSING"
                return dict(owner=owner, id=video_id, status=state)

            @remote.post("/rest/posts")
            def post():
                body = request.json
                uid = body["content"]["media"]["id"].split(":")[-1]
                assert uid in ready and body["commentary"] == "Public broadcast from the Porch."
                assert (body["author"], body["visibility"], body["lifecycleState"]) == (owner, "PUBLIC", "PUBLISHED")
                assert body["distribution"]["feedDistribution"] == "MAIN_FEED"
                posts.append(uid)
                if uid == "Fixture2":
                    return "connection lost after commit", 502
                return "", 201, {"x-restli-id": "urn:li:share:87654321"}

            @remote.get("/playback/Fixture1")
            def playback():
                return Response(b"".join(received["Fixture1"]["parts"].values()), mimetype="video/mp4")

            cleanup.enter_context(service_worker(provider))
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                api = page.context.request
                auth = {"Authorization": "Bearer " + operator}
                page.goto(base + "/operator/integrations")
                page.get_by_label("Operator credential").fill(operator)
                page.get_by_role("button", name="Open integrations").click()
                page.get_by_role("button", name="Connect LinkedIn Company Page", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("LinkedIn Company Page is connected.", timeout=15000)
                upload = api.post(base + "/api/library/uploads", headers=auth, multipart={
                    "upload_id": str(uuid4()),
                    "file": {"name": clip.name, "mimeType": "video/mp4", "buffer": video},
                }, timeout=30000)
                self.assertEqual(201, upload.status, upload.text())
                uid = upload.json()["id"]
                endpoint = base + f"/api/library/work/{uid}/social/linkedin"
                self.assertEqual(403, api.get(endpoint).status)
                page.goto(base + f"/operator/videos/{uid}")
                page.get_by_role("link", name="Publish to LinkedIn").click()
                form = page.locator("#socialForm")
                expect(form).to_be_visible()
                expect(page.locator("#socialCompany")).to_have_text("12345")
                consent = form.get_by_label("Publish a public post on this Company Page")
                expect(consent).not_to_be_checked()
                invalid = api.post(endpoint, headers=auth | {"X-CuePorch-Social": "1"}, data={
                    "id": str(uuid4()), "title": "Wrong Page", "visibility": "public", "company": "12346",
                    "cut": api.get(endpoint, headers=auth).json()["cut"],
                })
                self.assertEqual(400, invalid.status)
                self.assertFalse(received)
                form.get_by_label("Post text").fill("Public broadcast from the Porch.")
                consent.check()
                form.get_by_role("button", name="Post to LinkedIn").click()
                deadline = monotonic() + 15
                checkpoint = None
                while monotonic() < deadline:
                    with closing(sqlite3.connect(database)) as db:
                        checkpoint = db.execute("SELECT session,offset,attempts FROM social_publication").fetchone()
                    if checkpoint and checkpoint[2]:
                        break
                    sleep(.05)
                self.assertEqual((middle, 1), checkpoint[1:])
                self.assertNotIn(b"fixture-token-", checkpoint[0])
                self.assertEqual({0}, set(received["Fixture1"]["parts"]))
                self.assertEqual([], posts)
                stop()
                start_app()
                page.reload()
                deadline = monotonic() + 15
                while not received["Fixture1"]["finalized"] and monotonic() < deadline:
                    sleep(.05)
                self.assertTrue(received["Fixture1"]["finalized"])
                self.assertEqual([], posts)
                self.assertTrue(api.put(base + "/api/policy/publication", headers=auth, data={"required": True}).ok)
                ready.add("Fixture1")
                expect(page.locator("#socialJobs")).to_contain_text("waiting for publication approval")
                waiting = api.get(endpoint, headers=auth).json()["jobs"][0]
                self.assertEqual([], posts)
                self.assertTrue(api.post(base + f"/api/actions/{waiting['id']}/decision", headers=auth,
                                        data={"decision": "approve"}).ok)
                expect(page.locator("#socialJobs").get_by_role("link", name="Open on LinkedIn Company Page")).to_have_attribute(
                    "href", "https://www.linkedin.com/feed/update/urn:li:share:87654321/", timeout=15000)
                self.assertEqual(["Fixture1"], posts)
                self.assertEqual(digest, hashlib.sha256(b"".join(received["Fixture1"]["parts"].values())).hexdigest())
                player = browser.new_page()
                player.goto(origin + "/playback/Fixture1")
                self.assertEqual(640, player.locator("video").evaluate("async v => {v.muted = true; await v.play(); return v.videoWidth}"))

                self.assertTrue(api.put(base + "/api/policy/publication", headers=auth, data={"required": False}).ok)
                form.get_by_label("Post text").fill("Public broadcast from the Porch.")
                consent.check()
                ready.add("Fixture2")
                form.get_by_role("button", name="Post to LinkedIn").click()
                expect(page.locator("#socialJobs")).to_contain_text("unconfirmed", timeout=15000)
                self.assertEqual(["Fixture1", "Fixture2"], posts)
                second = api.get(endpoint, headers=auth).json()["jobs"][0]
                self.assertEqual(("unconfirmed", None, False), (second["state"], second["link"], second["cancelable"]))
                page.goto(base + "/operator/integrations")
                catalog = base + "/api/integrations/linkedin-live/company"
                self.assertEqual(403, api.get(catalog).status)
                self.assertEqual(409, api.get(catalog, headers=auth).status)
                page.get_by_role("button", name="Connect LinkedIn Live", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("LinkedIn Live is connected.")
                self.assertEqual(403, api.get(catalog, headers=auth).status)
                approved_live_pages.add(owner)
                page.get_by_role("button", name="Show LinkedIn Company Page", exact=True).click()
                expect(page.get_by_text("Company Page 12345", exact=True)).to_be_visible()
                self.assertEqual([0, 10, 0, 10], page_offsets)
                self.assertNotIn("fixture-linkedin-live", api.get(base + "/api/integrations", headers=auth).text())
                starts = (datetime.now(timezone.utc) + timedelta(minutes=8)).replace(second=0, microsecond=0)
                event = dict(title="LinkedIn Live rehearsal", slug="linkedin-live-rehearsal", visibility="public",
                             type="live", latency="standard", save_replay=False,
                             schedule=dict(timezone="UTC", repeat="days", count=4,
                                           start=starts.strftime("%Y-%m-%dT%H:%M"),
                                           end=(starts + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")))
                created = api.post(base + "/api/live-events", headers=auth, data=event)
                self.assertTrue(created.ok, created.text())
                data = created.json()
                first, next_date, later, skipped_date = data["occurrences"]
                path = base + f"/api/live-events/{data['id']}"
                schedule_path = f"{path}/{first['id']}/destinations/linkedin/schedule"
                next_path = f"{path}/{next_date['id']}/destinations/linkedin/schedule"
                company = {"company": "12345"}
                denied = api.post(schedule_path, headers=auth, data={"company": "999"})
                self.assertEqual(403, denied.status)
                self.assertEqual([], scheduled_videos)
                page.goto(base + "/operator/live-events")
                page.get_by_role("button", name="Open production for LinkedIn Live rehearsal",
                                 exact=False).first.click()
                page.get_by_text("Simulcast destinations", exact=True).click()
                page.get_by_role("button", name="Load LinkedIn Company Page", exact=True).click()
                page.get_by_role("button", name="Schedule and announce on LinkedIn", exact=True).click()
                expect(page.locator("#linkedinLivePost")).to_have_attribute(
                    "href", "https://www.linkedin.com/feed/update/urn:li:ugcPost:2222/")
                repeated = api.post(schedule_path, headers=auth, data=company)
                self.assertEqual(200, repeated.status, repeated.text())
                self.assertEqual("urn:li:ugcPost:2222", repeated.json()["post_urn"])
                self.assertEqual([int(starts.timestamp()) * 1000], scheduled_videos)
                self.assertEqual(["urn:li:liveVideo:1111"], announcement_writes)
                self.assertTrue(api.put(base + "/api/policy/publication", headers=auth, data={"required": True}).ok)
                held = api.post(base + "/api/live-events", headers=auth, data=event | {
                    "slug": "held-linkedin-live", "title": "Held LinkedIn Live rehearsal",
                })
                self.assertTrue(held.ok, held.text())
                hold = held.json()
                pending = hold["occurrences"][0]["id"]
                blocked = api.post(base + f"/api/live-events/{hold['id']}/{pending}/destinations/linkedin/schedule",
                                   headers=auth, data=company)
                self.assertEqual(409, blocked.status, blocked.text())
                self.assertEqual(1, len(scheduled_videos))
                self.assertEqual(1, len(announcement_writes))
                self.assertTrue(api.put(base + "/api/policy/publication", headers=auth, data={"required": False}).ok)
                uncertain = api.post(next_path, headers=auth, data=company)
                self.assertEqual((202, {"state": "unconfirmed"}), (uncertain.status, uncertain.json()))
                stop()
                stop = start_app()
                after_restart = api.post(next_path, headers=auth, data=company)
                self.assertEqual((202, {"state": "unconfirmed"}), (after_restart.status, after_restart.json()))
                self.assertEqual([int(starts.timestamp()) * 1000,
                                  int((starts + timedelta(days=1)).timestamp()) * 1000], scheduled_videos)
                self.assertEqual(["urn:li:liveVideo:1111", "urn:li:liveVideo:1112"], announcement_writes)
                targets = schedule_path.removesuffix("/linkedin/schedule")
                prepare = targets + "/linkedin/prepare"
                page.goto(base + "/operator/live-events")
                page.get_by_role("button", name="Open production for LinkedIn Live rehearsal", exact=False).first.click()
                page.get_by_text("Simulcast destinations", exact=True).click()
                page.get_by_role("button", name="Prepare LinkedIn delivery", exact=True).click()
                expect(page.locator("#linkedinLiveStatus")).to_contain_text("CuePorch will publish")
                repeated = api.post(prepare, headers=auth, data={})
                self.assertEqual(200, repeated.status, repeated.text())
                self.assertEqual([asset], ingests)
                self.assertEqual(409, api.post(next_path.removesuffix("/schedule") + "/prepare",
                                               headers=auth, data={}).status)
                destination = repeated.json()["id"]
                roster = api.get(targets, headers=auth)
                self.assertNotIn("fixture-linkedin-key", roster.text() + page.content())
                self.assertEqual("registered", roster.json()["destinations"][0]["provider_phase"])
                with closing(sqlite3.connect(database)) as db:
                    secret, = db.execute("SELECT secret FROM live_simulcast_destination WHERE id=?",
                                         (destination,)).fetchone()
                self.assertNotIn(b"fixture-linkedin-key", secret)
                check_destination_rtmps(root, database, destination, operator,
                                        "rtmps://live-ingest.linkedin.com:443/live", "fixture-linkedin-key",
                                        "linkedin-live")
                sending.add(destination)
                with patch.object(event_simulcast.Simulcast, "state",
                                  lambda _self, uid, opens, dest: "sending" if dest in sending else "waiting"):
                    deadline = monotonic() + 8
                    while not status_requests and monotonic() < deadline:
                        sleep(.05)
                    self.assertTrue(status_requests)
                    self.assertEqual([], links)
                    ready_to_link.add(asset)
                    deadline = monotonic() + 10
                    while monotonic() < deadline:
                        rows = api.get(targets, headers=auth).json()["destinations"]
                        if rows[0]["provider_phase"] == "bound":
                            break
                        sleep(.1)
                    self.assertEqual("bound", rows[0]["provider_phase"])
                    self.assertEqual([asset], links)
                page.get_by_role("button", name="Turn off LinkedIn Live", exact=True).click()
                deadline = monotonic() + 18
                received_state = []
                while monotonic() < deadline:
                    received_state = api.get(targets, headers=auth).json()["destinations"]
                    if received_state[0]["provider_phase"] == "ended":
                        break
                    sleep(.1)
                self.assertEqual([asset], ends)
                self.assertEqual("ended", received_state[0]["provider_phase"])
                self.assertEqual(409, api.put(targets + "/" + destination, headers=auth, data={"enabled": True}).status)
                self.assertEqual([asset], ingests)
                self.assertEqual([asset], links)

                starts_later = starts + timedelta(days=1)
                catalog_time.append(int(starts_later.timestamp()) * 1000)
                schedule = dict(timezone="UTC", repeat="days", count=2,
                                start=starts_later.strftime("%Y-%m-%dT%H:%M"),
                                end=(starts_later + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
                existing_event = event | dict(title="Pick an existing LinkedIn event", slug="existing-linkedin", schedule=schedule)
                picked = api.post(base + "/api/live-events", headers=auth, data=existing_event)
                self.assertTrue(picked.ok, picked.text())
                item = picked.json()
                selected_path = (f"{base}/api/live-events/{item['id']}/{item['occurrences'][0]['id']}"
                                 "/destinations/linkedin/existing")
                self.assertEqual(400, api.get(selected_path + "?start=1", headers=auth).status)
                self.assertEqual(409, api.post(selected_path, headers=auth, data={"id": "3456", "start": 0}).status)
                page.goto(base + "/operator/live-events")
                page.get_by_role("button", name="Open production for Pick an existing LinkedIn event",
                                 exact=False).first.click()
                page.get_by_text("Simulcast destinations", exact=True).click()
                page.get_by_role("button", name="Load existing LinkedIn events", exact=True).click()
                options = page.get_by_role("combobox", name="Existing LinkedIn event", exact=True)
                expect(options.locator("option")).to_have_count(0)
                page.get_by_role("button", name="More LinkedIn events", exact=True).click()
                options.select_option("3456")
                expect(page.get_by_role("checkbox", name="Prepare LinkedIn delivery automatically")).to_be_checked()
                page.get_by_role("button", name="Use existing LinkedIn event", exact=True).click()
                expect(page.locator("#linkedinLivePost")).to_have_attribute(
                    "href", "https://www.linkedin.com/feed/update/urn:li:ugcPost:7654/")
                later_series = (f"{base}/api/live-events/{item['id']}/{item['occurrences'][1]['id']}"
                                "/destinations/linkedin/series")
                self.assertEqual({"active": True, "inherited": True, "skipped": False},
                                 api.get(later_series, headers=auth).json())
                self.assertTrue(api.delete(later_series, headers=auth, data={"date": False}).ok)
                repeat_choice = api.post(selected_path, headers=auth, data={"id": "3456", "start": 15})
                self.assertEqual(200, repeat_choice.status, repeat_choice.text())
                self.assertIs(repeat_choice.json()["auto_prepare"], True)
                duplicate = api.post(base + "/api/live-events", headers=auth, data=existing_event | {
                    "title": "Conflicting LinkedIn event", "slug": "conflicting-linkedin",
                }).json()
                manual_path = (f"{base}/api/live-events/{duplicate['id']}/{duplicate['occurrences'][0]['id']}"
                               "/destinations/linkedin/existing")
                self.assertEqual(409, api.post(manual_path, headers=auth, data={"id": "3456", "start": 15}).status)
                self.assertEqual([0, 0, 15, 15, 15], catalog_offsets)
                manual = api.post(manual_path, headers=auth, data={"id": "3457", "start": 15, "auto_prepare": False})
                self.assertEqual(201, manual.status, manual.text())
                self.assertIs(manual.json()["auto_prepare"], False)

                clock = starts_later + timedelta(seconds=1)
                opens = int(starts_later.timestamp())

                class OccurrenceClock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return clock.astimezone(tz) if tz else clock.replace(tzinfo=None)

                with closing(sqlite3.connect(database)) as db:
                    db.executemany("INSERT INTO live_occurrence VALUES (?, ?, 'fixture', 'fixture', 'ready')",
                                   [(item["id"], opens), (duplicate["id"], opens)])
                    db.commit()

                self.assertEqual([asset], ingests)
                scope = (item["id"], opens)
                with closing(sqlite3.connect(database)) as db:
                    count = db.execute(
                        "SELECT count(*) FROM live_simulcast_destination "
                        "WHERE event=? AND opens=? AND provider IS NOT NULL", scope,
                    ).fetchone()[0]
                    db.executemany(
                        "INSERT INTO live_simulcast_destination "
                        "(id,event,name,server,secret,enabled,opens,provider) VALUES(?,?,?,?,?,?,?,?)",
                        [(f"li-capacity-fixture-{i}", item["id"], f"LinkedIn capacity fixture {i}",
                          "rtmp://seed.invalid", b"", 0, opens, "youtube-live") for i in range(10 - count)],
                    )
                    db.commit()

                due_auto = linkedin_live_job.due_auto
                first_poll, second_poll, release = Event(), Event(), Event()

                def observe_capacity_poll(con, now):
                    if now == int(clock.timestamp()):
                        if first_poll.is_set():
                            second_poll.set()
                            if not release.is_set():
                                return []
                        else:
                            first_poll.set()
                    return due_auto(con, now)

                deliveries = []
                register_attempts = []
                original_register = linkedin_live.LiveConnection.register_ingest

                def register_after_local_pause(connection, revision, author, before_send):
                    register_attempts.append(author)
                    if len(register_attempts) > 1:
                        return original_register(connection, revision, author, before_send)

                    def withdrawal():
                        with closing(sqlite3.connect(database)) as db, db:
                            db.execute("UPDATE live_occurrence SET delivery='withdrawing' WHERE event=? AND opens=?", scope)
                        try:
                            before_send()
                        finally:
                            with closing(sqlite3.connect(database)) as db, db:
                                db.execute("UPDATE live_occurrence SET delivery='ready' WHERE event=? AND opens=?", scope)

                    return original_register(connection, revision, author, withdrawal)

                try:
                    with (
                        patch.object(linkedin_live.LiveConnection, "register_ingest", register_after_local_pause),
                        patch.object(event_simulcast, "datetime", OccurrenceClock),
                        patch.object(linkedin_live_job, "time", lambda: clock.timestamp()),
                        patch.object(linkedin_live_job, "due_auto", observe_capacity_poll),
                    ):
                        self.assertTrue(second_poll.wait(8), "LinkedIn did not advance while capacity was full.")
                        self.assertEqual([asset], ingests)
                        with closing(sqlite3.connect(database)) as db:
                            self.assertEqual([("queued",)], db.execute(
                                "SELECT state FROM linkedin_live_auto WHERE event=? AND opens=?", scope,
                            ).fetchall())
                            db.execute("DELETE FROM live_simulcast_destination WHERE id='li-capacity-fixture-0'")
                            db.commit()
                        release.set()
                        deadline = monotonic() + 8
                        while monotonic() < deadline:
                            with closing(sqlite3.connect(database)) as db:
                                deliveries = db.execute("SELECT event, phase FROM linkedin_live_ingest WHERE opens=?",
                                                        (opens,)).fetchall()
                            if deliveries and all(phase != "registering" for _, phase in deliveries):
                                break
                            sleep(.05)
                finally:
                    with closing(sqlite3.connect(database)) as db:
                        db.execute("DELETE FROM live_simulcast_destination WHERE id LIKE 'li-capacity-fixture-%'")
                        db.commit()
                self.assertEqual([owner, owner], register_attempts)
                self.assertEqual([(item["id"], "registered")], deliveries)
                self.assertEqual([asset, asset], ingests)
                self.assertEqual([asset], links)
                self.assertEqual(2, len(scheduled_videos))
                self.assertEqual(2, len(announcement_writes))
                page.goto(base + "/operator/integrations")
                profile_path = base + "/api/integrations/linkedin-member-live/profile"
                self.assertEqual(403, api.get(profile_path).status)
                self.assertEqual(409, api.get(profile_path, headers=auth).status)
                page.get_by_role("button", name="Connect LinkedIn Profile Live", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text("LinkedIn Profile Live is connected.")
                page.get_by_role("button", name="Show LinkedIn Live profile", exact=True).click()
                expect(page.get_by_text("Profile FixtureMember1", exact=True)).to_be_visible()
                profile = api.get(profile_path, headers=auth)
                self.assertEqual(200, profile.status, profile.text())
                self.assertEqual(dict(id="FixtureMember1", urn="urn:li:person:FixtureMember1"), profile.json())
                self.assertNotIn("fixture-linkedin-member-live", page.content())

                member_schedule = schedule_path.replace("/linkedin/", "/linkedin-member/")
                self.assertEqual(403, api.post(member_schedule, headers=auth, data={"profile": "Wrong"}).status)
                member_existing = selected_path.replace("/linkedin/", "/linkedin-member/")
                member_catalog = api.get(member_existing, headers=auth)
                self.assertEqual(["5556"], [entry["id"] for entry in member_catalog.json()["items"]])
                member_selection = api.post(member_existing, headers=auth, data={"id": "5556"})
                self.assertEqual(201, member_selection.status, member_selection.text())
                self.assertEqual("urn:li:ugcPost:5557", member_selection.json()["post_urn"])
                self.assertIs(member_selection.json()["auto_prepare"], True)

                page.goto(base + "/operator/live-events")
                page.get_by_role("button", name="Open production for LinkedIn Live rehearsal", exact=False).first.click()
                page.get_by_text("Simulcast destinations", exact=True).click()
                page.get_by_role("combobox", name="LinkedIn account", exact=True).select_option("member")
                page.get_by_role("button", name="Load LinkedIn profile", exact=True).click()
                expect(page.get_by_role("checkbox", name="Create LinkedIn broadcasts for future dates")).to_be_checked()
                page.get_by_role("checkbox", name="Prepare LinkedIn delivery automatically").uncheck()
                page.get_by_role("button", name="Schedule and announce on LinkedIn", exact=True).click()
                expect(page.locator("#linkedinLivePost")).to_have_attribute(
                    "href", "https://www.linkedin.com/feed/update/urn:li:ugcPost:5555/")
                repeat_profile = api.post(member_schedule, headers=auth, data={"profile": "FixtureMember1"})
                self.assertEqual(200, repeat_profile.status, repeat_profile.text())
                self.assertIs(repeat_profile.json()["auto_prepare"], False)
                page.get_by_role("button", name="Prepare LinkedIn delivery", exact=True).click()
                expect(page.locator("#linkedinLiveStatus")).to_contain_text("CuePorch will publish")
                member_destination = api.post(member_schedule.removesuffix("/schedule") + "/prepare",
                                              headers=auth, data={})
                self.assertEqual(200, member_destination.status, member_destination.text())
                member_destination_id = member_destination.json()["id"]
                member_roster = api.get(targets, headers=auth)
                self.assertEqual("linkedin-member-live", next(entry["provider"] for entry in
                                 member_roster.json()["destinations"] if entry["id"] == member_destination_id))
                self.assertNotIn("fixture-member-live-key", member_roster.text() + page.content())
                check_destination_rtmps(root, database, member_destination_id, operator,
                                        "rtmps://live-ingest.linkedin.com:443/live", "fixture-member-live-key",
                                        "linkedin-live")
                with patch.object(event_simulcast.Simulcast, "state",
                                  lambda _self, uid, opens, dest: "sending" if dest == member_destination_id else "waiting"):
                    deadline = monotonic() + 8
                    while "bind" not in member_writes and monotonic() < deadline:
                        sleep(.05)
                self.assertEqual(["schedule", "announce", "register", "bind"], member_writes)
                page.get_by_role("button", name="Turn off LinkedIn Profile Live", exact=True).click()
                deadline = monotonic() + 16
                while "end" not in member_writes and monotonic() < deadline:
                    sleep(.05)
                self.assertEqual(["schedule", "announce", "register", "bind", "end"], member_writes)
                self.assertEqual([asset], ends)
                real_due = live_destination_series.due
                clock = datetime.fromisoformat(later["start"]) - timedelta(minutes=10)

                def live_rows(opens):
                    with closing(sqlite3.connect(database)) as db:
                        return db.execute(
                            "SELECT company,video,state,post FROM linkedin_live_creation "
                            "WHERE event=? AND opens=? ORDER BY company",
                            (data["id"], opens),
                        ).fetchall()

                with patch.object(live_destination_series, "due", lambda con, _: real_due(con, clock)):
                    deadline = monotonic() + 8
                    while monotonic() < deadline:
                        rows = live_rows(later["id"])
                        if len(rows) == 2 and all(row[2] == "scheduled" for row in rows):
                            break
                        sleep(.05)
                self.assertEqual(["1113", "5501"], [row[1] for row in rows])
                self.assertTrue(live_rows(first["id"]))

                announced_url = f"{path}/{later['id']}/destinations/linkedin/series"

                def announced_due():
                    with closing(sqlite3.connect(database)) as db:
                        db.execute("INSERT INTO live_occurrence VALUES (?,?,'fixture','fixture','ready')",
                                   (data["id"], later["id"]))
                        return [name for event_id, opens, name in linkedin_live_job.due_auto(db, later["id"])
                                if (event_id, opens) == (data["id"], later["id"])]

                self.assertEqual([company["company"]], announced_due())
                before = live_rows(later["id"])
                self.assertEqual(2, len(before))
                self.assertEqual(200, api.delete(announced_url, headers=auth, data={"date": True}).status)
                self.assertTrue(api.get(announced_url, headers=auth).json()["skipped"])
                self.assertEqual([], announced_due())
                restored = api.put(announced_url, headers=auth, data={"date": True})
                self.assertEqual(200, restored.status, restored.text())
                self.assertEqual([company["company"]], announced_due())
                self.assertFalse(api.get(announced_url, headers=auth).json()["skipped"])
                self.assertEqual(before, live_rows(later["id"]))  # same post id, no new write

                before = live_rows(skipped_date["id"])
                company_url = f"{path}/{skipped_date['id']}/destinations/linkedin/series"
                member_url = f"{path}/{skipped_date['id']}/destinations/linkedin-member/series"
                skipped = api.delete(company_url, headers=auth, data={"date": True})
                self.assertEqual(200, skipped.status, skipped.text())
                skipped = api.delete(member_url, headers=auth, data={"date": True})
                self.assertEqual(200, skipped.status, skipped.text())
                for url in (company_url, member_url):
                    state = api.get(url, headers=auth).json()
                    self.assertTrue(state["skipped"])
                    self.assertTrue(state["restorable"])
                    restored = api.put(url, headers=auth, data={"date": True})
                    self.assertEqual(200, restored.status, restored.text())
                    state = api.get(url, headers=auth).json()
                    self.assertFalse(state["skipped"])
                skipped = api.delete(company_url, headers=auth, data={"date": True})
                self.assertEqual(200, skipped.status, skipped.text())
                stopped = api.delete(member_url, headers=auth, data={"date": False})
                self.assertEqual(200, stopped.status, stopped.text())
                with closing(sqlite3.connect(database)) as db:
                    db.row_factory = sqlite3.Row
                    future_window = datetime.fromisoformat(skipped_date["start"]) - timedelta(minutes=10)
                    self.assertEqual([], list(real_due(db, future_window)))
                self.assertEqual(before, live_rows(skipped_date["id"]))
                with closing(sqlite3.connect(database)) as db:
                    db.execute("INSERT INTO live_occurrence VALUES (?,?,'fixture','fixture','ready')",
                               (data["id"], first["id"]))
                    db.commit()
                clock = datetime.fromtimestamp(first["id"] + 1, timezone.utc)
                with patch.object(event_simulcast, "datetime", OccurrenceClock):
                    with page.expect_response(lambda response: response.request.method == "PUT"
                                              and response.url == targets + "/" + member_destination_id) as result:
                        page.get_by_role("button", name="Turn on LinkedIn Profile Live", exact=True).click()
                    restarted = result.value
                self.assertEqual(201, restarted.status, restarted.text())
                new_destination = restarted.json()["id"]
                self.assertNotEqual(member_destination_id, new_destination)
                check_destination_rtmps(root, database, new_destination, operator,
                                        "rtmps://live-ingest.linkedin.com:443/live", "fixture-member-restart-key",
                                        "linkedin-live")
                with patch.object(event_simulcast.Simulcast, "state",
                                  lambda _self, uid, opens, dest: "sending" if dest == new_destination else "waiting"):
                    deadline = monotonic() + 8
                    while "restart_bind" not in member_writes and monotonic() < deadline:
                        sleep(.05)
                self.assertEqual(["schedule", "announce", "register", "restart_bind"], member_writes[-4:])
                roster = api.get(targets, headers=auth).json()["destinations"]
                self.assertNotIn(member_destination_id, [entry["id"] for entry in roster])
                self.assertEqual(["1113", "5501"], [row[1] for row in live_rows(later["id"])])
                page.goto(base + "/operator/integrations")
                member_id[0] = {"invalid": 1}
                self.assertEqual(502, api.get(profile_path, headers=auth).status)
                page.get_by_role("button", name="Disconnect LinkedIn Profile Live", exact=True).click()
                expect(page.get_by_role("button", name="Disconnect LinkedIn Live", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Disconnect LinkedIn Company Page", exact=True)).to_be_visible()
                self.assertEqual(409, api.get(profile_path, headers=auth).status)
                page.get_by_role("button", name="Disconnect LinkedIn Live", exact=True).click()
                expect(page.get_by_role("button", name="Connect LinkedIn Live", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Disconnect LinkedIn Company Page", exact=True)).to_be_visible()
                page.get_by_role("button", name="Disconnect LinkedIn Company Page", exact=True).click()
                expect(page.locator("#integrationStatus")).to_have_text(
                    "Disconnected here. You can remove CuePorch access in your LinkedIn Company Page account.")
                expect(page.get_by_role("button", name="Connect LinkedIn Company Page", exact=True)).to_be_visible()
                integrations = api.get(base + "/api/integrations", headers=auth).json()["integrations"]
                self.assertFalse(next(item for item in integrations if item["id"] == "linkedin")["connected"])
                self.assertEqual(["Fixture1", "Fixture2"], posts)
        print("LinkedIn Company Page OAuth, multipart restart and ordered ETags, opted-in approval, public posting,"
              " exact playback, ambiguous post result and disconnect passed.")
        print("Separate LinkedIn Live grant, paginated Page admin verification, and independent disconnect passed.")
        print("LinkedIn Live profile grant verified separate scopes, exact profile, invalid response, and independent disconnect.")
        print("LinkedIn Live Company Page browser scheduling, public announcement, optional approval and restart passed.")
        print("LinkedIn Live verified encrypted ingest, local RTMPS playback, provider-ready binding and end.")
        print("LinkedIn Live existing event picker verified paging, date and Page scope, and repeat and duplicate handling.")
        print("LinkedIn Live prepared exactly the opted-in ready occurrence and preserved manual delivery for the other.")
        print("LinkedIn profile scheduled and selected events, delivered decoded RTMPS, bound and ended in isolation.")


if __name__ == "__main__":
    unittest.main()
