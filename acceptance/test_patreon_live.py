import json
import secrets
import sqlite3
import sys
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from flask import Flask
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


class PatreonLive(unittest.TestCase):
    def test_occurrence_scoped_destination(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps(dict(institution="station", schema=1, works=[]))
            )
            database = root / "workspace.sqlite"
            sqlite3.connect(database).close()
            operator = secrets.token_urlsafe(32)
            remote = Flask("cueporch-service")
            service = make_server(
                "127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True
            )
            base = f"http://127.0.0.1:{service.server_port}"
            cleanup.enter_context(
                patch.dict("os.environ", {"CUEPORCH_PUBLIC_URL": base})
            )
            cleanup.enter_context(patch("event_peer.host_available", return_value=True))
            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, database, operator, "station")
            stops = [
                stop
                for name, stop in app.extensions.items()
                if name.startswith("stop_")
            ]

            def stop_app():
                while stops:
                    stops.pop()()

            cleanup.callback(stop_app)
            service.app = app
            cleanup.enter_context(service_worker(service))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                self.exercise(browser, base, operator, database)

    def exercise(self, browser, base, operator, database):
        now = datetime.now(timezone.utc)
        page = browser.new_page()
        page.add_init_script(
            f"sessionStorage.setItem('cueporchOperator', {json.dumps(operator)})"
        )
        request = page.context.request
        auth = {"Authorization": "Bearer " + operator}
        event = dict(
            title="Patreon rehearsal",
            slug="patreon-rehearsal",
            visibility="public",
            type="live",
            latency="standard",
            save_replay=False,
            schedule=dict(
                timezone="UTC",
                repeat="days",
                count=2,
                start=(now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                end=(now + timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M"),
            ),
        )
        response = request.post(base + "/api/live-events", headers=auth, data=event)
        assert response.ok
        created = response.json()
        first, tomorrow = created["occurrences"]
        prefix = f"{base}/api/live-events/{created['id']}"
        production = prefix + f"/{first['id']}"
        next_production = prefix + f"/{tomorrow['id']}"
        targets = production + "/destinations"
        next_targets = next_production + "/destinations"
        response = request.post(
            targets,
            headers=auth,
            data={
                "name": "Station feed",
                "server": "rtmp://127.0.0.1:41998/live",
                "key": "fixture-station-key",
            },
        )
        assert response.ok
        response = request.post(
            targets,
            headers=auth,
            data={
                "name": "Bad",
                "preset": "patreon",
                "server": "rtmp://127.0.0.1:41998/live",
                "key": "fixture-station-key",
            },
        )
        assert response.status == 422
        page.goto(base + "/operator/live-events")
        page.get_by_role(
            "button", name="Open production for Patreon rehearsal", exact=False
        ).first.click()
        page.get_by_text("Simulcast destinations", exact=True).click()
        form = page.locator("#addDestination")
        form.get_by_label("Destination type").select_option("patreon")
        expect(page.locator("#patreonDestinationHelp")).to_be_visible()
        form.get_by_label("Destination name").fill("Members live")
        form.get_by_label("RTMP or RTMPS server").fill("rtmps://127.0.0.1:41999/live")
        form.get_by_label("Destination stream key").fill("fixture-patreon-key")
        form.get_by_role("button", name="Add destination").click()
        expect(form.get_by_label("Destination stream key")).to_have_value("")
        expect(page.get_by_text("Patreon Live for this date")).to_be_visible()

        current = request.get(targets, headers=auth)
        next_response = request.get(next_targets, headers=auth)
        assert current.status == 200
        assert next_response.status == 200

        current_text = current.text()
        next_text = next_response.text()
        assert "fixture-patreon-key" not in current_text
        assert "fixture-patreon-key" not in next_text

        current_by_name = {row["name"]: row for row in current.json()["destinations"]}
        next_by_name = {
            row["name"]: row for row in next_response.json()["destinations"]
        }
        assert set(current_by_name) == {"Station feed", "Members live"}
        assert set(next_by_name) == {"Station feed"}
        assert current_by_name["Members live"]["opens"] == int(first["id"])
        assert current_by_name["Station feed"]["opens"] is None

        member_id = current_by_name["Members live"]["id"]

        assert (
            request.put(
                next_targets + "/" + member_id, headers=auth, data={"enabled": False}
            ).status
            == 404
        )
        assert (
            request.delete(next_targets + "/" + member_id, headers=auth).status == 404
        )

        conn = sqlite3.connect(database)
        try:
            stored = conn.execute(
                "SELECT secret FROM live_simulcast_destination WHERE id=?",
                (member_id,),
            ).fetchone()
        finally:
            conn.close()
        assert stored is not None
        assert b"fixture-patreon-key" not in stored[0]

        assert request.delete(targets + "/" + member_id, headers=auth).status == 204


if __name__ == "__main__":
    unittest.main()
