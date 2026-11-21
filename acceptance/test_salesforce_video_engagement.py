import sqlite3
import time
import unittest
from contextlib import contextmanager
from threading import Event
from unittest.mock import Mock, patch

from catalog import video_version_key
import library_analytics
import library_list_registration
import library_registration
import library_salesforce_video_engagement as reports


class TestSalesforceVideoEngagement(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)

        self.con.executescript(
            library_analytics.SCHEMA
            + library_list_registration.SCHEMA
            + library_registration.SCHEMA
            + reports.SCHEMA
            + "CREATE TABLE action("
            "  id TEXT PRIMARY KEY,"
            "  status TEXT,"
            "  decision TEXT"
            ");"
        )

        self.work = dict(
            id="video",
            files=[],
            duration_seconds=10,
            source_created_time="2020-01-01T12:00:00+00:00",
            library_added_at="2025-01-01T18:00:00+00:00",
        )
        self.version = video_version_key(self.work)

        self.con.execute(
            "INSERT INTO library_salesforce_form_plan("
            "  kind, subject, generation, provider_revision,"
            "  form_revision, list_id, list_name, company"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            ("video", "video", "gen", "grant", "form",
             "campaign", "Campaign", "Studio"),
        )
        self.plan = self.con.execute(
            "SELECT * FROM library_salesforce_form_plan"
            " WHERE kind=? AND subject=?",
            ("video", "video"),
        ).fetchone()

        self.con.execute(
            "INSERT INTO viewer_registrant("
            "  kind, subject, email_key, email, registered, answers"
            ") VALUES(?, ?, ?, ?, ?, ?)",
            ("video", "video", "viewer@example.test",
             "viewer@example.test", 1, "{}"),
        )

        self.con.execute(
            "INSERT INTO library_salesforce_form_delivery("
            "  id, kind, subject, generation,"
            "  recipient_key, state, registered"
            ") VALUES(?, ?, ?, ?, ?, ?, ?)",
            ("delivery", "video", "video", "gen", "hashed", "queued", 1),
        )
        self.con.commit()

        self.conn = Mock(name="OAuth")
        self.stop = Event()

    @contextmanager
    def tx(self):
        yield self.con
        self.con.commit()

    def validate(self, con, kind, uid):
        return dict(
            revision="form",
            salesforce_version=self.version,
            salesforce_duration=10,
        )

    @staticmethod
    def action(con, job_id, step, body):
        con.execute(
            "INSERT INTO action(id, status, decision) VALUES(?, 'waiting', NULL)",
            (job_id,),
        )

    def test_daily_queue(self):
        self.assertEqual(reports.first_report_at(self.work), 1577883600)
        reports.enable(self.con, "gen")
        args = (self.con, self.action, self.plan, self.work, "hashed", "viewer@example.test", int(time.time()))
        report = reports.consent(*args)
        self.assertIsNotNone(report)
        self.assertEqual(report, reports.consent(*args))
        due = self.con.execute("SELECT due FROM library_salesforce_video_report WHERE id=?", (report,)).fetchone()[0]
        self.assertIsNone(reports.next_job(self.con, due))
        self.con.execute("UPDATE action SET status='eligible' WHERE id=?", (report,))
        self.assertIsNone(reports.next_job(self.con, due))
        self.con.execute("UPDATE library_salesforce_form_delivery SET state='sent' WHERE id='delivery'")
        self.assertIsNone(reports.next_job(self.con, due - 1))
        self.verify_delivery(report, due)

    def playback(self, report):
        sessions = (("one",0,2000),("two",1500,4000),("unsigned",0,10000))
        for session, left, right in sessions:
            payload = dict(sequence=1, played=True, completed=False,
                           watched=right-left, intervals=[[left,right]])
            library_analytics.record(self.con,self.work,session,"watch",payload)
            if session in ("one","two"):
                reports.link_sample(self.con,report,"video",self.version,session)

    def verify_delivery(self, report, due):
        self.playback(report)

        def publish(*args, **kwargs):
            kwargs["before_write"]()
            return "sent"

        with patch.object(reports.salesforce_outbound, "publish_engagement", side_effect=publish) as send:
            self.assertTrue(reports.run_one(self.tx, self.conn, self.validate, self.stop, lambda: due))
            self.assertEqual(send.call_args.kwargs["watch_percent"], 40)
            self.assertEqual(send.call_args.kwargs["video_views"], 2)

        self.assertIsNone(reports.next_job(self.con, due))
        self.assertEqual(reports.next_job(self.con, due + reports.DAY)["id"], report)
        reports.revoke(self.con, report=report)
        self.assertIsNone(reports.next_job(self.con, due + reports.DAY))
