import os
import re
import threading
import time
import urllib.parse

from werkzeug.exceptions import BadGateway, Forbidden, TooManyRequests

import library_analytics
from workspace_oauth import check_provider, provider_request, response_object

_MARGIN = 30.0


def _origin(value):
    if not value or any(c.isspace() or not c.isprintable() or c == "\\" for c in value):
        return None
    try:
        origin = library_analytics.source_origin(value)
        parts = urllib.parse.urlsplit(value)
        if (
            origin == "Unknown"
            or origin != value
            or parts.scheme not in ("https", "http")
        ):
            return None
        if parts.port == 0 or (
            parts.scheme == "http"
            and parts.hostname not in ("localhost", "127.0.0.1", "::1")
        ):
            return None
        return origin
    except ValueError:
        return None


def configured_connection():
    origin = os.environ.get("CUEPORCH_MARKETO_ORIGIN")
    client_id = os.environ.get("CUEPORCH_MARKETO_CLIENT_ID")
    client_secret = os.environ.get("CUEPORCH_MARKETO_CLIENT_SECRET")
    if not (origin or client_id or client_secret):
        return None
    if not (origin and client_id and client_secret):
        return None
    validated = _origin(origin)
    return MarketoConnection(validated, client_id, client_secret) if validated else None


class MarketoConnection:
    def __init__(self, origin, client_id, client_secret):
        self.origin = origin
        self.client_id = client_id
        self._secret = client_secret
        self._lock = threading.Lock()
        self._token = None
        self._expiry = 0.0

    def __repr__(self):
        return "MarketoConnection(origin=%r)" % (self.origin,)

    def revision(self, signing_key):
        import hmac
        import json

        payload = json.dumps(
            [
                "CuePorch Marketo configuration",
                self.origin,
                self.client_id,
                self._secret,
            ],
            separators=(",", ":"),
        )
        return hmac.new(signing_key.encode(), payload.encode(), "sha256").hexdigest()

    def _get_token(self):
        with self._lock:
            if self._token is not None and time.monotonic() < self._expiry:
                return self._token
            response = provider_request(
                "POST",
                self.origin + "/identity/oauth/token",
                params={
                    "client_id": self.client_id,
                    "client_secret": self._secret,
                    "grant_type": "client_credentials",
                },
            )
            check_provider(response)
            data = response_object(response)
            token = data.get("access_token") if isinstance(data, dict) else None
            expires_in = data.get("expires_in") if isinstance(data, dict) else None
            token_type = data.get("token_type") if isinstance(data, dict) else None
            if (
                not isinstance(token, str)
                or not token
                or not token.isascii()
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token)
                or not isinstance(expires_in, int)
                or isinstance(expires_in, bool)
                or expires_in <= 0
                or token_type != "bearer"
            ):
                raise BadGateway()
            self._token = token
            self._expiry = time.monotonic() + expires_in - _MARGIN
            return token

    def _invalidate(self, token):
        with self._lock:
            if self._token == token:
                self._token = None
                self._expiry = 0.0

    def _call(self, method, url, token, before_write, kwargs):
        if method == "POST" and before_write is not None:
            before_write()
        return provider_request(
            method, url, headers={"Authorization": "Bearer " + token}, **kwargs
        )

    @staticmethod
    def _error_codes(errors):
        if not isinstance(errors, list) or not errors:
            raise BadGateway()
        codes = set()
        for entry in errors:
            if not isinstance(entry, dict) or "code" not in entry:
                raise BadGateway()
            code = str(entry["code"])
            if not re.fullmatch("[0-9]{3}", code):
                raise BadGateway()
            codes.add(code)
        return codes

    @staticmethod
    def _fail(codes, response):
        if codes & {"606", "615"}:
            raw = (
                response.headers.get("Retry-After")
                if response and response.headers
                else None
            )
            retry = (
                int(raw)
                if isinstance(raw, str) and re.fullmatch("[0-9]{1,8}", raw)
                else 15
            )
            raise TooManyRequests(retry_after=retry)
        if "603" in codes:
            raise Forbidden()
        raise BadGateway()

    def api(self, method, path, *, before_write=None, **kwargs):
        verb = method.upper()
        if (
            verb not in ("GET", "POST")
            or not isinstance(path, str)
            or not path.startswith("/rest/")
        ):
            raise ValueError("invalid path")
        url = self.origin + path
        token = self._get_token()
        for attempt in range(2):
            response = self._call(verb, url, token, before_write, kwargs)
            if response.status_code == 401 and attempt == 0:
                self._invalidate(token)
                token = self._get_token()
                continue
            check_provider(response)
            data = response_object(response)
            if data.get("success") is True:
                result = data.get("result")
                if isinstance(result, list):
                    return data
                raise BadGateway()
            codes = (
                self._error_codes(data.get("errors") or [])
                if data.get("success") is False
                else []
            )
            if attempt == 0 and codes and codes <= {"601", "602"}:
                self._invalidate(token)
                token = self._get_token()
                continue
            self._fail(codes, response)


