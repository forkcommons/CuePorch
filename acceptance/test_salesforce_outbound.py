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

    def publish(self, provider, before_write=lambda: None):
        return delivery.publish_registration(
            provider,
            REVISION,
            campaign=CAMPAIGN,
            kind="webinar",
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
