import re
import unittest
from urllib.parse import unquote

from test_marketo_connection import _resp
from werkzeug.exceptions import BadGateway
import salesforce_outbound as delivery

ORIGIN = "https://station.my.salesforce.com"
REVISION = "fixture-grant"
CAMPAIGN = "701000000000001"
LEAD = "00Q000000000001"
CONTACT = "003000000000001"
EMAIL = "viewer@example.test"
MEDIA_KEY = "a01000000000001"
REGISTRANT_KEY = "a02000000000001"


class Provider:
    origin = ORIGIN

    def __init__(self):
        self.writes = []
        self.rows = {"Contact": [], "Lead": []}
        self.objects = {}
        self.lost = set()

    def require_configured(self):
        pass

    def api(self, revision, method, url, *, before_send=None, **kwargs):
        assert revision == REVISION, revision
        prefix = f"{ORIGIN}/services/data/v64.0/"
        assert url.startswith(prefix), url
        path = url[len(prefix):]
        method = method.upper()

        if method == "GET":
            if path == "query":
                q = (kwargs.get("params") or {})["q"]
                assert f"Email = '{EMAIL}'" in q, q
                name = next((n for n in ("Contact", "Lead") if n in q), None)
                assert name is not None, q
                return _resp({"done": True, "records": self.rows[name]})

            if path == f"sobjects/Campaign/{CAMPAIGN}":
                return _resp({"Id": CAMPAIGN, "Name": "Fixture Campaign", "IsActive": True})

            match = re.fullmatch(r"sobjects/([^/]+)/External_Key__c/([^/]+)", path)
            if match:
                name, key = match.group(1), unquote(match.group(2))
                record = self.objects.get((name, key))
                if record is None:
                    resp = _resp({})
                    resp.status_code = 404
                    resp._content_consumed = True
                    return resp
                return _resp(record)

            raise AssertionError(f"unexpected GET {url}")

        if method == "PATCH":
            before_send()
            assert path == f"sobjects/{delivery.REGISTRANT}/{REGISTRANT_KEY}", path
            record = next(record for (name, _), record in self.objects.items() if name == delivery.REGISTRANT)
            record.update(kwargs["json"])
            self.writes.append("PATCH")
            if "PATCH" in self.lost:
                self.lost.remove("PATCH")
                raise BadGateway("lost response")
            resp = _resp({})
            resp.status_code = 204
            resp._content_consumed = True
            return resp

        if method == "POST":
            before_send()
            match = re.fullmatch(r"sobjects/([^/]+)", path)
            assert match, f"unexpected POST {url}"
            name = match.group(1)
            data = kwargs.get("json") or kwargs.get("data") or {}

            if name == "Lead":
                key = data[delivery.LEAD_KEY]
                assert (name, key) not in self.objects, f"repeated Lead POST {key}"
                record = {
                    "Id": LEAD,
                    "Email": data["Email"],
                    "HasOptedOutOfEmail": False,
                }
                self.objects[(name, key)] = record
                self.rows["Lead"].append(record)
                new_id = LEAD
            else:
                key = data["External_Key__c"]
                assert (name, key) not in self.objects, f"repeated {name} POST {key}"
                record = dict(data)
                if name == delivery.REGISTRANT:
                    record.setdefault("Lead__c", None)
                    record.setdefault("Contact__c", None)
                new_id = MEDIA_KEY if name == delivery.MEDIA else REGISTRANT_KEY
                record["Id"] = new_id
                self.objects[(name, key)] = record

            self.writes.append(name)
            if name in self.lost:
                self.lost.discard(name)
                raise BadGateway()

            resp = _resp({"id": new_id, "success": True})
            resp.status_code = 201
            resp._content_consumed = True
            return resp

        raise AssertionError(f"unexpected method {method}")


