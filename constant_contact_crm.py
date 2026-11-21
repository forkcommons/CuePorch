import re
from urllib.parse import parse_qsl, urlsplit

from flask import abort

from browser_account_security import email_key
from workspace_oauth import check_provider, response_object, Config

OAUTH = Config(
    key="constant-contact",
    name="Constant Contact",
    authorization_url="https://authz.constantcontact.com/oauth2/default/v1/authorize",
    token_url="https://authz.constantcontact.com/oauth2/default/v1/token",
    revoke_url="",
    scope="contact_data offline_access",
    pkce=False,
    token_basic_query=True,
)
API_BASE = "https://api.cc.email/v3"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def list_id(value):
    return isinstance(value, str) and bool(_UUID_RE.fullmatch(value))


def _get(connection, revision, path, params=None):
    with connection.api(revision, "GET", API_BASE + path, params=params) as response:
        check_provider(response)
        return response_object(response)


def _list(row):
    if not isinstance(row, dict):
        abort(502, "Malformed Constant Contact provider data")
    lid = row.get("list_id")
    name = row.get("name")
    if not list_id(lid):
        abort(502, "Malformed Constant Contact list id")
    if not isinstance(name, str) or not name.strip():
        abort(502, "Malformed Constant Contact list name")
    name = name.strip()
    if len(name) > 255 or not name.isprintable():
        abort(502, "Malformed Constant Contact list name")
    out = {"id": lid.lower(), "name": name}
    if "dynamic_filtered_list" in row:
        dynamic = row["dynamic_filtered_list"]
        if not isinstance(dynamic, bool):
            abort(502, "Malformed Constant Contact dynamic flag")
        out["dynamic"] = dynamic
    return out


def _next_cursor(data, cursor):
    if not isinstance(data, dict) or "_links" not in data:
        return None
    links = data["_links"]
    if not isinstance(links, dict):
        abort(502)
    if "next" not in links:
        return None
    nxt = links["next"]
    if not isinstance(nxt, dict):
        abort(502)
    href = nxt.get("href")
    if not isinstance(href, str) or not href or len(href) > 4096:
        abort(502)
    try:
        parts = urlsplit(href)
        if parts.scheme or parts.netloc or parts.fragment or parts.path != "/v3/contact_lists":
            abort(502)
        pairs = parse_qsl(parts.query, strict_parsing=True, max_num_fields=2, keep_blank_values=True)
    except ValueError:
        abort(502)
    if len(pairs) != 1 or pairs[0][0] != "cursor":
        abort(502)
    value = pairs[0][1]
    if not _valid_cursor(value) or value == cursor:
        abort(502)
    return value


def _valid_cursor(c):
    return (
        isinstance(c, str)
        and 0 < len(c) <= 1024
        and all(32 <= ord(ch) <= 126 for ch in c)
    )


def contact_lists(connection, revision, cursor=None):
    if cursor is not None and not _valid_cursor(cursor):
        abort(400)

    if cursor is None:
        params = {"limit": 50, "status": "active", "channel_type": "email"}
    else:
        params = {"cursor": cursor}

    data = _get(connection, revision, "/contact_lists", params=params)
    if not isinstance(data, dict):
        abort(502)

    rows = data.get("lists")
    if not isinstance(rows, list) or len(rows) > 50:
        abort(502)

    lists = []
    seen = set()
    for row in rows:
        item = _list(row)
        if not isinstance(item, dict):
            abort(502)
        lid = item.get("id")
        name = item.get("name")
        if not isinstance(lid, str) or not isinstance(name, str):
            abort(502)
        if lid in seen:
            abort(502)
        seen.add(lid)
        if item.get("dynamic") is True:
            continue
        lists.append({"id": lid, "name": name})

    return {"lists": lists, "next_cursor": _next_cursor(data, cursor)}


def contact_list(connection, revision, value):
    if not isinstance(value, str):
        abort(404)
    lid = value.lower()
    if not list_id(lid):
        abort(404)

    data = _get(connection, revision, f"/contact_lists/{lid}")
    item = _list(data)
    if not isinstance(item, dict) or item.get("id") != lid:
        abort(502)
    if item.get("dynamic") is True:
        abort(422)

    return {"id": item["id"], "name": item["name"]}


