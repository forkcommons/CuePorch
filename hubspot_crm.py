from urllib.parse import quote

from flask import abort
from workspace_oauth import Config, check_provider, response_object

ORIGIN = "https://api.hubapi.com"
OAUTH = Config(
    "hubspot",
    "HubSpot",
    "https://app.hubspot.com/oauth/authorize",
    ORIGIN + "/oauth/2026-03/token",
    ORIGIN + "/oauth/2026-03/token/revoke",
    "crm.lists.read crm.objects.contacts.read",
    pkce=False,
    implicit_bearer=True,
)
LISTS = ORIGIN + "/crm/lists/2026-09"


def oauth_config():
    import os
    from dataclasses import replace

    if os.environ.get("CUEPORCH_HUBSPOT_OUTBOUND") == "1":
        return replace(
            OAUTH,
            authorization_parameters={
                "optional_scope": "crm.lists.write crm.objects.contacts.write communication_preferences.read_write"
            },
        )
    return OAUTH


def crm(connection, revision, method, url, **kwargs):
    with connection.api(revision, method, url, **kwargs) as response:
        check_provider(response)
        return response_object(response)


def results(data):
    if not isinstance(data, dict):
        abort(502)
    items = data.get("results")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        abort(502)
    return items


def cursor(data):
    if not isinstance(data, dict):
        abort(502)
    if "paging" not in data:
        return None
    paging = data["paging"]
    if not isinstance(paging, dict):
        abort(502)
    if "next" not in paging:
        return None
    nxt = paging["next"]
    if not isinstance(nxt, dict) or "after" not in nxt:
        abort(502)
    after = nxt["after"]
    if not isinstance(after, str) or not after or len(after) > 2048:
        abort(502)
    return after


def segment_id(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 30
        and all("0" <= ch <= "9" for ch in value)
    )


def segments(connection, revision, after=""):
    if not isinstance(after, str) or len(after) > 2048:
        abort(400)

    payload = {"objectTypeId": "0-1", "count": 50}
    if after:
        payload["after"] = after

    data = crm(connection, revision, "POST", LISTS + "/all", json=payload)
    items = results(data)
    if len(items) > 50:
        abort(502)

    out = []
    for item in items:
        if item.get("objectTypeId") != "0-1":
            abort(502)
        list_id = item.get("listId")
        if not segment_id(list_id):
            abort(502)
        name = item.get("name")
        if not isinstance(name, str) or not name or len(name) > 512:
            abort(502)
        processing_type = item.get("processingType")
        if processing_type not in ("MANUAL", "DYNAMIC", "SNAPSHOT"):
            abort(502)
        out.append({"id": list_id, "name": name, "type": processing_type})

    return {"segments": out, "after": cursor(data)}


def segment_membership(connection, revision, list_id):
    from registration_csv import MAX_ATTENDEES

    if not segment_id(list_id):
        abort(400, "Invalid list id")
    list_data = crm(connection, revision, "GET", f"{LISTS}/{list_id}")
    list_wrapper = list_data.get("list")
    if not isinstance(list_wrapper, dict) or list_wrapper.get("listId") != list_id:
        abort(400, "Invalid list metadata or listId mismatch")
    if list_wrapper.get("objectTypeId") != "0-1":
        abort(422, "List is not a supported object type")
    if list_wrapper.get("processingStatus") != "COMPLETE":
        abort(409, "List processing is not complete")
    url = f"{LISTS}/{list_id}/memberships"
    collected = {}
    after = None
    seen_cursors = set()
    for _ in range(20):
        kwargs = {"limit": 100}
        if after is not None:
            kwargs["after"] = after
        data = crm(connection, revision, "GET", url, params=kwargs)
        rows = results(data)
        if len(rows) > 100:
            abort(502, "Membership page exceeds 100 records")
        for row in rows:
            record_id = row.get("recordId")
            if not segment_id(record_id):
                abort(502, "Invalid recordId in membership response")
            collected[record_id] = None
        if len(collected) > MAX_ATTENDEES:
            abort(413, "Too many attendees")
        after = cursor(data)
        if after is None:
            return list(collected), list_wrapper.get("processingType")
        if after in seen_cursors:
            abort(502, "Repeated membership cursor")
        seen_cursors.add(after)
    abort(502, "Too many membership pages")


