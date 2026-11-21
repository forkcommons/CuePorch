"""Verify 30-day activity, resource scope and read state through the real workspace."""

from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
import uuid

from playwright.sync_api import expect, sync_playwright
import requests
from werkzeug.serving import make_server

from browser import chromium
from installed_team_accounts import PASSWORD, TEAM

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


def check_tour(page, steps):
    open_tour = page.get_by_role("button", name="Take a tour")
    progress = page.locator("#workspaceTourProgress")
    mark = page.locator("#workspaceTourMark")

    def step(position):
        anchor, title = steps[position - 1]
        expect(page.locator(f"{anchor}.workspace-tour-highlight + #workspaceTourMark")).to_be_visible()
        expect(mark.get_by_role("heading", name=title)).to_be_focused()
        expect(progress).to_have_text(f"Step {position} of {len(steps)}")

    expect(mark).to_be_hidden()
    open_tour.focus()
    page.keyboard.press("Enter")
    step(1)
    expect(mark.get_by_role("button", name="Back")).to_be_disabled()
    mark.get_by_role("button", name="Next").click()
    step(2)
    mark.get_by_role("button", name="Back").click()
    step(1)
    page.keyboard.press("Escape")
    expect(open_tour).to_be_focused()
    expect(mark).to_be_hidden()
    expect(page.locator(".workspace-tour-highlight")).to_have_count(0)
    open_tour.click()
    step(1)
    for position in range(2, len(steps) + 1):
        mark.get_by_role("button", name="Next").click()
        step(position)
    mark.get_by_role("button", name="Finish").click()
    expect(open_tour).to_be_focused()
    expect(mark).to_be_hidden()


