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


def member_ids(connection, revision, list_id):
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
            return list(collected)
        if after in seen_cursors:
            abort(502, "Repeated membership cursor")
        seen_cursors.add(after)
    abort(502, "Too many membership pages")


def contacts(connection, revision, list_id):
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

    ids = list(member_ids(connection, revision, list_id))
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

    return output, skipped
