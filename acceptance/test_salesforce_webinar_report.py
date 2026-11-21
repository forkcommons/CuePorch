import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep, time
from unittest.mock import Mock, patch

from browser import chromium
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

import event_salesforce_registration
import library_events
import salesforce_outbound as delivery
import test_salesforce_roster as roster

from event_media import Gateway, local_hls
from test_salesforce_outbound import Provider, EMAIL, REVISION
from test_marketo_connection import _resp
from test_drive_import import QuietHandler, service_worker

PEER = "https://peer.example"
REMOTE = "webinar-fixture"


def _description(object_name, field_specs):
    return {
        "name": object_name,
        "createable": True,
        "updateable": True,
        "fields": [
            {
                "name": field_name,
                "createable": True,
                "updateable": True,
                "externalId": is_key,
                "unique": is_key,
            }
            for field_name, is_key in field_specs
        ],
    }


_OBJECT_SPECS = {
    "Lead": [
        (delivery.LEAD_KEY, True),
        ("Email", False),
        ("Company", False),
        ("LastName", False),
        ("FirstName", False),
    ],
    delivery.MEDIA: [
        (delivery.EXTERNAL_KEY, True),
        ("Name", False),
        ("Campaign__c", False),
        ("Kind__c", False),
    ],
    delivery.REGISTRANT: [
        (delivery.EXTERNAL_KEY, True),
        ("Media__c", False),
        ("Lead__c", False),
        ("Contact__c", False),
        ("Status__c", False),
        ("Registered_At__c", False),
        ("Watch_Percent__c", False),
    ],
}


def describe(object_name):
    return _resp(_description(object_name, _OBJECT_SPECS[object_name]))


