import os
import re

from flask import abort
from werkzeug.exceptions import BadRequest

from browser_account_security import email_key
from workspace_oauth import Config, Connection, check_provider, response_object

_MY_DOMAIN = re.compile(
    r"https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+my\.salesforce\.com/?",
    re.ASCII | re.IGNORECASE,
)


class SalesforceConnection(Connection):
    def __init__(self, tx, operator):
        candidate = os.environ.get("CUEPORCH_SALESFORCE_ORIGIN", "")
        self.origin = (
            candidate.rstrip("/").lower()
            if _MY_DOMAIN.fullmatch(candidate) and len(candidate[8:].rstrip("/")) <= 253
            else ""
        )
        oauth = self.origin + "/services/oauth2" if self.origin else ""
        super().__init__(Config(
            key="salesforce", name="Salesforce Sales Cloud",
            authorization_url=oauth + "/authorize",
            token_url=oauth + "/token",
            revoke_url=oauth + "/revoke" if oauth else "",
            scope="api refresh_token", nonexpiring_access=True,
        ), tx, operator)
        self.configured = bool(self.origin and self.configured)

    def scope_allowed(self, scope):
        scopes = set(scope.split()) if isinstance(scope, str) else set()
        return "api" in scopes and bool(scopes & {"refresh_token", "offline_access"})

    def exchange(self, fields, previous_refresh=None):
        self.require_configured()
        return super().exchange(fields, previous_refresh) | {"origin": self.origin}

    def tokens(self, row):
        saved = super().tokens(row)
        if not self.origin or not isinstance(saved, dict) or saved.get("origin") != self.origin:
            abort(409, "Reconnect Salesforce using the configured My Domain.")
        return saved


API_PATH = "/services/data/v64.0"
_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")
_NEXT_RE = re.compile(re.escape(API_PATH) + r"/query/([A-Za-z0-9_-]{1,256})")
_ID_RE = re.compile(r"701[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?")


def campaign_id(value):
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


def matching_campaign(given, returned):
    if not campaign_id(given) or not campaign_id(returned):
        return False
    if given == returned:
        return True
    if len(given) == 15 and len(returned) == 18:
        return returned[:15] == given
    if len(given) == 18 and len(returned) == 15:
        return given[:15] == returned
    return False


def _get(connection, revision, path, **kwargs):
    connection.require_configured()
    with connection.api(revision, "GET", connection.origin + API_PATH + path, **kwargs) as response:
        check_provider(response)
        return response_object(response)


def _query_page(connection, revision, soql, cursor=None):
    if cursor is None:
        path = "/query"
        kwargs = {"params": {"q": soql}}
    else:
        if not isinstance(cursor, str) or _CURSOR_RE.fullmatch(cursor) is None:
            abort(400, "invalid cursor")
        path = "/query/" + cursor
        kwargs = {}
    body = _get(connection, revision, path, **kwargs)
    return _query_result(body, cursor)


def _query_result(body, cursor):
    if not isinstance(body, dict):
        abort(502, "malformed query response")
    records = body.get("records")
    done = body.get("done")
    if not isinstance(records, list) or not (0 <= len(records) <= 2000):
        abort(502, "malformed query response")
    if not isinstance(done, bool):
        abort(502, "malformed query response")

    if done and body.get("nextRecordsUrl") is not None:
        abort(502, "malformed query response")

    next_cursor = None
    if not done:
        next_url = body.get("nextRecordsUrl")
        if not isinstance(next_url, str):
            abort(502, "malformed query response")
        match = _NEXT_RE.fullmatch(next_url)
        if match is None:
            abort(502, "malformed query response")
        next_cursor = match.group(1)
        if cursor is not None and next_cursor == cursor:
            abort(502, "malformed query response")

    return records, next_cursor


def _campaign(row):
    if not isinstance(row, dict):
        abort(502, "corrupted campaign")
    cid = row.get("Id")
    name = row.get("Name")
    active = row.get("IsActive")
    if not campaign_id(cid):
        abort(502, "corrupted campaign")
    if not isinstance(name, str) or not (1 <= len(name) <= 255) or not name.isprintable():
        abort(502, "corrupted campaign")
    if not isinstance(active, bool):
        abort(502, "corrupted campaign")
    return cid, name, active


def _campaigns_from_records(records, next_cursor):
    campaigns_list = []
    seen = set()
    for row in records:
        cid, name, active = _campaign(row)
        if not active or cid in seen:
            abort(502, "corrupted campaign")
        seen.add(cid)
        campaigns_list.append({"id": cid, "name": name})
    return {"campaigns": campaigns_list, "next_cursor": next_cursor}