def _event_program(row):
    if not isinstance(row, dict):
        return None
    row_id = row.get("id")
    row_name = row.get("name")
    row_channel = row.get("channel")
    row_type = row.get("type")
    if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
        return None
    if not isinstance(row_name, str) or not row_name.strip():
        return None
    if not isinstance(row_channel, str) or not row_channel.strip():
        return None
    if row_type not in ("Event", "EventWithWebinar"):
        return None
    return {"id": row_id, "name": row_name, "channel": row_channel, "type": row_type}


def programs(connection, offset=0):
    if type(offset) is not int or offset < 0:
        raise ValueError("invalid offset")
    result = connection.api(
        "GET",
        "/rest/asset/v1/programs.json",
        params={"maxReturn": 50, "offset": offset},
    )["result"]
    if not isinstance(result, list):
        raise ValueError("invalid result")
    valid = [p for p in map(_event_program, result) if p is not None]
    return {
        "programs": valid,
        "next_offset": offset + 50 if len(result) == 50 else None,
    }


def program(connection, program_id):
    if (
        not isinstance(program_id, int)
        or isinstance(program_id, bool)
        or program_id <= 0
    ):
        raise ValueError()
    resp = connection.api("GET", f"/rest/asset/v1/program/{program_id}.json")
    rows = resp.get("result") if isinstance(resp, dict) else None
    if not isinstance(rows, list):
        raise BadGateway()
    matches = [
        r
        for r in rows
        if isinstance(r, dict) and _event_program(r) and r.get("id") == program_id
    ]
    if len(matches) != 1:
        raise BadGateway()
    chosen = matches[0]
    name = chosen.get("channel")
    if not isinstance(name, str):
        raise BadGateway()
    resp = connection.api(
        "GET", "/rest/asset/v1/channel/byName.json", params={"name": name}
    )
    channels = resp.get("result") if isinstance(resp, dict) else None
    if not isinstance(channels, list):
        raise BadGateway()
    ch_matches = [c for c in channels if isinstance(c, dict) and c.get("name") == name]
    if len(ch_matches) != 1:
        raise BadGateway()
    statuses = ch_matches[0].get("progressionStatuses")
    if not isinstance(statuses, list) or not any(
        isinstance(s, dict)
        and s.get("name") == "Registered"
        and s.get("hidden") is False
        for s in statuses
    ):
        raise BadGateway()
    return _event_program(chosen)