class TestSalesforceOutbound(unittest.TestCase):

    def publish(self, provider, before_write=lambda: None, kind="webinar"):
        return delivery.publish_registration(
            provider,
            REVISION,
            campaign=CAMPAIGN,
            kind=kind,
            title="Documentary",
            source_ref="station/event/123",
            registrant="viewer-1",
            registered_at="2026-11-21T14:00:00Z",
            address=EMAIL,
            answers={"first_name": "Ada", "last_name": "Viewer"},
            company="Individual",
            before_write=before_write,
        )

    def test_lost_replies_preserve_one_registration(self):
        provider = Provider()
        provider.lost = {"Lead", delivery.REGISTRANT}

        self.assertEqual(self.publish(provider), "sent")
        self.assertEqual(provider.writes, ["Lead", delivery.MEDIA, delivery.REGISTRANT])

        record = next(
            value
            for (name, _), value in provider.objects.items()
            if name == delivery.REGISTRANT
        )
        self.assertEqual(record["Lead__c"], LEAD)
        self.assertIsNone(record["Contact__c"])
        self.assertEqual(record["Media__c"], MEDIA_KEY)
        self.assertEqual(record["Status__c"], "Registered")

        record["Status__c"] = "Attended Live"

        self.assertEqual(self.publish(provider), "sent")
        self.assertEqual(provider.writes, ["Lead", delivery.MEDIA, delivery.REGISTRANT])
        self.assertEqual(record["Status__c"], "Attended Live")

    def test_contact_optout(self):
        provider = Provider()
        contact = {"Id": CONTACT, "Email": EMAIL.upper(), "HasOptedOutOfEmail": True}
        provider.rows["Contact"].append(contact)
        assert self.publish(provider) == "skipped"
        assert provider.writes == []
        contact["HasOptedOutOfEmail"] = False
        assert self.publish(provider) == "sent"
        assert provider.writes == [delivery.MEDIA, delivery.REGISTRANT]
        registrant = next(value for (name, key), value in provider.objects.items() if name == delivery.REGISTRANT)
        assert registrant["Contact__c"] == CONTACT
        assert registrant["Lead__c"] is None

    def report(self, provider, before_write=lambda: None, kind="webinar", **fields):
        return delivery.publish_engagement(
            provider,
            REVISION,
            campaign=CAMPAIGN,
            kind=kind,
            source_ref="station/event/123",
            registrant="viewer-1",
            address=EMAIL,
            before_write=before_write,
            **fields,
        )

    def test_engagement_recovers_lost_reply_and_preserves_progress(self):
        provider = Provider()
        self.assertEqual(self.publish(provider), "sent")
        provider.lost = {"PATCH"}

        self.assertEqual(
            self.report(
                provider,
                status="Attended Live",
                watch_percent=65,
            ),
            "sent",
        )
        record = next(
            value
            for (name, _), value in provider.objects.items()
            if name == delivery.REGISTRANT
        )
        self.assertEqual(record["Status__c"], "Attended Live")
        self.assertEqual(record["Watch_Percent__c"], 65)
        self.assertEqual(provider.writes.count("PATCH"), 1)

        self.report(
            provider,
            status="No Show",
            watch_percent=20,
        )
        self.assertEqual(record["Status__c"], "Attended Live")
        self.assertEqual(record["Watch_Percent__c"], 65)
        self.assertEqual(provider.writes.count("PATCH"), 1)

    def test_video_report(self):
        from werkzeug.exceptions import UnprocessableEntity

        provider = Provider()
        self.assertEqual(self.publish(provider, kind="video"), "sent")
        self.assertEqual(
            self.report(
                provider,
                kind="video",
                video_views=4,
                watch_percent=75,
            ),
            "sent",
        )
        record = next(
            value
            for (name, _), value in provider.objects.items()
            if name == delivery.REGISTRANT
        )
        self.assertEqual(record["Video_Views__c"], 4)
        self.assertEqual(record["Watch_Percent__c"], 75)

        with self.assertRaises(UnprocessableEntity):
            self.report(provider, kind="video", status="Attended Live")

    def test_report_respects_scope_and_refuses_missing_record(self):
        from werkzeug.exceptions import Forbidden, NotFound
        provider = Provider()
        self.publish(provider)
        provider.rows['Lead'][0]['HasOptedOutOfEmail'] = True
        self.assertEqual(self.report(provider, watch_percent=50), 'skipped')
        provider.rows['Lead'][0]['HasOptedOutOfEmail'] = False

        def cancel():
            raise Forbidden

        with self.assertRaises(Forbidden):
            self.report(provider, watch_percent=50, before_write=cancel)

        record_key = next(key for key in provider.objects if key[0] == delivery.REGISTRANT)
        provider.objects[record_key]['Media__c'] = CONTACT
        with self.assertRaises(BadGateway):
            self.report(provider, watch_percent=50)
        del provider.objects[record_key]
        with self.assertRaises(NotFound):
            self.report(provider, watch_percent=50)
        self.assertNotIn('PATCH', provider.writes)

    def test_engagement_preflight_requires_fields(self):
        from unittest.mock import patch
        from werkzeug.exceptions import Conflict

        metadata = {
            'updateable': True,
            'fields': [
                {'name': 'Status__c', 'updateable': True},
                {'name': 'Watch_Percent__c', 'updateable': True},
            ],
        }
        with patch.object(delivery.salesforce_crm, '_get', return_value=metadata):
            delivery.validate_engagement_installation(Provider(), REVISION, kind='webinar')
            with self.assertRaises(Conflict) as missing:
                delivery.validate_engagement_installation(Provider(), REVISION, kind='video')
            self.assertIn('Video_Views__c', str(missing.exception))

            metadata['updateable'] = False
            with self.assertRaises(Conflict):
                delivery.validate_engagement_installation(Provider(), REVISION, kind='webinar')

    def test_ambiguous_email(self):
        provider = Provider()
        provider.rows["Contact"].append({
            "Id": CONTACT,
            "Email": EMAIL,
            "HasOptedOutOfEmail": False,
        })
        provider.rows["Lead"].append({
            "Id": LEAD,
            "Email": EMAIL,
            "HasOptedOutOfEmail": False,
        })

        from werkzeug.exceptions import UnprocessableEntity

        with self.assertRaises(UnprocessableEntity):
            self.publish(provider)

        self.assertEqual(provider.writes, [])

    def test_canceled_before_provider_write(self):
        provider = Provider()

        from werkzeug.exceptions import Forbidden

        def canceled():
            raise Forbidden()

        with self.assertRaises(Forbidden):
            self.publish(provider, before_write=canceled)

        self.assertEqual(provider.writes, [])