def campaigns(connection, revision, cursor=None):
    soql = (
        "SELECT Id, Name, IsActive FROM Campaign "
        "WHERE IsActive = true ORDER BY Name, Id"
    )
    records, next_cursor = _query_page(connection, revision, soql, cursor)
    return _campaigns_from_records(records, next_cursor)


def campaign(connection, revision, value):
    if not campaign_id(value):
        abort(400, "invalid campaign id")
    body = _get(
        connection,
        revision,
        "/sobjects/Campaign/" + value,
        params={"fields": "Id,Name,IsActive"},
    )
    cid, name, active = _campaign(body)
    if not matching_campaign(value, cid):
        abort(502, "corrupted campaign")
    if not active:
        abort(409, "campaign inactive")
    return {"id": cid, "name": name}


def _member_name(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if len(stripped) > 80:
        return None
    if any(not ch.isprintable() for ch in stripped):
        return None
    return stripped


def _member_fields(row):
    if not row.get("LeadId") and not row.get("ContactId"):
        return None
    if row.get("HasOptedOutOfEmail") is not False:
        return None

    try:
        email = email_key(row.get("Email"))
    except BadRequest:
        return None

    first_name = _member_name(row.get("FirstName"))
    last_name = _member_name(row.get("LastName"))
    if first_name is None or last_name is None:
        return None

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
    }


def campaign_members(connection, revision, value):
    cid = campaign(connection, revision, value)["id"]
    soql = ("SELECT Id, CampaignId, LeadId, ContactId, Email, FirstName, LastName, "
            "HasOptedOutOfEmail FROM CampaignMember WHERE CampaignId = '%s' ORDER BY Id" % cid)
    results = []
    cursor = None
    seen = set()
    seen_ids = set()
    first = True
    while True:
        rows, next_cursor = _query_page(connection, revision, soql, cursor)
        if not first and not rows:
            abort(502)
        for row in rows:
            if not isinstance(row, dict):
                abort(502)
            rid = row.get("Id")
            if not (isinstance(rid, str) and rid):
                abort(502)
            if rid in seen_ids:
                abort(502)
            seen_ids.add(rid)
            if not matching_campaign(cid, row.get("CampaignId")):
                abort(502)
            results.append(_member_fields(row))
        first = False
        if next_cursor is None:
            break
        if next_cursor in seen:
            abort(502)
        seen.add(next_cursor)
        cursor = next_cursor
    return results


def person_id(object_name, value):
    if not isinstance(object_name, str) or not isinstance(value, str):
        return False
    prefix = {"Lead": "00Q", "Contact": "003"}.get(object_name)
    if prefix is None:
        return False
    if len(value) not in (15, 18):
        return False
    if not value.startswith(prefix):
        return False
    return value.isascii() and value.isalnum()


def _lookup_row(row, object_name, expected_email):
    if not isinstance(row, dict):
        abort(502)
    for key in ("Id", "Email", "HasOptedOutOfEmail"):
        if key not in row:
            abort(502)
    id_value = row["Id"]
    email_value = row["Email"]
    opted_out = row["HasOptedOutOfEmail"]
    if not person_id(object_name, id_value):
        abort(502)
    if not isinstance(email_value, str):
        abort(502)
    if not isinstance(opted_out, bool):
        abort(502)
    try:
        returned = email_key(email_value)
    except BadRequest:
        abort(502)
    if returned != expected_email:
        abort(502)
    return {"id": id_value, "opted_out": opted_out}


def lookup_person(connection, revision, address):
    normalized = email_key(address)
    escaped = normalized.replace("\\", "\\\\").replace("'", "\\'")
    matches = []
    for name in ("Contact", "Lead"):
        soql = (
            "SELECT Id,Email,HasOptedOutOfEmail FROM %s "
            "WHERE Email = '%s'" % (name, escaped)
        )
        if name == "Lead":
            soql += " AND IsConverted = false"
        soql += " LIMIT 2"
        records, cursor = _query_page(connection, revision, soql)
        if cursor is not None:
            abort(502)
        if not isinstance(records, (list, tuple)):
            abort(502)
        if len(records) > 2:
            abort(502)
        for row in records:
            matches.append((name, _lookup_row(row, name, normalized)))
    if not matches:
        return None
    if len(matches) > 1:
        abort(422, description="Multiple Salesforce contacts or leads match this email.")
    name, data = matches[0]
    return {"object": name, "id": data["id"], "opted_out": data["opted_out"]}
