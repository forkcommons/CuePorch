import hashlib
import json
import sqlite3
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import requests

import event_analytics
import event_contact_delivery
import event_engagement
import event_hubspot_sync
import event_mailchimp_engagement as mailchimp_engagement
import event_registrants


def response(status, body, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


class MailchimpEngagementTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = Path(tmp.name) / "webinar.sqlite"
        self.in_tx = False
        self.now = 100
        self.stop = threading.Event()
        with self.tx() as con:
            con.executescript(
                mailchimp_engagement.SCHEMA
                + event_contact_delivery.SCHEMA
                + event_registrants.SCHEMA
                + event_hubspot_sync.SCHEMA
                + event_engagement.SCHEMA
                + event_analytics.SCHEMA
                + "CREATE TABLE action(id TEXT PRIMARY KEY,status TEXT,decision TEXT);"
            )
            plan = event_contact_delivery.set_destination(
                con,
                "mailchimp",
                "event",
                10,
                provider_revision="rev",
                event_revision="e",
                form_revision="f",
                deadline=70,
                destination={"audience": "audience", "engagement": True},
            )
            con.executescript("""
                INSERT INTO live_event_registrant
                (id,event,opens,email_key,email,registered,source,answers)
                VALUES ('registrant','event',10,'viewer@example.org','viewer@example.org',12,'form','{}');
                INSERT INTO live_attendee_sharing VALUES ('sharing','registrant',0);
                INSERT INTO live_attendee_session VALUES ('live','sharing'),('replay','sharing');
                INSERT INTO live_playback VALUES
                ('live','event',10,'live',1,1,1,10000,40),
                ('replay','event',10,'replay',1,1,1,6000,49);
            """)
            con.execute(
                """INSERT INTO live_event_contact_delivery
                           (id,provider,event,opens,generation,registrant,state)
                           VALUES ('registration','mailchimp','event',10,?,'registrant','sent')""",
                (plan["generation"],),
            )
            self.assertTrue(
                mailchimp_engagement.enqueue(
                    con,
                    self.record_action,
                    plan,
                    "registrant",
                    "sharing",
                )
            )
            self.assertIsNone(mailchimp_engagement.next_job(con, self.now))
            self.assertTrue(
                mailchimp_engagement.complete(con, plan["generation"], 50, 40)
            )

    @contextmanager
    def tx(self):
        self.assertFalse(self.in_tx)
        con = sqlite3.connect(self.database)
        con.row_factory = sqlite3.Row
        self.in_tx = True
        try:
            with con:
                yield con
        finally:
            self.in_tx = False
            con.close()

    def record_action(self, con, uid, step, body):
        self.assertEqual(step, "event_engagement_sync")
        self.assertEqual(body["occurrence"], 10)
        con.execute("INSERT INTO action VALUES (?, 'eligible', 'automatic')", (uid,))

    def job(self):
        with self.tx() as con:
            return con.execute(
                "SELECT * FROM live_event_mailchimp_engagement"
            ).fetchone()

    def run_job(self, conn):
        return mailchimp_engagement.run_one(
            self.tx,
            conn,
            lambda *_: "Webinar",
            self.stop,
            lambda: self.now,
        )

    def test_uncertain_write_then_paginated_readback(self):
        queue_id = self.job()["id"]
        digest = hashlib.md5(b"viewer@example.org", usedforsecurity=False).hexdigest()
        member = f"https://api.mailchimp.com/3.0/lists/audience/members/{digest}"
        events = member + "/events"
        responses = iter(
            [
                response(
                    200,
                    {
                        "id": digest,
                        "list_id": "audience",
                        "email_address": "viewer@example.org",
                        "status": "subscribed",
                    },
                ),
                response(502, {}),
                response(429, {}, {"Retry-After": "37"}),
                response(200, {"total_items": 2, "events": [{"name": "unrelated"}]}),
                response(
                    200,
                    {
                        "total_items": 2,
                        "events": [
                            {
                                "name": "cueporch_webinar_engagement",
                                "properties": {"cueporch_delivery_id": queue_id},
                            }
                        ],
                    },
                ),
            ]
        )

        def api(revision, method, url, **kwargs):
            self.assertFalse(self.in_tx)
            self.assertEqual(revision, "rev")
            if method == "POST":
                self.assertEqual(self.job()["write_started"], 1)
            return next(responses)

        conn = Mock()
        conn.api.side_effect = api
        self.assertTrue(self.run_job(conn))
        self.assertEqual(
            (self.job()["state"], self.job()["write_started"], self.job()["due"]),
            ("queued", 1, 120),
        )
        self.assertFalse(self.run_job(conn))
        self.now = 120
        self.assertTrue(self.run_job(conn))
        self.assertEqual((self.job()["write_started"], self.job()["due"]), (1, 157))
        self.now = 157
        self.assertTrue(self.run_job(conn))
        self.assertEqual(
            (self.job()["state"], self.job()["attempts"], self.job()["snapshot"]),
            ("sent", 3, None),
        )

        calls = conn.api.call_args_list
        self.assertEqual(
            [call.args[1:] for call in calls],
            [
                ("GET", member),
                ("POST", events),
                ("GET", events),
                ("GET", events),
                ("GET", events),
            ],
        )
        self.assertEqual(
            [call.kwargs["params"] for call in calls[2:]],
            [
                {"count": 100, "offset": 0},
                {"count": 100, "offset": 0},
                {"count": 100, "offset": 1},
            ],
        )
        payload = calls[1].kwargs["json"]
        self.assertEqual(payload["name"], "cueporch_webinar_engagement")
        self.assertEqual(payload["occurred_at"], "1970-01-01T00:00:50+00:00")
        properties = payload["properties"]
        self.assertEqual(properties["cueporch_delivery_id"], queue_id)
        self.assertEqual(properties["live_watch_ms"], "10000")
        self.assertEqual(properties["replay_watch_ms"], "6000")
        self.assertEqual(properties["live_watch_percent"], "25")

    def test_revoked_sharing_cancels_before_provider_access(self):
        with self.tx() as con:
            con.execute("UPDATE live_attendee_sharing SET revoked=1 WHERE id='sharing'")
        conn = Mock()
        self.assertTrue(self.run_job(conn))
        conn.api.assert_not_called()
        self.assertEqual(self.job()["state"], "canceled")
