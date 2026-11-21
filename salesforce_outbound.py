import re
from hashlib import sha256

from flask import abort
from werkzeug.exceptions import BadGateway, NotFound

from browser_account_security import email_key
from workspace_oauth import check_provider, response_object
import salesforce_crm

LEAD_KEY = "CuePorch_Email_Key__c"


def ensure_person(connection, revision, address, answers, company, before_write):
    normalized = email_key(address)
    existing = salesforce_crm.lookup_person(connection, revision, normalized)
    if existing:
        return existing
    company = company.strip() if isinstance(company, str) else ""
    if (not company or len(company) > 255 or not company.isprintable()
            or not normalized or len(normalized) > 80):
        abort(422)
    last_name = salesforce_crm._member_name(answers.get("last_name")) or "Registrant"
    first_name = salesforce_crm._member_name(answers.get("first_name"))
    payload = {
        "Email": normalized,
        "LastName": last_name,
        "Company": company,
        LEAD_KEY: sha256(normalized.encode("utf-8")).hexdigest(),
    }
    if first_name:
        payload["FirstName"] = first_name
    proposed_id = _create_record(connection, revision, "Lead", payload, before_write)
    confirmed = salesforce_crm.lookup_person(connection, revision, normalized)
    if not confirmed:
        abort(502)
    if proposed_id is not None and (confirmed["object"] != "Lead" or not same_id(confirmed["id"], proposed_id)):
        abort(502)
    return confirmed


def _create_record(connection, revision, object_name, fields, before_write):
    try:
        with connection.api(revision, "POST", connection.origin + salesforce_crm.API_PATH + "/sobjects/" + object_name, json=fields, before_send=before_write) as response:
            status = response.status_code
            if status == 201:
                payload = response_object(response)
                ident = payload.get("id")
                if payload.get("success") is True and isinstance(ident, str) and ident:
                    return ident
                return None
            if status in (502, 503, 504):
                return None
            if status in (400, 409):
                try:
                    body = response.json()
                except ValueError:
                    abort(422)
                codes = ("DUPLICATE_VALUE", "DUPLICATES_DETECTED")
                if isinstance(body, list) and any(
                    isinstance(x, dict) and x.get("errorCode") in codes for x in body
                ):
                    return None
                abort(422)
            if status == 429:
                check_provider(response)
                return None
            if 400 <= status < 500:
                abort(422)
            check_provider(response)
            return None
    except BadGateway:
        return None


MEDIA = "CuePorch_Media__c"
REGISTRANT = "CuePorch_Registrant__c"
EXTERNAL_KEY = "External_Key__c"
_ID_RE = "[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?"
_KEY_RE = "[a-f0-9]{64}"


def _is_id(value):
    return isinstance(value, str) and re.fullmatch(_ID_RE, value) is not None


def same_id(first, second):
    if not (_is_id(first) and _is_id(second)):
        return False
    return first == second or (len(first) != len(second) and first[:15] == second[:15])


def _read_record(connection, revision, path):
    try:
        return salesforce_crm._get(connection, revision, path)
    except NotFound:
        return None


def ensure_record(connection, revision, object_name, key, fields, expected, before_write):
    if object_name not in (MEDIA, REGISTRANT) or not (isinstance(key, str) and re.fullmatch(_KEY_RE, key)):
        abort(502)
    path = "/sobjects/" + object_name + "/" + EXTERNAL_KEY + "/" + key
    record = _read_record(connection, revision, path)
    created_id = None
    if record is None:
        created_id = _create_record(connection, revision, object_name, fields | {EXTERNAL_KEY: key}, before_write)
        record = _read_record(connection, revision, path)
    if not isinstance(record, dict):
        abort(502)
    rid = record.get("Id")
    if not _is_id(rid) or record.get(EXTERNAL_KEY) != key:
        abort(502)
    if created_id is not None and not same_id(created_id, rid):
        abort(502)
    for field, want in expected.items():
        if field not in record:
            abort(502)
        got = record[field]
        if want is None:
            if got is not None:
                abort(502)
        elif not same_id(got, want):
            abort(502)
    return rid


def publish_registration(connection, revision, *, campaign, kind, title,
                         source_ref, registrant, registered_at, address,
                         answers, company, before_write):
    campaign = salesforce_crm.campaign(connection, revision, campaign)["id"]
    person = ensure_person(connection, revision, address, answers, company,
                           before_write)
    if person.get("opted_out"):
        return "skipped"
    media_key = sha256("\0".join(
        ("cueporch-media", campaign, kind, source_ref)).encode()).hexdigest()
    registrar_key = sha256("\0".join(("cueporch-registrant", media_key,
                                     registrant)).encode()).hexdigest()
    media_id = ensure_record(connection, revision, MEDIA, media_key,
                  {"Name": title[:80], "Campaign__c": campaign,
                   "Kind__c": kind},
                  {"Campaign__c": campaign}, before_write)
    current = salesforce_crm.lookup_person(connection, revision, address)
    if not current or current["object"] != person["object"] or not same_id(current["id"], person["id"]):
        abort(409)
    if current.get("opted_out"):
        return "skipped"
    expected = {
        "Media__c": media_id,
        "Lead__c": current["id"] if current["object"] == "Lead" else None,
        "Contact__c": current["id"] if current["object"] == "Contact" else None,
    }
    fields = {k: v for k, v in expected.items() if v is not None}
    fields["Status__c"] = "Registered"
    fields["Registered_At__c"] = registered_at
    ensure_record(connection, revision, REGISTRANT, registrar_key, fields,
                  expected, before_write)
    return "sent"
