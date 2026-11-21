import unittest
from unittest.mock import MagicMock
import hubspot_crm as hs


class TestHubSpotEngagement(unittest.TestCase):
    def test_send_engagement(self):
        conn = MagicMock()
        resp = MagicMock()
        resp.status_code = 204
        conn.api.return_value.__enter__.return_value = resp
        before = MagicMock()
        iso = "2026-09-15T12:00:00Z"
        uid = "11111111-2222-3333-4444-555555555555"

        hs.send_engagement(
            conn,
            "2026-09",
            "pe123_cueporch",
            "42",
            iso,
            uid,
            {"title": "Test webinar"},
            before_write=before,
        )

        args, kwargs = conn.api.call_args
        self.assertEqual(
            args,
            ("2026-09", "POST", hs.ORIGIN + "/events/2026-09/send"),
        )
        self.assertEqual(
            kwargs["json"],
            {
                "eventName": "pe123_cueporch",
                "objectId": "42",
                "occurredAt": iso,
                "uuid": uid,
                "properties": {"title": "Test webinar"},
            },
        )
        before.assert_called_once()
        resp.json.assert_not_called()
