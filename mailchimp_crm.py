import re

from flask import abort

from workspace_oauth import Config, check_provider, response_object

OAUTH = Config(
    "mailchimp",
    "Mailchimp",
    "https://login.mailchimp.com/oauth2/authorize",
    "https://login.mailchimp.com/oauth2/token",
    "",
    "",
    refresh_optional=True,
    implicit_bearer=True,
    nonexpiring_access=True,
)

API = "https://api.mailchimp.com/3.0"
_AUDIENCE_ID = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def audience_id(value):
    """Return True only for safe Mailchimp audience (list) ids."""
    return isinstance(value, str) and bool(_AUDIENCE_ID.match(value))


def _is_int(value, minimum):
    return type(value) is int and value >= minimum


def get(connection, revision, path, **params):
    with connection.api(revision, "GET", API + path, params=params) as resp:
        if resp.status_code == 404:
            abort(404, "Mailchimp audience not found")
        check_provider(resp)
        return response_object(resp)


def _audience(row):
    if not isinstance(row, dict):
        abort(502, "Invalid Mailchimp audience")
    list_id = row.get("id")
    name = row.get("name")
    if not audience_id(list_id):
        abort(502, "Invalid Mailchimp audience")
    if not isinstance(name, str):
        abort(502, "Invalid Mailchimp audience")
    name = name.strip()
    if not name or len(name) > 300 or not name.isprintable():
        abort(502, "Invalid Mailchimp audience")
    return {"id": list_id, "name": name}


def audiences(connection, revision, offset=0):
    if not _is_int(offset, 0):
        abort(400, "offset must be a nonnegative integer")
    count = 50
    data = get(
        connection,
        revision,
        "/lists",
        offset=offset,
        count=count,
        fields="lists.id,lists.name,total_items",
    )
    lists = data.get("lists")
    if not isinstance(lists, list) or len(lists) > count:
        abort(502, "Invalid Mailchimp response")
    rows = []
    seen = set()
    for row in lists:
        item = _audience(row)
        if item["id"] in seen:
            abort(502, "Duplicate Mailchimp audience")
        seen.add(item["id"])
        rows.append(item)
    total = data.get("total_items")
    if total is not None and not _is_int(total, 0):
        abort(502, "Invalid Mailchimp response")
    next_offset = None
    if rows:
        consumed = offset + len(rows)
        if total is not None:
            if total > consumed:
                next_offset = consumed
        elif len(rows) == count:
            next_offset = consumed
    return {"audiences": rows, "next_offset": next_offset}


def audience(connection, revision, list_id):
    if not audience_id(list_id):
        abort(400, "Invalid audience id")
    data = get(
        connection,
        revision,
        "/lists/" + list_id,
        fields="id,name,double_optin,marketing_permissions",
    )
    item = _audience(data)
    if item["id"] != list_id:
        abort(502, "Invalid Mailchimp audience")
    double_optin = data.get("double_optin")
    marketing_permissions = data.get("marketing_permissions")
    if not isinstance(double_optin, bool) or not isinstance(
        marketing_permissions, bool
    ):
        abort(502, "Invalid Mailchimp audience")
    return dict(
        item,
        double_optin=double_optin,
        marketing_permissions=marketing_permissions,
    )


def _merge_field(row):
    if not isinstance(row, dict):
        abort(502)
    tag = row.get("tag")
    name = row.get("name")
    field_type = row.get("type")
    required = row.get("required")
    if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", tag):
        abort(502)
    if (
        not isinstance(name, str)
        or not name.strip()
        or not name.isprintable()
        or len(name) > 300
    ):
        abort(502)
    if not isinstance(field_type, str):
        abort(502)
    field_type = field_type.lower()
    if field_type not in {
        "text",
        "number",
        "address",
        "date",
        "birthday",
        "radio",
        "dropdown",
        "zip",
        "phone",
        "url",
        "imageurl",
    }:
        abort(502)
    if not isinstance(required, bool):
        abort(502)
    choices = []
    date_format = None
    options = row.get("options")
    if options is None:
        options = {}
    elif not isinstance(options, dict):
        abort(502)
    if "choices" in options:
        choices = options["choices"]
        if not isinstance(choices, list) or not all(
            isinstance(choice, str) for choice in choices
        ):
            abort(502)
    if "date_format" in options:
        date_format = options["date_format"]
        if not isinstance(date_format, str):
            abort(502)
    return {
        "tag": tag,
        "name": name,
        "type": field_type,
        "required": required,
        "choices": choices,
        "date_format": date_format,
    }


def merge_fields(connection, revision, list_id, offset=0):
    if not _is_int(offset, 0):
        abort(400)
    if not audience_id(list_id):
        abort(400)
    path = f"/lists/{list_id}/merge-fields"
    data = get(
        connection,
        revision,
        path,
        count=100,
        offset=offset,
        fields="list_id,total_items,merge_fields.tag,merge_fields.name,merge_fields.type,merge_fields.required,merge_fields.options",
    )
    if "list_id" in data and data["list_id"] != list_id:
        abort(502)
    total_items = data.get("total_items")
    if total_items is not None and not _is_int(total_items, 0):
        abort(502)
    rows = data.get("merge_fields")
    if not isinstance(rows, list):
        abort(502)
    if len(rows) > 100:
        abort(502)
    if not rows:
        return {"fields": [], "next_offset": None}
    fields = []
    tags = set()
    for row in rows:
        field = _merge_field(row)
        if field["tag"] in tags:
            abort(502)
        tags.add(field["tag"])
        fields.append(field)
    consumed = offset + len(rows)
    if total_items is not None:
        next_offset = consumed if consumed < total_items else None
    else:
        next_offset = consumed if len(rows) == 100 else None
    return {"fields": fields, "next_offset": next_offset}
