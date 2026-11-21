import re
from urllib.parse import parse_qsl, urlsplit

from flask import abort
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
