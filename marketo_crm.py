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
