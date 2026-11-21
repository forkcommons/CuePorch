import json
import sqlite3
import time
import unittest
from contextlib import contextmanager

from flask import Flask, request

import event_analytics
import event_engagement
import event_marketo_engagement


class MarketoActivityTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(
            event_engagement.SCHEMA
            + event_analytics.SCHEMA
            + "CREATE TABLE live_attendee_session(session TEXT PRIMARY KEY, sharing TEXT NOT NULL);"
        )
        self.con.execute("INSERT INTO live_qa_settings VALUES('e',10,1,1,0,NULL)")
        self.scopes = []

        @contextmanager
        def tx():
            try:
                yield self.con
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise

        def session(scope, current):
            token = current or "session-token"
            return token, "viewer:" + token

        def share(con, scope):
            self.scopes.append(scope)
            return "grant" if request.headers.get("X-Test-Consent") == "1" else None

        self.app = Flask(__name__)
        event_engagement.register(
            self.app,
            tx,
            lambda: None,
            session,
            lambda con, uid, opens: None,
            lambda con, slug, opens: (["e", "revision", 10, "form"], True),
            share_activity=share,
        )
        self.client = self.app.test_client()
        self.headers = {"X-CuePorch-Engagement-Session": "s1"}
        self.consent = dict(self.headers, **{"X-Test-Consent": "1"})

    def tearDown(self):
        self.con.close()

    def test_engagement_queue_waits_for_completion_approval_and_registration(self):
        from event_contact_delivery import SCHEMA as CONTACT_SCHEMA

        self.con.executescript(
            CONTACT_SCHEMA
            + event_marketo_engagement.SCHEMA
            + "CREATE TABLE action(id TEXT PRIMARY KEY, status TEXT, decision TEXT);"
        )
        plan = {"event": "e", "opens": 10, "generation": "generation"}

        def action(con, uid, step, body):
            self.assertEqual(step, "event_engagement_sync")
            self.assertEqual(body["occurrence"], 10)
            con.execute("INSERT INTO action VALUES (?, 'waiting', NULL)", (uid,))

        enqueue = event_marketo_engagement.enqueue
        next_job = event_marketo_engagement.next_job
        self.assertTrue(enqueue(self.con, action, plan, "registrant", "grant"))
        self.assertFalse(enqueue(self.con, action, plan, "registrant", "other"))
        self.assertIsNone(next_job(self.con, 20))
        self.assertTrue(
            event_marketo_engagement.complete(self.con, "generation", 20, 8)
        )
        self.assertIsNone(next_job(self.con, 20))
        self.con.execute("UPDATE action SET status='eligible'")
        self.con.execute(
            """INSERT INTO live_event_contact_delivery
               (id, provider, event, opens, generation, registrant, state)
               VALUES ('registration', 'marketo', 'e', 10, 'generation', 'registrant', 'queued')"""
        )
        self.assertIsNone(next_job(self.con, 20))
        self.con.execute("UPDATE live_event_contact_delivery SET state='sent'")
        job = next_job(self.con, 20)
        self.assertEqual(
            (job["sharing"], job["completed"], job["duration"]), ("grant", 20, 8)
        )
        self.con.execute(
            "INSERT INTO live_event_shared_activity VALUES ('qa', 'q', 'u', 'grant')"
        )
        event_marketo_engagement.cancel(self.con, sharing="grant")
        self.assertIsNone(next_job(self.con, 20))
        receipt = self.con.execute("SELECT status, decision FROM action").fetchone()
        self.assertEqual(tuple(receipt), ("rejected", "canceled"))
        self.assertIsNone(
            self.con.execute("SELECT 1 FROM live_event_shared_activity").fetchone()
        )

    def test_completion_observed_without_webhook(self):
        from contextlib import contextmanager
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock

        import event_contact_delivery
        import event_schedule
        import event_webhooks
        import library_events
        import event_marketo_engagement

        self.con.executescript(
            event_contact_delivery.SCHEMA
            + event_webhooks.SCHEMA
            + library_events.SCHEMA
            + "CREATE TABLE action(id TEXT PRIMARY KEY, status TEXT, decision TEXT);"
        )

        schedule = event_schedule.validate(
            dict(
                timezone="UTC",
                start="2021-01-01T12:00",
                end="2021-01-01T12:10",
            )
        )
        opens = 1609502400

        self.con.execute(
            "INSERT INTO live_event VALUES (?, ?, ?, ?, ?, ?)",
            (
                "e",
                "event",
                "r",
                json.dumps(dict(schedule=schedule)),
                None,
                "2021",
            ),
        )
        self.con.execute(
            "INSERT INTO live_occurrence VALUES (?, ?, ?, ?, ?)",
            ("e", opens, "https://peer.example", "remote", "ready"),
        )

        plan = event_contact_delivery.set_destination(
            self.con,
            "marketo",
            "e",
            opens,
            provider_revision="p",
            event_revision="r",
            form_revision="f",
            deadline=opens + 600,
            destination={"program": 1, "mapping": {}, "engagement": True},
        )
        event_marketo_engagement.enqueue(self.con, lambda *_: None, plan, "r", "s")

        @contextmanager
        def tx():
            yield self.con

        host = Mock()
        host.call().json.return_value = dict(
            isLive=False,
            privacy={"id": 3},
            state={"id": 1},
            duration=57,
        )

        with TemporaryDirectory() as root:
            library_events.observe_delivery(
                tx,
                Path(root) / "workspace.sqlite",
                "https://peer.example",
                lambda _: host,
            )

        row = self.con.execute(
            "SELECT duration FROM live_event_marketo_completion WHERE generation = ?",
            (plan["generation"],),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 57)

        count = self.con.execute("SELECT COUNT(*) FROM live_webhook_feed").fetchone()[0]
        self.assertEqual(count, 0)

        delivery = self.con.execute(
            "SELECT delivery FROM live_occurrence WHERE event = ? AND opens = ?",
            ("e", opens),
        ).fetchone()[0]
        self.assertEqual(delivery, "ready")

    def test_shared_activity_payload_and_watch_boundary(self):
        c = self.client
        url = "/events/e/10/engagement"
        r = c.post(
            url,
            json={
                "feature": "qa",
                "type": "question",
                "name": "Ada",
                "question": "Named question",
            },
            headers=self.consent,
        )
        self.assertEqual(r.status_code, 200)
        named = self.con.execute(
            "SELECT id FROM live_question WHERE question='Named question'"
        ).fetchone()["id"]
        self.assertEqual(
            self.con.execute(
                "SELECT count(*) FROM live_event_shared_activity WHERE feature='qa' AND activity=? AND sharing='grant'",
                (named,),
            ).fetchone()[0],
            1,
        )
        self.con.execute(
            "UPDATE live_question SET sent = sent - 2 WHERE id=?", (named,)
        )

        r = c.post(
            url,
            json={
                "feature": "qa",
                "type": "question",
                "anonymous": True,
                "question": "Secret question",
            },
            headers=self.consent,
        )
        self.assertEqual(r.status_code, 200)
        secret = self.con.execute(
            "SELECT id FROM live_question WHERE question='Secret question'"
        ).fetchone()["id"]
        self.assertIsNone(
            self.con.execute(
                "SELECT name FROM live_question WHERE id=?", (secret,)
            ).fetchone()["name"]
        )
        self.assertEqual(
            self.con.execute(
                "SELECT count(*) FROM live_event_shared_activity WHERE activity=?",
                (secret,),
            ).fetchone()[0],
            0,
        )
        self.con.execute(
            "UPDATE live_question SET sent = sent - 2 WHERE id=?", (secret,)
        )

        r = c.post(
            url,
            json={
                "feature": "qa",
                "type": "question",
                "name": "Ada",
                "question": "Archived question",
            },
            headers=self.consent,
        )
        self.assertEqual(r.status_code, 200)
        archived = self.con.execute(
            "SELECT id FROM live_question WHERE question='Archived question'"
        ).fetchone()["id"]
        self.con.execute("UPDATE live_question SET archived=1 WHERE id=?", (archived,))

        now = time.time()
        self.con.execute(
            "INSERT INTO live_poll VALUES(?,?,?,?,?,?,'open',?)",
            ("poll-no", "e", 10, "First poll", json.dumps(["A", "B"]), 0, now - 2),
        )
        body = {"feature": "poll", "id": "poll-no", "choice": 1}
        self.assertEqual(c.post(url, json=body, headers=self.headers).status_code, 200)
        self.assertEqual(c.post(url, json=body, headers=self.consent).status_code, 200)
        self.assertEqual(
            self.con.execute(
                "SELECT count(*) FROM live_event_shared_activity WHERE feature='poll' AND activity='poll-no'"
            ).fetchone()[0],
            0,
        )

        self.con.execute("UPDATE live_poll SET state='results' WHERE id='poll-no'")
        self.con.execute(
            "INSERT INTO live_poll VALUES(?,?,?,?,?,?,'open',?)",
            ("poll-yes", "e", 10, "Second poll", json.dumps(["Yes", "No"]), 0, now - 1),
        )
        self.assertEqual(
            c.post(
                url,
                json={"feature": "poll", "id": "poll-yes", "choice": 0},
                headers=self.consent,
            ).status_code,
            200,
        )
        self.assertEqual(
            self.con.execute(
                "SELECT count(*) FROM live_event_shared_activity WHERE feature='poll' AND activity='poll-yes' AND sharing='grant'"
            ).fetchone()[0],
            1,
        )

        completed = now + 10
        self.con.execute("INSERT INTO live_attendee_session VALUES('live1','grant')")
        self.con.execute(
            "INSERT INTO live_playback VALUES('live1','e',10,'live',1,1,0,2000,?)",
            (completed - 1,),
        )
        self.con.execute("INSERT INTO live_attendee_session VALUES('other','other')")
        self.con.execute(
            "INSERT INTO live_playback VALUES('other','e',10,'live',1,1,0,5000,?)",
            (completed - 1,),
        )
        self.con.execute("INSERT INTO live_attendee_session VALUES('wrong','grant')")
        self.con.execute(
            "INSERT INTO live_playback VALUES('wrong','e',11,'live',1,1,0,7000,?)",
            (completed - 1,),
        )
        self.con.execute("INSERT INTO live_attendee_session VALUES('replay','grant')")
        self.con.execute(
            "INSERT INTO live_playback VALUES('replay','e',10,'replay',1,1,0,7000,?)",
            (completed - 1,),
        )

        fields = {
            "cuePorchQuestions": {"length": 500},
            "cuePorchPolls": {"length": 500},
        }
        props, attended = event_marketo_engagement.payload(
            self.con,
            {"event": "e", "opens": 10, "sharing": "grant"},
            fields,
            8,
            completed,
        )
        self.assertTrue(attended)
        self.assertEqual(props["cuePorchWatchPercentage"], 25)
        self.assertIn("Named question", props["cuePorchQuestions"])
        self.assertNotIn("Secret question", props["cuePorchQuestions"])
        self.assertNotIn("Archived question", props["cuePorchQuestions"])
        self.assertIn("Second poll: Yes", props["cuePorchPolls"])
        self.assertNotIn("First poll", props["cuePorchPolls"])
        self.assertTrue(
            self.scopes
            and all(scope == ["e", "revision", 10, "form"] for scope in self.scopes)
        )
        self.assertEqual(event_marketo_engagement._truncate("ab😀c", 3), "ab")
        self.assertEqual(event_marketo_engagement._truncate("😀x", 1), "")
        self.assertEqual(event_marketo_engagement._truncate("😀x", 2), "😀")