def lead_fields(connection):
    allowed = {
        "string",
        "text",
        "boolean",
        "integer",
        "float",
        "currency",
        "date",
        "datetime",
        "email",
        "phone",
        "url",
    }
    blocked = {
        "email",
        "firstName",
        "lastName",
        "unsubscribed",
        "marketingSuspended",
        "emailInvalid",
        "blackListed",
        "emailSuspended",
        "emailSuspendedAt",
        "emailSuspendedCause",
        "id",
        "marketoGUID",
        "createdAt",
        "updatedAt",
        "leadId",
        "leadID",
        "externalId",
    }
    result = connection.api("GET", "/rest/v1/leads/describe.json")["result"]
    fields = {}
    for item in result or []:
        if not isinstance(item, dict):
            continue
        rest = item.get("rest") or {}
        if not isinstance(rest, dict) or rest.get("readOnly") is not False:
            continue
        name = rest.get("name")
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,127}", name
        ):
            continue
        if name in blocked:
            continue
        dtype = item.get("dataType")
        if not isinstance(dtype, str):
            continue
        dtype = dtype.lower()
        if dtype not in allowed:
            continue
        label = item.get("displayName") or name
        if not isinstance(label, str) or not label.strip():
            label = name
        length = item.get("length")
        if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
            length = None
        fields.setdefault(
            name, {"name": name, "label": label, "type": dtype, "length": length}
        )
    return sorted(fields.values(), key=lambda f: f["label"])


def lookup_lead(connection, email):
    if not isinstance(email, str) or not email or not email.isascii() or "," in email:
        raise ValueError("invalid email")
    flags = (
        "unsubscribed",
        "marketingSuspended",
        "emailInvalid",
        "blackListed",
        "emailSuspended",
    )
    params = {
        "filterType": "email",
        "filterValues": email,
        "fields": ",".join(("id", "email", *flags)),
    }
    payload = connection.api("GET", "/rest/v1/leads.json", params=params)
    if payload.get("moreResult") is True or payload.get("nextPageToken"):
        raise BadGateway("incomplete lead result")
    results = payload.get("result")
    if not isinstance(results, list):
        raise BadGateway("ambiguous lead result")
    if not results:
        return None
    if len(results) > 1:
        raise BadGateway("ambiguous lead result")
    lead = results[0]
    lead_id = lead.get("id") if isinstance(lead, dict) else None
    if isinstance(lead_id, bool) or not isinstance(lead_id, int) or lead_id <= 0:
        raise BadGateway("invalid lead id")
    if not isinstance(lead, dict):
        raise BadGateway("email mismatch")
    lead_email = lead.get("email")
    if not isinstance(lead_email, str) or lead_email.casefold() != email.casefold():
        raise BadGateway("email mismatch")
    values = [lead.get(name) for name in flags]
    if any(value is not None and not isinstance(value, bool) for value in values):
        raise BadGateway("invalid boolean field")
    return {"id": lead_id, "opted_out": any(value is True for value in values)}


def save_lead(connection, email, properties, existing_id, *, before_write):
    creating = existing_id is None
    row = dict(properties)
    row.pop("email", None)
    row.pop("id", None)
    if creating:
        row["email"] = email
        action, lookup, expected = "createOnly", "email", "created"
    else:
        row["id"] = existing_id
        action, lookup, expected = "updateOnly", "id", "updated"
    payload = {"action": action, "lookupField": lookup, "input": [row]}
    data = connection.api(
        "POST", "/rest/v1/leads.json", before_write=before_write, json=payload
    )
    results = data.get("result") if isinstance(data, dict) else None
    result = results[0] if isinstance(results, list) and len(results) == 1 else None
    if not isinstance(result, dict):
        raise BadGateway("lead write failed")
    lead_id = result.get("id")
    if result.get("status") != expected or type(lead_id) is not int or lead_id <= 0:
        raise BadGateway("lead write failed")
    if not creating and lead_id != existing_id:
        raise BadGateway("lead write failed")
    return lead_id