class WorkspaceActivity(unittest.TestCase):
    def test_activity(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            db = root / "workspace.sqlite"
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            operator = secrets.token_urlsafe(32)
            app = create_app(root, db, operator, "station")
            for name, stop in app.extensions.items():
                if name.startswith("stop_"):
                    cleanup.callback(stop)
            service = make_server("127.0.0.1", 0, app)
            runner = Thread(target=service.serve_forever, daemon=True)
            runner.start()
            cleanup.callback(service.server_close)
            cleanup.callback(runner.join)
            cleanup.callback(service.shutdown)
            base = f"http://127.0.0.1:{service.server_port}"
            auth = TEAM | dict(Authorization="Bearer " + operator)
            operator_client = app.test_client()
            topic = "/api/team/support/requests"
            activity = "/api/team/activity"
            title = "Private gallery rehearsal"

            clip = root / "fixture.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=320x180:rate=24", "-t", "1", "-c:v", "libx264", str(clip)],
                           check=True, capture_output=True)
            with clip.open("rb") as video:
                upload = requests.post(base + "/api/library/uploads", headers=auth, timeout=25,
                    files={"file": (title + ".mp4", video, "video/mp4")}, data={"upload_id": uuid.uuid4().hex})
            upload.raise_for_status()
            work = upload.json()["id"]
            now = datetime.now(timezone.utc)
            event = operator_client.post("/api/live-events", headers=auth, json=dict(
                title="Private station production", slug="fixture-production", type="live", latency="standard",
                visibility="private", save_replay=False, dvr_seconds=120, schedule=dict(timezone="UTC", repeat="none",
                    start=(now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M"),
                    end=(now + timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M"))))
            self.assertEqual(201, event.status_code, event.data)
            operator_ticket = operator_client.post(topic, headers=auth, json=dict(
                subject="Sensitive station request", message="Staff-only description."))
            self.assertEqual(201, operator_ticket.status_code)
            self.assertEqual(403, operator_client.get(activity).status_code)
            operator_items = operator_client.get(activity, headers=auth)
            self.assertEqual(200, operator_items.status_code)
            private_item = next(item for item in operator_items.json["items"]
                                if item["title"] == "Sensitive station request")
            self.assertNotIn("Staff-only description.", operator_items.get_data(as_text=True))
            self.assertEqual(204, operator_client.put(activity, headers=auth, json={"id": private_item["id"]}).status_code)

            with sync_playwright() as playwright, chromium(playwright) as browser, \
                    browser.new_context() as viewer, browser.new_context() as other, \
                    browser.new_context() as fresh, browser.new_context() as admin:
                def join(context, username, role):
                    invitation = operator_client.post("/api/team/invitations", headers=auth,
                        json=dict(email=f"{username}@example.test", role=role))
                    self.assertEqual(201, invitation.status_code, invitation.data)
                    page = context.new_page()
                    page.goto(base + invitation.json["path"])
                    page.get_by_label("Display name").fill(username)
                    page.get_by_label("Choose a password").fill(PASSWORD)
                    page.get_by_role("button", name="Join team").click()
                    expect(page.locator("#teamIdentity")).to_contain_text(username)
                    return page

                page = join(viewer, "viewer", "viewer")
                other_page = join(other, "other", "viewer")
                manager = join(admin, "manager", "admin")
                manager_items = admin.request.get(base + activity, headers=TEAM).json()["items"]
                self.assertFalse(next(item for item in manager_items if item["id"] == private_item["id"])["read"])
                member = viewer.request.get(base + "/api/team/account").json()["id"]
                grant = f"/api/team/members/{member}/works/{work}"
                self.assertEqual(204, operator_client.put(grant, headers=auth, json=dict(permission="view")).status_code)
                search = page.get_by_role("search", name="Find videos")
                search.get_by_label("Search videos").fill("GALLERY REHEARSAL")
                search.get_by_role("button", name="Search", exact=True).click()
                expect(page.locator("#teamVideos").get_by_role("link", name=title)).to_be_visible()
                search.get_by_label("Search videos").fill("Nothing with this title")
                search.get_by_role("button", name="Search", exact=True).click()
                expect(page.get_by_text("No videos in your Team Library match your search.")).to_be_visible()
                search.get_by_role("button", name="Clear search").click()
                expect(page.locator("#teamVideos").get_by_role("link", name=title)).to_be_visible()

                other_search = other_page.get_by_role("search", name="Find videos")
                other_search.get_by_label("Search videos").fill(title)
                other_search.get_by_role("button", name="Search", exact=True).click()
                expect(other_page.get_by_text("No videos in your Team Library match your search.")).to_be_visible()
                other_search.get_by_label("Search in").select_option("watch")
                other_search.get_by_role("button", name="Search", exact=True).click()
                expect(other_page.get_by_label("Search public videos")).to_have_value(title)
                expect(other_page.get_by_role("link", name=title)).to_have_count(0)

                first = viewer.request.post(base + topic, headers=TEAM, data=dict(
                    subject="Caption support", message="A message for workspace support."))
                self.assertEqual(201, first.status)
                reply = operator_client.post(topic + "/" + first.json()["id"], headers=auth,
                                             json={"message": "We have the reviewed captions."})
                self.assertEqual(200, reply.status_code)
                older = viewer.request.post(base + topic, headers=TEAM, data=dict(
                    subject="Older than 30 days", message="A test of the age boundary."))
                self.assertEqual(201, older.status)
                with closing(sqlite3.connect(db)) as con, con:
                    con.execute("UPDATE help_request_event SET at=? WHERE request=?",
                                (time.time() - 31 * 86400, older.json()["id"]))

                bell = page.locator(".workspace-activity summary")
                panel = page.get_by_role("region", name="Workspace activity")
                bell.click()
                expect(panel.get_by_role("link", name=title)).to_be_visible()
                expect(panel.get_by_role("link", name="Caption support").first).to_be_visible()
                expect(panel.get_by_text("Sensitive station request")).to_have_count(0)
                expect(panel.get_by_text("Older than 30 days")).to_have_count(0)
                expect(panel.get_by_text("Private station production")).to_have_count(0)
                feed = viewer.request.get(base + activity, headers=TEAM)
                self.assertIn("no-store", feed.headers["cache-control"])
                self.assertEqual([], other.request.get(base + activity, headers=TEAM).json()["items"])
                shared = next(item for item in feed.json()["items"] if item["title"] == title)
                self.assertEqual(404, other.request.put(base + activity, headers=TEAM, data={"id": shared["id"]}).status)
                self.assertEqual(403, viewer.request.put(base + activity, data={"id": shared["id"]},
                    headers=TEAM | {"Origin": "https://foreign.example.test"}).status)
                panel.get_by_role("button", name=f"Mark as read: {title}", exact=False).click()
                expect(panel.get_by_text("Activity marked as read.")).to_be_visible()

                self.assertTrue(fresh.request.post(base + "/api/team/session", headers=TEAM,
                    data=dict(email="viewer@example.test", password=PASSWORD)).ok)
                persisted = fresh.request.get(base + activity, headers=TEAM).json()["items"]
                self.assertTrue(next(item for item in persisted if item["id"] == shared["id"])["read"])
                panel.get_by_role("link", name=title).click()
                player = page.locator("video")
                self.assertEqual(320, player.evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth;}"))
                self.assertEqual(204, operator_client.delete(grant, headers=auth, json={}).status_code)
                self.assertEqual(404, viewer.request.get(base + shared["url"] + "/media").status)
                self.assertEqual(404, viewer.request.put(base + activity, headers=TEAM, data={"id": shared["id"]}).status)
                after_revocation = viewer.request.get(base + "/api/team/library?q=gallery")
                self.assertEqual([], after_revocation.json()["works"])
                self.assertIn("no-store", after_revocation.headers["cache-control"])

                manager.locator(".workspace-activity summary").click()
                manager_panel = manager.get_by_role("region", name="Workspace activity")
                manager_panel.get_by_role("link", name="Private station production").click()
                expect(manager.get_by_label("Title", exact=True)).to_have_value("Private station production")
                page.goto(base + "/team")
                bell.click()
                panel.get_by_role("link", name="Caption support").first.click()
                expect(page.get_by_text("We have the reviewed captions.", exact=True)).to_be_visible()
                for index in range(32):
                    new = viewer.request.post(base + topic, headers=TEAM, data=dict(
                        subject=f"Support item {index:02}", message="Pagination receipt."))
                    self.assertEqual(201, new.status)
                page.goto(base + "/team")
                bell.click()
                expect(panel.get_by_role("link")).to_have_count(30)
                panel.get_by_role("button", name="Show older activity").click()
                expect(panel.get_by_role("link")).to_have_count(34)
                expect(panel.get_by_text(title)).to_have_count(0)
                expect(panel.get_by_text("Older than 30 days")).to_have_count(0)
                page.get_by_role("button", name="Sign out").click()
                expect(panel).to_be_hidden()
                self.assertEqual(401, viewer.request.get(base + activity, headers=TEAM).status)

                manager.goto(base + f"/operator/videos/{work}")
                check_tour(manager, [
                    ("#previewTitle", "Preview the video"),
                    ("#videoMetaTitle", "Describe the video"),
                    ("#videoReviewLinks", "Share for review"),
                ])
                production = event.json
                occurrence = production["occurrences"][0]["id"]
                manager.goto(base + f"/operator/live-events/{production['id']}/{occurrence}/studio")
                check_tour(manager, [
                    ("#recordSources", "Connect your sources"),
                    ("#studioScenes", "Prepare scenes and graphics"),
                    ("#recordProductionMode", "Choose a production mode"),
                ])
                self.assertEqual("broadcast", manager.locator("#browserProductionMode").input_value())

                pw = "Synthetic password for this review"
                review = operator_client.post(f"/api/library/work/{work}/review-links", headers=auth,
                                              json=dict(name="Gallery review", password=pw, allow_comment=True))
                self.assertEqual(201, review.status_code, review.data)
                with browser.new_context(viewport={"width": 390, "height": 780}) as visitor:
                    guest = visitor.new_page()
                    guest.goto(base + review.json["path"])
                    expect(guest.get_by_role("button", name="Take a tour")).to_have_count(0)
                    guest.get_by_label("Review password").fill(pw)
                    guest.get_by_role("button", name="Open review").click()
                    check_tour(guest, [
                        ("#reviewTourPlayer", "Play the selected version"),
                        ("#reviewPlayerControls", "Inspect a moment"),
                        ("#reviewTourFeedback", "Find feedback"),
                    ])
                    self.assertEqual(320, guest.locator("video").evaluate(
                        "async v => {v.muted=true; await v.play(); return v.videoWidth;}"))


if __name__ == "__main__":
    unittest.main()
