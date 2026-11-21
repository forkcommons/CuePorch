import re
from urllib.parse import quote

from flask import abort

from browser_account_security import email_key
from workspace_oauth import Config, Connection, check_provider, response_object

OAUTH = Config(
    key="keap",
    name="Keap",
    authorization_url="https://accounts.infusionsoft.com/app/oauth/authorize",
    token_url="https://api.infusionsoft.com/token",
    revoke_url="",
    scope="full",
    pkce=False,
)

API_BASE = "https://api.infusionsoft.com/crm/rest/v2"

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PINNED_SCOPE_RE = re.compile(
    r"^full\|[A-Za-z0-9][A-Za-z0-9.-]*\.infusionsoft\.com$"
)


class KeapConnection(Connection):
    def __init__(self, tx, operator):
        super().__init__(OAUTH, tx, operator)

    def exchange_kwargs(self, fields):
        if fields.get("grant_type") == "refresh_token":
            return dict(data=fields, auth=(self.client, self.secret))
        return super().exchange_kwargs(fields)

    def scope_allowed(self, scope):
        if not isinstance(scope, str):
            return False
        if scope == "full":
            return True
        return bool(_PINNED_SCOPE_RE.fullmatch(scope))


def _get(connection, revision, path, params=None):
    with connection.api(revision, "GET", API_BASE + path, params=params) as response:
        check_provider(response)
        return response_object(response)


def _valid_cursor(c):
    return (
        isinstance(c, str)
        and 0 < len(c) <= 1024
        and all(32 <= ord(ch) <= 126 for ch in c)
    )


def _valid_id(value):
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _parse_tag(row):
    if not isinstance(row, dict):
        abort(502)
    tid = row.get("id")
    if not _valid_id(tid):
        abort(502)
    name = row.get("name")
    if not isinstance(name, str):
        abort(502)
    name = name.strip()
    if not name or len(name) > 255 or not name.isprintable():
        abort(502)
    return {"id": tid, "name": name}


def contact_lists(connection, revision, cursor=None):
    if cursor is not None and not _valid_cursor(cursor):
        abort(400)

    params = {"page_size": 50}
    if cursor is not None:
        params["page_token"] = cursor

    data = _get(connection, revision, "/tags", params=params)
    if not isinstance(data, dict):
        abort(502)

    rows = data.get("tags")
    if not isinstance(rows, list) or len(rows) > 50:
        abort(502)

    tags = []
    seen = set()
    for row in rows:
        item = _parse_tag(row)
        if item["id"] in seen:
            abort(502)
        seen.add(item["id"])
        tags.append(item)

    next_cursor = data.get("next_page_token")
    if next_cursor is not None:
        if not _valid_cursor(next_cursor) or next_cursor == cursor:
            abort(502)

    return {"lists": tags, "next_cursor": next_cursor}


def contact_list(connection, revision, value):
    if not _valid_id(value):
        abort(404)

    data = _get(connection, revision, "/tags/" + value)
    item = _parse_tag(data)
    if item["id"] != value:
        abort(502)
    return item


def _contact_match(row, email):
    if not isinstance(row, dict):
        abort(502)
    contact_id = row.get('id')
    if not _valid_id(contact_id):
        abort(502)
    email_addresses = row.get('email_addresses')
    if not isinstance(email_addresses, list):
        abort(502)
    for entry in email_addresses:
        if not isinstance(entry, dict):
            abort(502)
        addr = entry.get('email')
        if not isinstance(addr, str) or not addr.strip():
            abort(502)
        if not isinstance(entry.get('field'), str):
            abort(502)
    tag_ids = row.get('tag_ids')
    if tag_ids is not None and (not isinstance(tag_ids, list) or not all(_valid_id(tag) for tag in tag_ids)):
        abort(502)
    matches = [entry for entry in email_addresses if entry['email'].strip().casefold() == email]
    if not matches:
        return None
    return {
        'id': contact_id,
        'primary': any(entry.get('field') == 'EMAIL1' for entry in matches),
        'tag_ids': tuple(tag_ids) if tag_ids is not None else ()
    }