def mark_registered(connection, program_id, lead_id, *, before_write):
    if not all(
        isinstance(v, int) and not isinstance(v, bool) and v > 0
        for v in (program_id, lead_id)
    ):
        raise ValueError("program_id and lead_id must be positive ints")
    path = f"/rest/v1/programs/{program_id}/members/status.json"
    payload = {"statusName": "Registered", "input": [{"leadId": lead_id}]}
    resp = connection.api("POST", path, before_write=before_write, json=payload)
    result = resp.get("result") if isinstance(resp, dict) else None
    if not isinstance(result, list) or len(result) != 1:
        raise BadGateway()
    item = result[0]
    if not isinstance(item, dict) or item.get("seq") != 0:
        raise BadGateway()
    status = item.get("status")
    if status in ("created", "updated"):
        if item.get("leadId") != lead_id:
            raise BadGateway()
        return True
    if status == "skipped":
        if "leadId" in item and item["leadId"] != lead_id:
            raise BadGateway()
        reasons = item.get("reasons")
        if (
            isinstance(reasons, list)
            and len(reasons) == 1
            and isinstance(reasons[0], dict)
            and reasons[0].get("code") == "1037"
        ):
            return True
    raise BadGateway()


def validate_mapping(form, mapping, fields):
    source = {}
    for entry in form.get("fields", []) if isinstance(form, dict) else form.fields:
        key = entry.get("id") if isinstance(entry, dict) else entry.id
        source[key] = entry
    destination = {item["name"]: item for item in fields}
    if not isinstance(mapping, dict) or len(mapping) > 20:
        raise ValueError("Invalid mapping")
    allowed = {
        "string",
        "text",
        "email",
        "phone",
        "url",
        "date",
        "datetime",
        "integer",
        "float",
        "currency",
    }
    result, seen = {}, set()
    for field_id, name in mapping.items():
        if not isinstance(name, str):
            raise ValueError("Invalid mapping")
        field, provider_field = source.get(field_id), destination.get(name)
        if field is None or provider_field is None or name in seen:
            raise ValueError("Invalid mapping")
        field_type = field.get("type") if isinstance(field, dict) else field.type
        provider_type = provider_field.get("type")
        if field_type == "checkbox":
            ok = provider_type == "boolean"
        elif field_type in ("text", "select"):
            ok = provider_type in allowed
        else:
            ok = False
        if not ok:
            raise ValueError("Invalid mapping")
        seen.add(name)
        result[field_id] = {
            "name": provider_field.get("name"),
            "type": provider_type,
            "length": provider_field.get("length"),
        }
    return result


def mapped_properties(answers, mapping):
    import datetime
    import math

    result = {}
    for source, target in (("first_name", "firstName"), ("last_name", "lastName")):
        answer = answers.get(source)
        if isinstance(answer, str) and answer.strip():
            result[target] = answer
    for key, meta in mapping.items():
        answer = answers.get(key)
        if answer is None or (isinstance(answer, str) and not answer.strip()):
            continue
        name, value_type, length = meta["name"], meta.get("type"), meta.get("length")
        if value_type == "boolean":
            if not isinstance(answer, bool):
                raise ValueError("Invalid answer")
            value = answer
        elif value_type in ("integer", "int"):
            if (
                not isinstance(answer, str)
                or re.fullmatch(r"[+-]?[0-9]+", answer) is None
            ):
                raise ValueError("Invalid answer")
            value = int(answer)
        elif value_type in ("float", "currency"):
            if not isinstance(answer, str):
                raise ValueError("Invalid answer")
            try:
                value = float(answer)
            except (TypeError, ValueError):
                raise ValueError("Invalid answer")
            if not math.isfinite(value):
                raise ValueError("Invalid answer")
        elif value_type == "date":
            try:
                value = datetime.date.fromisoformat(answer).isoformat()
            except (TypeError, ValueError):
                raise ValueError("Invalid answer")
        elif value_type in ("datetime", "date_time"):
            try:
                parsed = datetime.datetime.fromisoformat(answer)
            except (TypeError, ValueError):
                raise ValueError("Invalid answer")
            if parsed.utcoffset() is None:
                raise ValueError("Invalid answer")
            value = parsed.isoformat()
        else:
            if not isinstance(answer, str):
                raise ValueError("Invalid answer")
            if isinstance(length, int) and length > 0 and len(answer) > length:
                raise ValueError("Invalid answer")
            value = answer
        result[name] = value
    return result
