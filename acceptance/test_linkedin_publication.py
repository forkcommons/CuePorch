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
import linkedin_upload as linkedin
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
            database = root / "workspace.sqlite"
            owner = "urn:li:organization:12345"
            received = {}
            posts = []
            failed_once = set()
            ready = set()
            approved_live_pages = set()
            page_offsets = []
            scheduled_videos = []
            announcement_writes = []
            ingests = []
            links = []
            ends = []
            sending = set()
            ready_to_link = set()
            status_requests = []
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
            ), API=origin + "/rest", V2_API=origin + "/v2"))
            cleanup.enter_context(patch.object(social_publication, "RETRY_SECONDS", (3, 3)))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_PUBLIC_URL": "", "CUEPORCH_LINKEDIN_CLIENT_ID": "cueporch",
                "CUEPORCH_LINKEDIN_CLIENT_SECRET": "fixture-secret", "CUEPORCH_LINKEDIN_COMPANY_ID": "12345",
                "CUEPORCH_LINKEDIN_LIVE_CLIENT_ID": "cueporch-live",
                "CUEPORCH_LINKEDIN_LIVE_CLIENT_SECRET": "fixture-live-secret",
                "CUEPORCH_LINKEDIN_LIVE_COMPANY_ID": "12345", "CUEPORCH_LINKEDIN_LIVE_REGION": "WEST_US",
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

            @remote.post("/v2/liveVideos")
            def live_videos():
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
                assert request.headers["Authorization"] == "Bearer fixture-linkedin-live"
                action = request.args["action"]
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
                             schedule=dict(timezone="UTC", repeat="days", count=2,
                                           start=starts.strftime("%Y-%m-%dT%H:%M"),
                                           end=(starts + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")))
                created = api.post(base + "/api/live-events", headers=auth, data=event)
                self.assertTrue(created.ok, created.text())
                data = created.json()
                first, next_date = data["occurrences"]
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
                page.goto(base + "/operator/integrations")
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
        print("LinkedIn Live Company Page browser scheduling, public announcement, optional approval and restart passed.")
        print("LinkedIn Live verified encrypted ingest, local RTMPS playback, provider-ready binding and end.")


if __name__ == "__main__":
    unittest.main()