def _contact(row, email):
    if not isinstance(row, dict):
        abort(502)

    cid = row.get("contact_id")
    if not list_id(cid):
        abort(502)

    ea = row.get("email_address")
    if not isinstance(ea, dict):
        abort(502)

    address = ea.get("address")
    if not isinstance(address, str):
        abort(502)
    if address.strip().casefold() != email.strip().casefold():
        abort(502)

    perm, conf = ea.get("permission_to_send"), ea.get("confirm_status")
    if not isinstance(perm, str):
        abort(502)
    if conf is not None and not isinstance(conf, str):
        abort(502)

    m = row.get("list_memberships", [])
    if not isinstance(m, list):
        abort(502)
    if len(m) > 50:
        abort(502)
    if not all(list_id(x) for x in m):
        abort(502)

    return {
        "id": cid.lower(),
        "memberships": {x.lower() for x in m},
        "eligible": perm in ("explicit", "implicit") and conf in (None, "off", "confirmed"),
        "pending": perm == "pending_confirmation" or conf == "pending"
    }


def read_contact(connection, revision, email):
    normalized = email_key(email)
    params = {"email": normalized, "status": "all", "include": "list_memberships", "limit": 2}
    data = _get(connection, revision, "/contacts", params=params)
    if not isinstance(data, dict):
        abort(502)
    contacts = data.get("contacts")
    if not isinstance(contacts, list) or len(contacts) > 1:
        abort(502)
    links = data.get("_links", {})
    if not isinstance(links, dict) or links.get("next"):
        abort(502)
    return _contact(contacts[0], normalized) if contacts else None


def _create_contact(connection, revision, list_id_value, email, first, last, before_send):
    body = {
        "email_address": {"address": email, "permission_to_send": "explicit"},
        "first_name": first,
        "last_name": last,
        "create_source": "Contact",
        "list_memberships": [list_id_value],
    }

    with connection.api(
        revision,
        "POST",
        API_BASE + "/contacts",
        json=body,
        before_send=before_send,
    ) as response:
        if response.status_code == 409:
            return None
        if response.status_code in (200, 201):
            return _contact(response_object(response), email)
        check_provider(response)


def _add_contact_to_list(connection, revision, contact_id, list_id_value, before_send):
    body = {"source": {"contact_ids": [contact_id]}, "list_ids": [list_id_value]}

    with connection.api(
        revision,
        "POST",
        API_BASE + "/activities/add_list_memberships",
        json=body,
        before_send=before_send,
    ) as response:
        if response.status_code in (200, 201, 202):
            data = response_object(response)
            activity_id = data.get("activity_id")
            if list_id(activity_id):
                return activity_id.lower()
            abort(502, "Constant Contact returned an invalid activity ID.")
        check_provider(response)


def send_registration(connection, revision, destination, email, first, last, *, before_send):
    normalized_email = email_key(email)
    if len(normalized_email) > 80:
        raise ValueError("Email address exceeds 80 characters.")

    if not list_id(destination):
        raise ValueError("Invalid Constant Contact list.")
    destination = destination.lower()

    if not isinstance(first, str) or not isinstance(last, str):
        raise ValueError("First and last name must be strings.")
    first = first.strip()
    last = last.strip()
    if (not first or not last or len(first) > 50 or len(last) > 50
            or not first.isprintable() or not last.isprintable()):
        raise ValueError("First and last name must be non-empty, printable, and at most 50 characters.")

    record = read_contact(connection, revision, normalized_email)
    if record is None:
        record = _create_contact(
            connection, revision, destination,
            normalized_email, first, last, before_send,
        )
        if record is None:
            record = read_contact(connection, revision, normalized_email)
            if record is None:
                abort(502)

    if record["pending"]:
        return {"state": "pending_confirmation"}
    if not record["eligible"]:
        return {"state": "skipped"}
    if destination in record["memberships"]:
        return {"state": "sent"}

    activity_id = _add_contact_to_list(
        connection, revision, record["id"], destination, before_send,
    )
    return {"state": "submitted", "activity_id": activity_id}
