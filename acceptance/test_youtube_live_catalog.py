import base64
import hashlib
import json
import secrets
import sqlite3
import sys
import unittest
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlencode

from flask import Flask, redirect, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from installed_simulcast import wait_until
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import event_simulcast
from institution import create_app
import youtube_live
from youtube_rtmps_fixture import check_youtube_rtmps


class YouTubeLiveCatalog(unittest.TestCase):
    def test_connect_catalog_pages_refresh_disconnect(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            remote = Flask("youtube-live-fixture")
            provider = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            origin = f"http://127.0.0.1:{provider.server_port}"
            service = make_server("127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True)
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/youtube-live/callback"
            observed = dict(refresh=False, revoked=False, uploaded=False)
            observed.update(broadcast_pages=[], broadcast_fetch=False, stream_fetch=False)
            observed.update(single_channel=False, show_created=False, writes=[])
            created_broadcast, created_stream = {}, {}
            cleanup.enter_context(patch("event_peer.host_available", return_value=True))
            raw_secrets = ("old-live-access", "fresh-live-access", "live-refresh", "fixture-secret")
            cleanup.enter_context(patch.multiple(
                youtube_live,
                OAUTH=replace(
                    youtube_live.OAUTH,
                    authorization_url=origin + "/oauth/authorize",
                    token_url=origin + "/oauth/token",
                    revoke_url=origin + "/oauth/revoke",
                ),
                API=origin,
            ))
            cleanup.enter_context(patch.dict("os.environ", {
                "CUEPORCH_PUBLIC_URL": "",
                "CUEPORCH_YOUTUBE_LIVE_CLIENT_ID": "cueporch",
                "CUEPORCH_YOUTUBE_LIVE_CLIENT_SECRET": "fixture-secret",
            }))
            operator = secrets.token_urlsafe(32)
            database = root / "workspace.sqlite"
            scheduler_targets = set()
            original_update = event_simulcast.Simulcast.update

            def capture_candidates(self, candidates):
                nonlocal scheduler_targets
                if self.directory.name == "event-simulcast":
                    scheduler_targets = {row["id"] for _, _, rows, _ in candidates.values() for row in rows}
                    return
                original_update(self, candidates)

            cleanup.enter_context(patch.object(event_simulcast.Simulcast, "update", capture_candidates))

            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, database, operator, "station")
            stops = [stop for name, stop in app.extensions.items() if name.startswith("stop_")]

            def stop_app():
                while stops:
                    stops.pop()()

            cleanup.callback(stop_app)
            service.app = app

            @remote.get("/oauth/authorize")
            def authorize():
                assert request.args["client_id"] == "cueporch"
                assert request.args["redirect_uri"] == callback
                assert request.args["scope"] == youtube_live.OAUTH.scope
                assert request.args["code_challenge_method"] == "S256"
                observed["challenge"] = request.args["code_challenge"]
                return redirect(callback + "?" + urlencode(dict(state=request.args["state"], code="live-code")))

            @remote.post("/oauth/token")
            def token():
                assert request.form["client_id"] == "cueporch"
                assert request.form["client_secret"] == "fixture-secret"
                if request.form["grant_type"] == "authorization_code":
                    assert request.form["code"] == "live-code"
                    assert request.form["redirect_uri"] == callback
                    verifier = request.form["code_verifier"]
                    assert verifier
                    challenge = base64.urlsafe_b64encode(
                        hashlib.sha256(verifier.encode()).digest()
                    ).rstrip(b"=").decode()
                    assert challenge == observed["challenge"]
                    access, refresh = "old-live-access", "live-refresh"
                else:
                    assert request.form["grant_type"] == "refresh_token"
                    assert request.form["refresh_token"] == "live-refresh"
                    observed["refresh"] = True
                    access, refresh = "fresh-live-access", "live-refresh"
                return dict(
                    access_token=access,
                    refresh_token=refresh,
                    scope="https://www.googleapis.com/auth/youtube.force-ssl",
                    token_type="Bearer",
                    expires_in=3600,
                )

            @remote.post("/oauth/revoke")
            def revoke():
                assert request.form["token"] == "live-refresh"
                observed["revoked"] = True
                return "", 200

            @remote.post("/upload")
            def upload():
                observed["uploaded"] = True
                return "", 200

            @remote.get("/channels")
            def channels():
                assert request.args["mine"] == "true"
                assert request.args["part"] == "id,snippet"
                assert request.args["maxResults"] == "50"
                if observed["revoked"]:
                    return "revoked", 401
                auth = request.headers["Authorization"]
                if auth == "Bearer old-live-access":
                    return "expired", 401
                assert auth == "Bearer fresh-live-access"
                if request.args.get("pageToken") == "live-page-2":
                    return {"items": [{"id": "UC-live-2", "snippet": {"title": "Porch Live Two"}}]}
                if observed["single_channel"]:
                    return {"items": [{"id": "UC-live-1", "snippet": {"title": "Porch Live One"}}]}
                return {
                    "items": [{"id": "UC-live-1", "snippet": {"title": "Porch Live One"}}],
                    "nextPageToken": "live-page-2",
                }

            broadcast = {
                "id": "fixture-broadcast",
                "snippet": {
                    "channelId": "UC-live-1",
                    "title": "Porch Simulcast",
                    "scheduledStartTime": "2030-01-01T12:00:00Z",
                },
                "status": {"privacyStatus": "private", "lifeCycleStatus": "ready"},
                "contentDetails": {
                    "boundStreamId": "fixture-stream",
                    "enableAutoStart": True,
                    "enableAutoStop": True,
                },
            }
            non_eligible = {
                "id": "fixture-not-eligible",
                "snippet": {"channelId": "UC-live-1", "title": "Not eligible broadcast"},
                "status": {"privacyStatus": "public", "lifeCycleStatus": "complete"},
            }
            second_broadcast = {
                "id": "fixture-broadcast-2",
                "snippet": {
                    "channelId": "UC-live-1",
                    "title": "Porch Simulcast Two",
                    "scheduledStartTime": "2030-01-02T12:00:00Z",
                },
                "status": {"privacyStatus": "private", "lifeCycleStatus": "ready"},
                "contentDetails": {
                    "boundStreamId": "fixture-stream-2",
                    "enableAutoStart": True,
                    "enableAutoStop": True,
                },
            }

            @remote.get("/liveBroadcasts")
            def live_broadcasts():
                if request.args.get("id") == "created-broadcast":
                    return {"items": [created_broadcast]}
                if request.args["part"] == "id,snippet":
                    assert request.args["mine"] == "true"
                    return {"items": [created_broadcast] if observed["show_created"] else []}
                if request.args.get("id"):
                    assert request.args["id"] == "fixture-broadcast"
                    observed["broadcast_fetch"] = True
                    return {"items": [broadcast]}
                assert request.args["mine"] == "true"
                if request.args.get("pageToken") == "broadcast-page-2":
                    observed["broadcast_pages"].append(2)
                    return {"items": [second_broadcast]}
                observed["broadcast_pages"].append(1)
                return {"items": [broadcast, non_eligible], "nextPageToken": "broadcast-page-2"}

            @remote.get("/liveStreams")
            def live_streams():
                if request.args.get("id") == "created-stream":
                    return {"items": [created_stream]}
                assert request.args["id"] == "fixture-stream"
                observed["stream_fetch"] = True
                return {"items": [{
                    "id": "fixture-stream",
                    "snippet": {"channelId": "UC-live-1", "title": "Porch Simulcast Stream"},
                    "cdn": {
                        "ingestionType": "rtmp",
                        "ingestionInfo": {
                            "rtmpsIngestionAddress": "rtmps://a.rtmps.youtube.com:443/live2",
                            "streamName": "fixture-live-stream-key",
                        },
                    },
                }]}

            @remote.post("/liveBroadcasts")
            def insert_broadcast():
                assert request.headers["Authorization"] == "Bearer fresh-live-access"
                assert request.args["part"] == "id,snippet,status,contentDetails"
                body = request.get_json()
                assert body["status"]["privacyStatus"] == "private"
                assert body["contentDetails"] == {"enableAutoStart": True, "enableAutoStop": True}
                assert body["snippet"]["description"].startswith("Created by CuePorch (")
                assert datetime.fromisoformat(body["snippet"]["scheduledStartTime"]) < datetime.fromisoformat(
                    body["snippet"]["scheduledEndTime"])
                created_broadcast.update(body, id="created-broadcast")
                created_broadcast["snippet"]["channelId"] = "UC-live-1"
                created_broadcast["status"]["lifeCycleStatus"] = "created"
                observed["writes"].append("broadcast")
                return {"error": "simulated loss after creating broadcast"}, 503

            @remote.post("/liveStreams")
            def insert_stream():
                assert request.headers["Authorization"] == "Bearer fresh-live-access"
                assert request.args["part"] == "id,snippet,cdn,contentDetails"
                body = request.get_json()
                assert body["cdn"] == {"resolution": "variable", "frameRate": "variable", "ingestionType": "rtmp"}
                assert body["contentDetails"] == {"isReusable": False}
                assert body["snippet"]["description"] == created_broadcast["snippet"]["description"]
                created_stream.update(body, id="created-stream")
                created_stream["snippet"]["channelId"] = "UC-live-1"
                created_stream["cdn"]["ingestionInfo"] = {
                    "rtmpsIngestionAddress": "rtmps://a.rtmps.youtube.com:443/live2",
                    "streamName": "fixture-created-stream-key",
                }
                observed["writes"].append("stream")
                return created_stream

            @remote.post("/liveBroadcasts/bind")
            def bind_broadcast():
                assert request.headers["Authorization"] == "Bearer fresh-live-access"
                assert request.args["part"] == "id,snippet,contentDetails"
                assert request.args["id"] == created_broadcast["id"]
                assert request.args["streamId"] == created_stream["id"]
                assert not request.get_data()
                created_broadcast["status"]["lifeCycleStatus"] = "ready"
                created_broadcast["contentDetails"]["boundStreamId"] = "created-stream"
                observed["writes"].append("bind")
                return {"error": "simulated loss after binding"}, 503

            cleanup.enter_context(service_worker(provider))
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                api = page.context.request
                self.assertEqual(403, api.get(base + "/api/integrations/youtube-live/channels").status)
                page.add_init_script(
                    f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator)});"
                )
                page.goto(base + "/operator/integrations")
                expect(page.get_by_role("heading", name="YouTube Live")).to_be_visible()
                page.get_by_role("button", name="Connect YouTube Live", exact=True).click()
                expect(page.locator("#integrationStatus")).to_contain_text("YouTube Live is connected.", timeout=15000)
                listed = api.get(
                    base + "/api/integrations/youtube-live/channels",
                    headers={"Authorization": f"Bearer {operator}"},
                )
                self.assertEqual(200, listed.status)
                for secret in raw_secrets:
                    self.assertNotIn(secret, listed.text())
                page.get_by_role("button", name="Show YouTube channels", exact=True).click()
                expect(page.get_by_text("Porch Live One")).to_be_visible(timeout=15000)
                page.get_by_role("button", name="More YouTube channels", exact=True).click()
                expect(page.get_by_text("Porch Live Two")).to_be_visible(timeout=15000)
                self.assertTrue(observed["refresh"])
                for secret in raw_secrets:
                    self.assertNotIn(secret, page.content())
                now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
                schedule = {
                    "timezone": "UTC",
                    "repeat": "days",
                    "count": 2,
                    "start": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                    "end": (now + timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M"),
                }
                created = api.post(
                    base + "/api/live-events",
                    headers={"Authorization": f"Bearer {operator}"},
                    data={
                        "title": "YouTube rehearsal",
                        "slug": "youtube-rehearsal",
                        "visibility": "public",
                        "type": "live",
                        "latency": "standard",
                        "save_replay": False,
                        "schedule": schedule,
                    },
                )
                self.assertTrue(created.ok)
                created_body = created.json()
                event_id = created_body["id"]
                occurrences = created_body["occurrences"]
                occurrence_id = occurrences[0]["id"]
                self.assertIsInstance(event_id, str)
                self.assertIsInstance(occurrence_id, int)
                destinations_url = base + f"/api/live-events/{event_id}/{occurrence_id}/destinations"
                operator_headers = {"Authorization": f"Bearer {operator}"}

                def destination_rows(response):
                    return response.json()["destinations"]

                events = browser.new_page()
                events.add_init_script(
                    f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator)});"
                )
                events.goto(base + "/operator/live-events")
                events.get_by_role("button", name="Open production for YouTube rehearsal", exact=False).first.click()
                events.get_by_text("Simulcast destinations", exact=True).click()
                events.get_by_role("button", name="Load YouTube broadcasts", exact=True).click()
                expect(events.locator("#youtubeBroadcastList option[value=fixture-broadcast]")).to_have_count(1)
                expect(events.locator("#youtubeBroadcastList option[value=fixture-not-eligible]")).to_have_count(0)
                events.get_by_role("button", name="More YouTube broadcasts", exact=True).click()
                expect(events.locator("#youtubeBroadcastList option[value=fixture-broadcast-2]")).to_have_count(1)
                events.get_by_role("combobox", name="YouTube broadcast", exact=True).select_option(value="fixture-broadcast")
                events.get_by_role("button", name="Add YouTube destination", exact=True).click()
                expect(events.get_by_role("button", name="Turn off Porch Simulcast")).to_be_enabled()

                listed = api.get(destinations_url, headers=operator_headers)
                self.assertTrue(listed.ok)
                rows = destination_rows(listed)
                self.assertEqual(1, len(rows))
                destination = rows[0]
                self.assertEqual("waiting", destination["state"])
                self.assertNotIn("fixture-live-stream-key", listed.text())

                with closing(sqlite3.connect(database)) as conn:
                    secret_row = conn.execute(
                        "SELECT secret FROM live_simulcast_destination WHERE id = ?",
                        (destination["id"],),
                    ).fetchone()
                self.assertIsNotNone(secret_row)
                self.assertIsInstance(secret_row[0], bytes)
                self.assertNotIn(b"fixture-live-stream-key", secret_row[0])
                self.assertNotIn("fixture-live-stream-key", events.content())

                self.assertTrue(observed["broadcast_fetch"])
                self.assertTrue(observed["stream_fetch"])
                self.assertEqual([1, 2], observed["broadcast_pages"])

                other_occurrence_id = occurrences[1]["id"]
                other = api.get(
                    base + f"/api/live-events/{event_id}/{other_occurrence_id}/destinations",
                    headers=operator_headers,
                )
                self.assertTrue(other.ok)
                self.assertEqual([], destination_rows(other))

                self.assertEqual(
                    403,
                    api.post(
                        destinations_url + "/youtube",
                        headers=operator_headers,
                        data={"broadcast": "fixture-broadcast", "channel": "unowned"},
                    ).status,
                )
                self.assertEqual(
                    409,
                    api.post(
                        destinations_url + "/youtube",
                        headers=operator_headers,
                        data={"broadcast": "fixture-broadcast", "channel": "UC-live-1"},
                    ).status,
                )

                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute(
                        "INSERT INTO live_occurrence(event,opens,origin,remote,delivery) VALUES (?,?,?,?,'ready')",
                        (event_id, occurrence_id, base, "fixture-remote"),
                    )

                wait_until(lambda: destination["id"] in scheduler_targets, 10, "The released YouTube destination was not selected.")
                check_youtube_rtmps(root, database, destination["id"], operator)

                observed["single_channel"] = True
                start = now + timedelta(hours=1)
                future = {
                    "title": "Future channel broadcast", "slug": "future-channel-broadcast",
                    "visibility": "public", "type": "live", "latency": "standard", "save_replay": False,
                    "schedule": dict(schedule, count=1, start=start.strftime("%Y-%m-%dT%H:%M"),
                                     end=(start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")),
                }
                scheduled = api.post(base + "/api/live-events", headers=operator_headers, data=future)
                self.assertTrue(scheduled.ok, scheduled.text())
                creation = base + f"/api/live-events/{scheduled.json()['id']}/{int(start.timestamp())}/destinations/youtube/create"
                self.assertEqual(403, api.post(creation, data={}).status)

                creation_page = browser.new_page()
                creation_page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator)});")
                creation_page.goto(base + "/operator/live-events")
                creation_page.get_by_role("button", name="Open production for Future channel broadcast", exact=False).first.click()
                creation_page.get_by_text("Simulcast destinations", exact=True).click()
                creation_page.get_by_role("button", name="Create YouTube broadcast").click()
                check_status = creation_page.get_by_role("button", name="Check creation status")
                expect(check_status).to_be_enabled()
                self.assertEqual(["broadcast"], observed["writes"])

                stop_app()
                with patch("institution.library_worker.start", return_value=lambda: None):
                    service.app = create_app(root, database, operator, "station")
                stops.extend(stop for name, stop in service.app.extensions.items() if name.startswith("stop_"))
                observed["show_created"] = True
                check_status.click()
                expect(check_status).to_be_enabled()
                self.assertEqual(["broadcast", "stream", "bind"], observed["writes"])
                check_status.click()
                expect(creation_page.get_by_text("YouTube broadcast created.")).to_be_visible()
                expect(creation_page.get_by_role("button", name="Turn off Future channel broadcast")).to_be_enabled()
                repeat = api.post(creation, headers=operator_headers, data={})
                self.assertEqual(200, repeat.status)
                self.assertEqual("complete", repeat.json()["state"])
                self.assertNotIn("fixture-created-stream-key", repeat.text() + creation_page.content())
                self.assertEqual(["broadcast", "stream", "bind"], observed["writes"])
                creation_page.close()

                policy = api.put(base + "/api/policy/publication", headers=operator_headers, data={"required": True})
                self.assertTrue(policy.ok, policy.text())
                held = api.post(base + "/api/live-events", headers=operator_headers,
                                data=dict(future, title="Needs publication approval", slug="needs-publication-approval"))
                self.assertTrue(held.ok, held.text())
                held_path = base + f"/api/live-events/{held.json()['id']}/{int(start.timestamp())}/destinations/youtube/create"
                refused = api.post(held_path, headers=operator_headers, data={})
                self.assertEqual(409, refused.status)
                self.assertEqual(["broadcast", "stream", "bind"], observed["writes"])

                page.get_by_role("button", name="Disconnect YouTube Live", exact=True).click()
                expect(page.locator("#integrationStatus")).to_contain_text("YouTube Live disconnected.", timeout=15000)
                self.assertTrue(observed["revoked"])
                wait_until(lambda: destination["id"] not in scheduler_targets, 10, "The revoked YouTube destination remained selected.")
                listed = api.get(destinations_url, headers=operator_headers)
                self.assertTrue(listed.ok)
                rows = destination_rows(listed)
                self.assertEqual(1, len(rows))
                self.assertEqual("disconnected", rows[0]["state"])

                events.get_by_role("button", name="Refresh destinations", exact=True).click()
                expect(events.locator("#destinationList").get_by_text("Disconnected", exact=False)).to_be_visible()
                expect(events.get_by_role("button", name="Turn off Porch Simulcast")).to_be_disabled()

                events.close()
                self.assertFalse(observed["uploaded"])

                after = api.get(
                    base + "/api/integrations/youtube-live/channels",
                    headers={"Authorization": f"Bearer {operator}"},
                )
                self.assertEqual(409, after.status)
                self.assertFalse(observed["uploaded"])

                invite = api.post(base + "/api/team/invitations",
                    headers={"Authorization": "Bearer " + operator, "X-CuePorch-Team": "1"}, data={"email": "admin@example.org", "role": "admin"})
                self.assertEqual(invite.status, 201)
                invite_token = invite.json()["path"].split("#", 1)[1]
                with browser.new_context() as team:
                    joined = team.request.post(base + "/api/team/join",
                        headers={"X-CuePorch-Team": "1"}, data={"invitation": invite_token, "name": "Admin", "password": "CuePorch team rehearsal password!"})
                    self.assertEqual(joined.status, 201)
                    connect = team.request.post(base + "/api/integrations/youtube-live/connect",
                        headers={"Authorization": "Bearer team-session", "X-CuePorch-Integration": "1"}, data={})
                    self.assertEqual(connect.status, 200)
                    authorization_url = connect.json()[
                        "authorization_url"]
                    signed_out = team.request.delete(base + "/api/team/session",
                        headers={"X-CuePorch-Team": "1"}, data={})
                    self.assertTrue(signed_out.ok)
                    oauth_page = team.new_page()
                    oauth_response = oauth_page.goto(authorization_url)
                    self.assertEqual(oauth_response.status, 403)
                    oauth_page.close()
                channels = api.get(base + "/api/integrations/youtube-live/channels",
                    headers={"Authorization": "Bearer " + operator, "X-CuePorch-Team": "1"})
                self.assertEqual(channels.status, 409)
        print("YouTube Live OAuth PKCE, catalog page one/two via browser, 409 after disconnect and revoke passed.")
        print("YouTube creation, restart reconciliation, stream binding, repeat and configurable publication gating passed.")


if __name__ == "__main__":
    unittest.main()
