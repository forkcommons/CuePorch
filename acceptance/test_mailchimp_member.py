import hashlib
import json
import unittest

import mailchimp_crm

EMAIL = "viewer@example.org"
AUDIENCE = "audience"
REVISION = "rev"
MERGE_FIELDS = {"NAME": "A"}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._content = json.dumps(body).encode("utf-8")
        self._content_consumed = True

    @property
    def content(self):
        return self._content

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return json.loads(self._content.decode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def api(self, revision, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        return self.responses.pop(0)


def member_body(status):
    return {
        "id": hashlib.md5(EMAIL.lower().encode("utf-8")).hexdigest(),
        "list_id": AUDIENCE,
        "email_address": EMAIL,
        "status": status,
    }


class SaveMemberTest(unittest.TestCase):
    def test_save_member_statuses(self):
        cases = [
            (None, ["GET", "GET", "PUT"], ("delivered", "pending")),
            ("unsubscribed", ["GET", "GET"], ("suppressed", "unsubscribed")),
            ("subscribed", ["GET", "GET", "PATCH"], ("delivered", "subscribed")),
        ]

        for initial_status, expected_methods, expected_result in cases:
            with self.subTest(initial_status=initial_status):
                responses = [
                    FakeResponse(
                        200,
                        {
                            "id": AUDIENCE,
                            "name": "Audience",
                            "double_optin": True,
                            "marketing_permissions": False,
                        },
                    )
                ]

                if initial_status is None:
                    responses.append(
                        FakeResponse(
                            404, {"title": "Resource Not Found", "status": 404}
                        )
                    )
                    responses.append(FakeResponse(200, member_body("pending")))
                else:
                    responses.append(FakeResponse(200, member_body(initial_status)))
                    if initial_status == "subscribed":
                        responses.append(FakeResponse(200, member_body("subscribed")))

                conn = FakeConnection(responses)
                result = mailchimp_crm.save_member(
                    conn,
                    REVISION,
                    AUDIENCE,
                    EMAIL,
                    MERGE_FIELDS,
                    before_write=lambda: None,
                )

                self.assertEqual([call[0] for call in conn.calls], expected_methods)
                self.assertEqual(
                    result, dict(zip(("result", "status"), expected_result))
                )

                if initial_status is None:
                    self.assertEqual(
                        conn.calls[2][2],
                        {
                            "email_address": EMAIL,
                            "status_if_new": "pending",
                            "merge_fields": MERGE_FIELDS,
                        },
                    )
                elif initial_status == "unsubscribed":
                    self.assertFalse(
                        [call for call in conn.calls if call[0] in ("PUT", "PATCH")]
                    )
                else:
                    self.assertEqual(
                        conn.calls[2][2],
                        {"merge_fields": MERGE_FIELDS},
                    )


if __name__ == "__main__":
    unittest.main()
