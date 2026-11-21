import re

from flask import abort

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