def find_contact(connection, revision, email):
    normalized = email_key(email)
    if any(char in normalized for char in "*;=,\\:"):
        abort(400, "This address cannot be used in a Keap contact lookup.")

    params = {
        "filter": f"email=={normalized}",
        "fields": "id,email_addresses,tag_ids",
        "page_size": 50,
    }
    match = None
    seen = set()
    for _ in range(10):
        page = _get(connection, revision, "/contacts", params)
        contacts = page.get("contacts")
        if not isinstance(contacts, list) or len(contacts) > 50:
            abort(502, "Keap returned an invalid contact page.")
        for row in contacts:
            candidate = _contact_match(row, normalized)
            if candidate is None:
                continue
            if match is not None and candidate != match:
                abort(502, "Keap returned ambiguous records for this address.")
            match = candidate

        cursor = page.get("next_page_token")
        if cursor in (None, ""):
            return match
        if not _valid_cursor(cursor) or cursor in seen:
            abort(502, "Keap returned an invalid contact page token.")
        seen.add(cursor)
        params["page_token"] = cursor

    abort(502, "Keap did not finish the contact lookup.")


def read_contact(connection, revision, contact_id, email):
    if not _valid_id(contact_id):
        abort(400, "invalid contact id")
    params = {"fields": "id,email_addresses,tag_ids"}
    response = _get(connection, revision, f"/contacts/{contact_id}", params)
    if not isinstance(response, dict):
        abort(502, "invalid contact response")
    match = _contact_match(response, email_key(email))
    if match is None:
        abort(502, "contact identity mismatch")
    if match["id"] != contact_id:
        abort(502, "contact identity mismatch")
    return match

_MARKETABLE = frozenset({
    'UNENGAGED_MARKETABLE',
    'SINGLE_OPT_IN',
    'DOUBLE_OPT_IN',
    'CONFIRMED',
})


def marketing_opt_in(connection, revision, email):
    if not isinstance(email, str) or not email.strip():
        abort(422)
    try:
        normalized = email_key(email)
    except Exception:
        abort(422)
    if not isinstance(normalized, str) or not normalized:
        abort(422)
    path = "/emailAddresses/" + quote(normalized, safe='') + "/status"
    with connection.api(revision, "GET", API_BASE + path) as response:
        if response.status_code == 404:
            return None
        check_provider(response)
        data = response_object(response)
        if not isinstance(data, dict):
            abort(502)
        email_field = data.get('email')
        if not isinstance(email_field, str) or email_field.casefold() != normalized.casefold():
            abort(502)
        opted_in = data.get('opted_in')
        status = data.get('status')
        if type(opted_in) is not bool or not isinstance(status, str):
            abort(502)
        return opted_in and status in _MARKETABLE


def create_contact(connection, revision, email, first, last, *, before_send):
    if not isinstance(email, str) or not isinstance(first, str) or not isinstance(last, str):
        abort(422)
    try:
        normalized = email_key(email)
    except Exception:
        abort(422)
    if not isinstance(normalized, str) or not normalized or len(normalized) > 75:
        abort(422)
    cleaned = []
    for value in (first, last):
        value = value.strip()
        if not value or len(value) > 40 or not value.isprintable():
            abort(422)
        cleaned.append(value)
    body = {
        'given_name': cleaned[0],
        'family_name': cleaned[1],
        'email_addresses': [{
            'email': normalized,
            'field': 'EMAIL1',
            'opt_in_reason': 'Viewer opted in through a CuePorch registration form',
        }],
    }
    with connection.api(
        revision,
        "POST",
        API_BASE + "/contacts",
        params={'fields': 'id,email_addresses,tag_ids'},
        json=body,
        before_send=before_send,
    ) as response:
        if response.status_code not in (200, 201):
            check_provider(response)
        data = response_object(response)
    match = _contact_match(data, normalized.casefold())
    if not match or not match['primary']:
        abort(502)
    return match['id']


def apply_tag(connection, revision, tag_id, contact_id, *, before_send):
    if not _valid_id(tag_id):
        abort(422)
    if not _valid_id(contact_id):
        abort(422)

    url = API_BASE + f'/tags/{tag_id}/contacts:applyTags'

    with connection.api(
        revision,
        'POST',
        url,
        json={'contact_ids': [contact_id]},
        before_send=before_send,
    ) as response:
        check_provider(response)
        results = response_object(response).get('results')
        if not (isinstance(results, dict) and isinstance(results.get(contact_id), str)):
            abort(502)

    # Actual membership verification is performed in a separate application stage.
