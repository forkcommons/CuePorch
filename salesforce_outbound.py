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
REGISTRATION_FIELD = "CuePorch_Registration_Field__c"
WEBINAR_POLL = "CuePorch_Webinar_Poll__c"
WEBINAR_QA = "CuePorch_Webinar_QA__c"
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


def _confirmed_record(record, key, expected, created_id=None):
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
    return record


def ensure_record(connection, revision, object_name, key, fields, expected, before_write):
    if object_name not in (MEDIA, REGISTRANT, REGISTRATION_FIELD, WEBINAR_POLL, WEBINAR_QA) or not (isinstance(key, str) and re.fullmatch(_KEY_RE, key)):
        abort(502)
    path = "/sobjects/" + object_name + "/" + EXTERNAL_KEY + "/" + key
    record = _read_record(connection, revision, path)
    created_id = None
    if record is None:
        created_id = _create_record(connection, revision, object_name, fields | {EXTERNAL_KEY: key}, before_write)
        record = _read_record(connection, revision, path)
    return _confirmed_record(record, key, expected, created_id)["Id"]


def registration_keys(campaign, kind, source_ref, registrant):
    media_key = sha256("\0".join(
        ("cueporch-media", campaign, kind, source_ref)).encode()).hexdigest()
    registrar_key = sha256("\0".join(("cueporch-registrant", media_key,
                                     registrant)).encode()).hexdigest()
    return media_key, registrar_key


def publish_registration(connection, revision, *, campaign, kind, title,
                         source_ref, registrant, registered_at, address,
                         answers, company, before_write, related_fields=()):
    campaign = salesforce_crm.campaign(connection, revision, campaign)["id"]
    person = ensure_person(connection, revision, address, answers, company,
                           before_write)
    if person.get("opted_out"):
        return "skipped"
    media_key, registrar_key = registration_keys(campaign, kind, source_ref,
                                                 registrant)
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
    registrant_id = ensure_record(connection, revision, REGISTRANT, registrar_key, fields,
                  expected, before_write)
    for field_id, field_name, field_type, value in related_fields:
        child_key = sha256("\0".join(("cueporch-registration-field", registrar_key, field_id)).encode()).hexdigest()
        ensure_record(connection, revision, REGISTRATION_FIELD, child_key,
                      {"Registrant__c": registrant_id, "Field_Name__c": field_name,
                       "Field_Type__c": field_type, "Value__c": value},
                      {"Registrant__c": registrant_id}, before_write)
    return "sent"

def validate_installation(connection, revision, *, related_fields=False, engagement_details=False):
    expected = (
        ("Lead", LEAD_KEY, (LEAD_KEY, "Email", "Company", "LastName", "FirstName")),
        (MEDIA, EXTERNAL_KEY, (EXTERNAL_KEY, "Name", "Campaign__c", "Kind__c")),
        (REGISTRANT, EXTERNAL_KEY, (EXTERNAL_KEY, "Media__c", "Lead__c", "Contact__c", "Status__c", "Registered_At__c")),
    )
    if related_fields:
        expected += (
            (REGISTRATION_FIELD, EXTERNAL_KEY, (EXTERNAL_KEY, "Registrant__c", "Field_Name__c", "Field_Type__c", "Value__c")),
        )
    if engagement_details:
        fields = (EXTERNAL_KEY, "Registrant__c", "Question__c", "Answer__c")
        expected += ((WEBINAR_POLL, EXTERNAL_KEY, fields), (WEBINAR_QA, EXTERNAL_KEY, fields))
    for object_name, unique_key, required_fields in expected:
        try:
            result = salesforce_crm._get(connection, revision, "/sobjects/" + object_name + "/describe")
        except NotFound:
            abort(409, "Deploy the CuePorch Salesforce metadata before enabling registration delivery.")
        if not isinstance(result, dict) or result.get("createable") is not True or not isinstance(result.get("fields"), list):
            abort(409, f"Salesforce {object_name} metadata is missing create permission or configuration.")
        by_name = {}
        for field in result["fields"]:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                abort(502, f"Salesforce {object_name} field metadata is malformed.")
            by_name[field["name"]] = field
        for field_name in required_fields:
            field = by_name.get(field_name)
            if not field or field.get("createable") is not True:
                abort(409, f"Salesforce {object_name}.{field_name} must be createable and configured for registration delivery.")
        unique = by_name.get(unique_key)
        if not unique or unique.get("externalId") is not True or unique.get("unique") is not True:
            abort(409, f"Salesforce {object_name}.{unique_key} must be an external ID and unique. Update the CuePorch Salesforce metadata before enabling registration delivery.")

def _patch_registrant(connection, revision, record_id, fields, before_write):
    if not _is_id(record_id):
        abort(502)
    url = (
        connection.origin
        + salesforce_crm.API_PATH
        + "/sobjects/"
        + REGISTRANT
        + "/"
        + record_id
    )
    try:
        with connection.api(
            revision,
            "PATCH",
            url,
            json=fields,
            before_send=before_write,
        ) as response:
            status = response.status_code
            if status in (204, 502, 503, 504):
                return
            if status == 400:
                abort(422)
            if status == 401:
                abort(403)
            if status == 200:
                abort(502)
            check_provider(response)
    except BadGateway:
        return