def contacts(connection, revision, list_id, *, include_membership=False):
    from library_registration import EMAIL

    def clean_email(value):
        if not isinstance(value, str):
            return None
        s = value.strip()
        if not s or len(s) > 254:
            return None
        for ch in s:
            o = ord(ch)
            if o < 32 or o == 127:
                return None
        return s if EMAIL.fullmatch(s) else None

    def clean_name(value):
        if not isinstance(value, str):
            return ""
        s = value.strip()
        return s if len(s) <= 80 else ""

    ids, processing_type = segment_membership(connection, revision, list_id)
    if include_membership and processing_type not in ("DYNAMIC", "MANUAL", "SNAPSHOT"):
        abort(502)
    url = ORIGIN + "/crm/objects/2026-09/contacts/batch/read"
    properties = ["email", "firstname", "lastname"]

    output = []
    skipped = 0

    for start in range(0, len(ids), 100):
        batch = ids[start : start + 100]
        batch_set = set(batch)

        payload = {
            "inputs": [{"id": cid} for cid in batch],
            "properties": properties,
        }

        response = crm(connection, revision, "POST", url, json=payload)
        items = results(response)

        by_id = {}
        for item in items:
            rid = item.get("id")
            if not isinstance(rid, str):
                abort(502)
            if rid in by_id:
                abort(502)
            if rid not in batch_set:
                abort(502)
            by_id[rid] = item

        for cid in batch:
            item = by_id.get(cid)
            if item is None:
                skipped += 1
                continue

            archived = item.get("archived")
            if not isinstance(archived, bool):
                abort(502)

            props = item.get("properties")
            if not isinstance(props, dict):
                abort(502)

            if archived:
                skipped += 1
                continue

            email = clean_email(props.get("email"))
            if email is None:
                skipped += 1
                continue

            output.append(
                {
                    "email": email,
                    "first_name": clean_name(props.get("firstname")),
                    "last_name": clean_name(props.get("lastname")),
                }
            )
            if include_membership:
                output[-1]["contact_id"] = cid

    if include_membership:
        return output, skipped, {"ids": ids, "active": processing_type == "DYNAMIC"}
    return output, skipped


def contact_record(connection, revision, email):
    url = ORIGIN + "/crm/objects/2026-09/contacts/" + quote(email, safe="")
    with connection.api(
        revision,
        "GET",
        url,
        params={"idProperty": "email", "properties": "email"},
    ) as response:
        if response.status_code == 404:
            return None
        check_provider(response)
        data = response_object(response)

    properties = data.get("properties")
    if (
        not segment_id(data.get("id"))
        or data.get("archived") is not False
        or not isinstance(properties, dict)
        or not isinstance(properties.get("email"), str)
        or properties["email"].casefold() != email.casefold()
    ):
        abort(502, "HubSpot returned an unrelated contact.")

    return data["id"]


PREFS = ORIGIN + "/communication-preferences/2026-09"


def preference_results(data):
    if not isinstance(data, dict):
        abort(502)

    if data.get("status") not in ("COMPLETE", "SUCCESS"):
        abort(502)

    if data.get("errors"):
        abort(502)

    if "numErrors" in data and data["numErrors"] != 0:
        abort(502)

    if data.get("error"):
        abort(502)

    items = data.get("results")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        abort(502)

    return items


def subscriptions(connection, revision):
    data = crm(connection, revision, "GET", PREFS + "/definitions")
    items = preference_results(data)

    out = []
    for item in items:
        method = item.get("communicationMethod")
        if not isinstance(method, str) or method.lower() != "email":
            continue
        if item.get("isActive") is not True:
            continue
        if item.get("isInternal") is True:
            continue

        subscription_id = item.get("id")
        if not segment_id(subscription_id):
            continue

        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue

        entry = {"id": str(subscription_id), "name": name}

        business_unit = item.get("businessUnitId", 0)
        if business_unit is None:
            business_unit = 0
        if business_unit:
            entry["business_unit"] = business_unit

        out.append(entry)

    return out