class WebinarProvider(roster.RosterProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outbound = Provider()
        self.outbound.campaign = roster.TARGET

    def request(self, method, url, **kwargs):
        prefix = self.outbound.origin + "/services/data/v64.0/"
        params = kwargs.get("params") or {}
        q = params.get("q", "") if isinstance(params, dict) else ""
        delegate = url.startswith(prefix) and (
            "FROM Contact" in q
            or "FROM Lead" in q
            or "sobjects/Lead" in url
            or "sobjects/CuePorch_" in url
        )
        if not delegate:
            return super().request(method, url, **kwargs)

        self._auth(kwargs)
        path = url.removeprefix(prefix)

        if url.endswith("/describe"):
            return describe(path.split("/")[1])

        return self.outbound.api(
            REVISION,
            method,
            url,
            before_send=lambda: None,
            **kwargs
        )

class TestSalesforceWebinarReport(roster.TestSalesforceRoster):
    def test_roster(self):
        provider = WebinarProvider()
        original_start = event_salesforce_registration.start

        def start(app, tx, database, connection, validate, **kwargs):
            provider.database = database
            provider.tx = tx
            kwargs["clock"] = lambda: provider.now
            return original_start(app, tx, database, connection, validate, **kwargs)

        with patch.object(roster, "RosterProvider", return_value=provider):
            with patch.object(event_salesforce_registration, "start", side_effect=start):
                super().test_roster()

    def exercise(self, client, headers, provider, app):
        start = datetime.now(timezone.utc) - timedelta(minutes=2)
        end = datetime.now(timezone.utc) + timedelta(minutes=10)
        response = client.post("/api/live-events", headers=headers, json={
            "title": "Salesforce live reporting fixture",
            "slug": "salesforce-live-reporting",
            "visibility": "public",
            "type": "webinar",
            "latency": "standard",
            "save_replay": False,
            "schedule": {
                "timezone": "UTC",
                "repeat": "none",
                "start": start.strftime("%Y-%m-%dT%H:%M"),
                "end": end.strftime("%Y-%m-%dT%H:%M"),
            },
        })
        self.assertIn(response.status_code, (200, 201))
        event = response.get_json()
        opens = event["occurrences"][0]["id"]
        registration = f"/api/live-events/{event['id']}/registration"
        options = client.get(registration, headers=headers).get_json()
        options["settings"]["enabled"] = True
        options["settings"]["attendance_analytics"] = True
        self.assertEqual(client.put(registration, headers=headers, json=options).status_code, 200)

        url = f"/api/live-events/{event['id']}/{opens}/registration/salesforce/outbound"
        with provider.tx() as con:
            con.execute("INSERT INTO live_occurrence VALUES (?,?,?,?,?)", (event["id"], opens, PEER, REMOTE, "ready"))
        result = client.put(url, headers=headers, json={"campaign": roster.TARGET, "engagement": True})
        self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
        self.observe_browser(client, headers, provider, app, event["id"], opens, url)

    def observe_browser(self, client, headers, provider, app, uid, opens, url):
        directory = Path(provider.database).parent
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=30",
                "-t",
                "6",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "30",
                "-sc_threshold",
                "0",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_playlist_type",
                "vod",
                "-hls_segment_filename",
                str(directory / "0-%d.ts"),
                str(directory / "0.m3u8"),
            ],
            check=True,
            timeout=20,
        )
        metadata = {
            "isLive": True,
            "privacy": {"id": 3},
            "state": {"id": 1},
            "streamingPlaylists": [
                {
                    "type": 1,
                    "playlistUrl": f"{PEER}/static/streaming-playlists/hls/private/{REMOTE}/0.m3u8",
                }
            ],
        }
        with patch.object(Gateway, "metadata", return_value=metadata), \
                patch.object(
                    Gateway,
                    "media",
                    side_effect=lambda origin, remote, gateway, filename, byte_range: local_hls(
                        directory, filename
                    ),
                ):
            self.play_fixture(app, opens)
        self.verify_report(client, headers, provider, uid, opens, url)

    def play_fixture(self, app, opens):
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        with service_worker(server), sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/events/salesforce-live-reporting?occurrence={opens}")
            page.fill("input[name=email]", EMAIL)
            page.fill("input[name=first_name]", "Ada")
            page.fill("input[name=last_name]", "Example")
            page.check("input[name=salesforce_consent]")
            page.check("input[name=salesforce_engagement_consent]")
            page.click("form button[type=submit]")
            expect(page.locator("#eventStatus")).to_have_text("Live")
            video = page.locator("#eventPlayer video")
            video.evaluate("video => {video.muted = true; return video.play();}")
            page.wait_for_function(
                "() => { const v = document.querySelector('#eventPlayer video'); "
                "return v && v.currentTime > 0.9 && v.getVideoPlaybackQuality().totalVideoFrames > 0; }"
            )
            with page.expect_response(
                lambda r: r.status == 204
                and "/api/event-playback" in r.url
                and r.request.method == "POST"
                and r.request.post_data_json.get("watched", 0) > 0
            ):
                video.evaluate("video => video.pause()")

    def verify_report(self, client, headers, provider, uid, opens, url):
        def receipt():
            return client.get(url, headers=headers).get_json()

        provider.now = time() + 1

        deadline = monotonic() + 8
        while monotonic() < deadline:
            if receipt()["states"].get("sent") == 1:
                break
            sleep(0.05)
        else:
            self.fail(f"Registration states after deadline: {receipt()['states']}")

        with provider.tx() as con:
            watched = int(con.execute(
                "SELECT SUM(p.watched) "
                "FROM live_playback p "
                "JOIN live_attendee_session a ON a.session = p.id "
                "WHERE p.event = ? AND p.opens = ? AND p.played = 1",
                (uid, opens),
            ).fetchone()[0])
        assert watched > 0

        host = Mock()
        host.call.return_value.json.return_value = dict(
            isLive=False,
            privacy={"id": 3},
            state={"id": 1},
            duration=6,
        )
        library_events.observe_delivery(
            provider.tx, provider.database, PEER, lambda _: host
        )

        report_at = receipt()["engagement_report_at"]
        self.assertGreater(report_at, time() + 3500)
        self.assertNotIn("PATCH", provider.outbound.writes)


        provider.now = report_at + 1
        deadline = monotonic() + 8
        while "PATCH" not in provider.outbound.writes and monotonic() < deadline:
            sleep(0.1)
        assert provider.outbound.writes.count("PATCH") == 1
        registrants = [record for (name, _), record in provider.outbound.objects.items() if name == delivery.REGISTRANT]
        (registrant,) = registrants
        assert registrant["Status__c"] == "Attended Live" and registrant["Watch_Percent__c"] == round(min(100, watched / 60))