def _engagement_changes(record, *, status, watch_percent, video_views):
    """Compute Salesforce outbound engagement field changes for one registrant."""
    transitions = {
        'Attended Live': ('Registered', 'Invited', 'No Show'),
        'No Show': ('Registered', 'Invited'),
        'Registered': ('No Show',),
    }

    if status is not None and (not isinstance(status, str) or status not in transitions):
        abort(422)

    if watch_percent is not None:
        if type(watch_percent) is not int or not (0 <= watch_percent <= 100):
            abort(422)

    if video_views is not None:
        if type(video_views) is not int or not (0 <= video_views <= 999999999999999999):
            abort(422)

    def _current_metric(value, upper):
        if value is None:
            return None
        if type(value) is bool:
            abort(502)
        if type(value) is int:
            number = value
        elif type(value) is float:
            if not value.is_integer():
                abort(502)
            number = int(value)
        else:
            abort(502)
        if number < 0 or number > upper:
            abort(502)
        return number

    changes = {}

    if status is not None and record.get('Status__c') in transitions[status]:
        changes['Status__c'] = status

    if watch_percent is not None:
        current_wp = _current_metric(record.get("Watch_Percent__c"), 100)
        if current_wp is None or watch_percent > current_wp:
            changes["Watch_Percent__c"] = watch_percent

    if video_views is not None:
        current_vv = _current_metric(record.get("Video_Views__c"), 999999999999999999)
        if current_vv is None or video_views > current_vv:
            changes["Video_Views__c"] = video_views

    return changes

def _existing_engagement_record(connection, revision, *, campaign, kind, source_ref, registrant, address):
    campaign = salesforce_crm.campaign(connection, revision, campaign)["id"]
    person = salesforce_crm.lookup_person(connection, revision, address)
    if not person:
        abort(409)
    if person.get('opted_out'):
        return None
    media_key, registrar_key = registration_keys(campaign, kind, source_ref, registrant)
    media_path = f'/sobjects/{MEDIA}/{EXTERNAL_KEY}/{media_key}'
    registrant_path = f'/sobjects/{REGISTRANT}/{EXTERNAL_KEY}/{registrar_key}'
    media = _read_record(connection, revision, media_path)
    if media is None:
        abort(404)
    record = _read_record(connection, revision, registrant_path)
    if record is None:
        abort(404)
    media = _confirmed_record(media, media_key, {'Campaign__c': campaign})
    if media.get('Kind__c') != kind:
        abort(409)
    expected = {
        'Media__c': media['Id'],
        'Lead__c': person['id'] if person['object'] == 'Lead' else None,
        'Contact__c': person['id'] if person['object'] == 'Contact' else None,
    }
    record = _confirmed_record(record, registrar_key, expected)
    return record, registrant_path, registrar_key, expected

def _write_engagement_details(connection, revision, record, registrar_key, poll_records, question_records, before_write):
    engagement_sources = (
        (WEBINAR_POLL, poll_records),
        (WEBINAR_QA, question_records),
    )

    for object_name, records in engagement_sources:
        for activity_id, question, answer in records:
            external_key = sha256(
                "\x00".join(
                    str(value)
                    for value in (object_name, registrar_key, activity_id, answer)
                ).encode("utf-8")
            ).hexdigest()

            fields = {
                "Registrant__c": record["Id"],
                "Question__c": question,
                "Answer__c": answer,
            }
            expected = {
                "Registrant__c": record["Id"],
            }

            ensure_record(
                connection,
                revision,
                object_name,
                external_key,
                fields,
                expected,
                before_write,
            )
def publish_engagement(
    connection,
    revision,
    *,
    campaign,
    kind,
    source_ref,
    registrant,
    address,
    before_write,
    status=None,
    watch_percent=None,
    video_views=None,
    poll_records=(),
    question_records=(),
):
    if (
        kind not in ('video', 'webinar')
        or (kind == 'webinar' and video_views is not None)
        or (kind == 'video' and (status is not None or poll_records or question_records))
    ):
        abort(422)
    existing = _existing_engagement_record(
        connection,
        revision,
        campaign=campaign,
        kind=kind,
        source_ref=source_ref,
        registrant=registrant,
        address=address,
    )
    if existing is None:
        return 'skipped'
    record, path, key, expected = existing
    changes = _engagement_changes(
        record,
        status=status,
        watch_percent=watch_percent,
        video_views=video_views,
    )
    _write_engagement_details(
        connection, revision, record, key, poll_records, question_records, before_write
    )
    if not changes:
        return 'sent'
    _patch_registrant(connection, revision, record['Id'], changes, before_write)
    confirmed = _confirmed_record(
        _read_record(connection, revision, path),
        key,
        expected,
        record['Id'],
    )
    if 'Status__c' in changes and confirmed.get('Status__c') not in (status, 'Attended Live'):
        abort(502)
    if _engagement_changes(
        confirmed,
        status=None,
        watch_percent=watch_percent,
        video_views=video_views,
    ):
        abort(502)
    return 'sent'


def validate_engagement_installation(connection, revision, *, kind):
    required = {
        "video": ("Watch_Percent__c", "Video_Views__c"),
        "webinar": ("Watch_Percent__c", "Status__c"),
    }
    if kind not in required:
        abort(422, "Unsupported engagement kind")
    try:
        described = salesforce_crm._get(connection, revision, f"/sobjects/{REGISTRANT}/describe")
    except NotFound:
        abort(409, "Deploy the Registrant metadata first")
    if not (isinstance(described, dict) and described.get("updateable") is True):
        abort(409, "Grant update permission on CuePorch Registrants.")
    fields = described.get("fields")
    if not isinstance(fields, list):
        abort(502, "Invalid describe response")
    updateable = {
        f["name"]
        for f in fields
        if isinstance(f, dict) and isinstance(f.get("name"), str) and f.get("updateable") is True
    }
    missing = [n for n in required[kind] if n not in updateable]
    if missing:
        abort(409, "Missing update permission: " + ", ".join(missing))