def opted_out(connection, revision, email, subscription_id, business_unit=0):
    if (
        not isinstance(business_unit, int)
        or isinstance(business_unit, bool)
        or business_unit < 0
    ):
        abort(400)

    path = PREFS + "/statuses/" + quote(email, safe="")
    email_key = email.casefold()
    known_statuses = ("UNSUBSCRIBED", "SUBSCRIBED", "NOT_SPECIFIED")

    base_params = {"channel": "EMAIL"}
    queries = [dict(base_params)]
    if business_unit:
        queries.append(dict(base_params, businessUnitId=business_unit))

    def preference_rows(url, params):
        data = crm(connection, revision, "GET", url, params=params)
        items = preference_results(data)

        for item in items:
            subscriber = item.get("subscriberIdString")
            if not isinstance(subscriber, str) or subscriber.casefold() != email_key:
                abort(502)

            if item.get("channel") != "EMAIL":
                abort(502)

            status = item.get("status")
            if status not in known_statuses:
                abort(502)

        return items

    unsubscribe_all = False
    for params in queries:
        for item in preference_rows(path + "/unsubscribe-all", params):
            if item["status"] == "UNSUBSCRIBED":
                unsubscribe_all = True

    if unsubscribe_all:
        return True

    target = str(subscription_id)
    selected_unsubscribed = False

    for params in queries:
        for item in preference_rows(path, params):
            sid = item.get("subscriptionId")
            if sid is None or str(sid) != target:
                continue
            if item["status"] == "UNSUBSCRIBED":
                selected_unsubscribed = True

    return selected_unsubscribed


def outbound_segment(connection, revision, list_id):
    if not segment_id(list_id):
        abort(400, "Invalid list id")

    data = crm(connection, revision, "GET", LISTS + "/" + list_id)
    if not isinstance(data, dict):
        abort(502)
    wrapper = data.get("list")
    if not isinstance(wrapper, dict):
        abort(502)
    if wrapper.get("listId") != list_id:
        abort(502)
    if wrapper.get("objectTypeId") != "0-1":
        abort(422)
    if wrapper.get("processingType") not in ("MANUAL", "SNAPSHOT"):
        abort(422)
    if wrapper.get("processingStatus") != "COMPLETE":
        abort(409)
    name = wrapper.get("name")
    if not isinstance(name, str) or not name or len(name) > 512:
        abort(502)
    return name


def save_contact(connection, revision, email, properties, *, before_write):
    base = ORIGIN + "/crm/objects/2026-09/contacts"

    fields = {}
    for key in ("firstname", "lastname"):
        value = properties.get(key)
        if isinstance(value, str):
            value = value.strip()
            if value:
                fields[key] = value[:80]

    contact_id = contact_record(connection, revision, email)

    if contact_id is None:
        before_write()
        with connection.api(
            revision,
            "POST",
            base,
            json={"properties": dict(fields, email=email)},
        ) as response:
            if response.status_code == 201:
                data = response_object(response)
                contact_id = data.get("id")
                if not segment_id(contact_id):
                    abort(502)

                returned_properties = data.get("properties")
                if isinstance(returned_properties, dict):
                    echoed_email = returned_properties.get("email")
                    if echoed_email is not None:
                        if (
                            not isinstance(echoed_email, str)
                            or echoed_email.casefold() != email.casefold()
                        ):
                            abort(502)
                return contact_id

            if response.status_code == 409:
                contact_id = contact_record(connection, revision, email)
                if contact_id is None:
                    abort(409)
            else:
                check_provider(response)
                abort(502)

    if fields:
        before_write()
        crm(
            connection,
            revision,
            "PATCH",
            f"{base}/{contact_id}",
            json={"properties": fields},
        )

    return contact_id


def add_to_segment(connection, revision, list_id, contact_id, *, before_write):
    if not segment_id(list_id) or not segment_id(contact_id):
        abort(400, "Invalid id")

    before_write()
    crm(
        connection,
        revision,
        "PUT",
        LISTS + "/" + list_id + "/memberships/add",
        json=[contact_id],
    )

    confirm = LISTS + "/records/0-1/" + contact_id + "/memberships"
    after = None
    seen_cursors = set()
    seen_lists = set()

    for _ in range(20):
        params = {"limit": 100}
        if after is not None:
            params["after"] = after
        data = crm(connection, revision, "GET", confirm, params=params)
        rows = results(data)
        if len(rows) > 100:
            abort(502)
        for row in rows:
            rid = row.get("listId")
            if not segment_id(rid):
                abort(502)
            if rid in seen_lists:
                abort(502, "Duplicate list id")
            seen_lists.add(rid)
            if rid == list_id:
                return True
        after = cursor(data)
        if after is None:
            break
        if after in seen_cursors:
            abort(502, "Repeated cursor")
        seen_cursors.add(after)

    abort(502, "Membership not confirmed")
