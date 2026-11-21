import hashlib
import math
import re
from urllib.parse import urlsplit

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


_ALLOWED = {
    "subscribed",
    "unsubscribed",
    "pending",
    "cleaned",
    "transactional",
    "archived",
}
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")


def _member_identity(list_id, email):
    if not isinstance(email, str):
        abort(400)
    normalized = email.strip().lower()
    if (
        len(normalized) > 254
        or not normalized.isprintable()
        or not _EMAIL.fullmatch(normalized)
    ):
        abort(400)
    if not audience_id(list_id):
        abort(400)
    subscriber_hash = hashlib.md5(
        normalized.encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return normalized, subscriber_hash, f"/lists/{list_id}/members/{subscriber_hash}"


def _member_status(row, list_id, normalized, subscriber_hash):
    provider_email = row.get("email_address")
    if not isinstance(provider_email, str):
        abort(502)
    status = row.get("status")
    if not isinstance(status, str) or status not in _ALLOWED:
        abort(502)
    if (
        row.get("list_id") != list_id
        or provider_email.strip().lower() != normalized
        or row.get("id") != subscriber_hash
    ):
        abort(502)
    return {"id": subscriber_hash, "status": status}


def read_member(connection, revision, list_id, email):
    normalized, subscriber_hash, path = _member_identity(list_id, email)
    with connection.api(
        revision,
        "GET",
        API + path,
        params={"fields": "id,email_address,list_id,status"},
    ) as response:
        if response.status_code == 404:
            return None
        check_provider(response)
        return _member_status(
            response_object(response), list_id, normalized, subscriber_hash
        )


def save_member(connection, revision, list_id, email, merge_values, *, before_write):
    normalized, subscriber_hash, path = _member_identity(list_id, email)
    policy = audience(connection, revision, list_id)
    if policy["marketing_permissions"] is True:
        abort(409, "Mailchimp audience requires a marketing permission mapping.")

    existing = read_member(connection, revision, list_id, normalized)
    if existing is not None and existing["status"] != "subscribed":
        return {"result": "suppressed", "status": existing["status"]}

    if existing is not None and not merge_values:
        return {"result": "delivered", "status": "subscribed"}

    payload = {}
    if merge_values:
        payload["merge_fields"] = merge_values

    if existing is None:
        method = "PUT"
        payload["email_address"] = normalized
        payload["status_if_new"] = "pending" if policy["double_optin"] else "subscribed"
    else:
        method = "PATCH"

    before_write()
    with connection.api(revision, method, API + path, json=payload) as resp:
        if method == "PATCH" and resp.status_code == 404:
            abort(409)
        check_provider(resp)
        status = _member_status(
            response_object(resp), list_id, normalized, subscriber_hash
        )["status"]

    if status == "subscribed" or (
        existing is None and policy["double_optin"] and status == "pending"
    ):
        return {"result": "delivered", "status": status}
    return {"result": "suppressed", "status": status}


def audience_schema(connection, revision, list_id):
    seen, out, offset = set(), [], 0
    for _ in range(20):
        page = merge_fields(connection, revision, list_id, offset)
        nxt = page["next_offset"]
        for f in page["fields"]:
            tag = f["tag"]
            if tag in seen:
                abort(502)
            seen.add(tag)
            out.append(f)
        if nxt is None:
            return out
        if isinstance(nxt, bool) or not isinstance(nxt, int) or nxt <= offset:
            abort(502)
        offset = nxt
    abort(502)


def validate_mapping(form, mapping, fields):
    if not isinstance(mapping, dict) or len(mapping) > 20:
        raise ValueError("invalid mapping")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise ValueError("invalid mapping")

    src = {}
    for f in form["fields"]:
        t = f["type"]
        if t not in ("text", "select"):
            continue
        src[f["id"]] = (
            t,
            bool(f.get("required")),
            list(f.get("options") or []) if t == "select" else None,
        )
    for name in ("first_name", "last_name"):
        s = form.get(name, "off")
        if s in ("optional", "required"):
            src[name] = ("text", s == "required", None)

    dest = {f["tag"]: f for f in fields}
    if any(tag not in dest for tag in mapping.values()):
        raise ValueError("invalid mapping")

    supported = {"text", "radio", "dropdown", "number", "zip", "phone", "url"}
    used, out = set(), {}
    for sid, tag in mapping.items():
        if sid not in src or tag in used:
            raise ValueError("invalid mapping")
        stype, sreq, sopts = src[sid]
        df = dest[tag]
        dtype = df["type"]
        req = bool(df.get("required"))
        if dtype not in supported:
            raise ValueError("invalid mapping")
        choices = list(df.get("choices") or [])
        if dtype in ("radio", "dropdown"):
            if stype != "select":
                raise ValueError("invalid mapping")
            if not set(sopts).issubset(set(choices)):
                raise ValueError("invalid mapping")
            oc = choices
        elif dtype == "text":
            if stype not in ("text", "select"):
                raise ValueError("invalid mapping")
            oc = None
        else:
            if stype != "text":
                raise ValueError("invalid mapping")
            oc = None
        if req and not sreq:
            raise ValueError("invalid mapping")
        used.add(tag)
        out[sid] = {"tag": tag, "type": dtype, "choices": oc, "required": req}

    for tag, df in dest.items():
        if bool(df.get("required")) and tag not in used:
            raise ValueError("invalid mapping")
    return out


def mapped_values(answers, mapping):
    MAX = 255
    out = {}
    for sid, spec in mapping.items():
        raw = answers.get(sid)
        if raw is None or raw == "":
            if spec["required"]:
                raise ValueError("missing required value")
            continue
        if not isinstance(raw, str) or not raw.isprintable() or len(raw) > MAX:
            raise ValueError("invalid value")

        t = spec["type"]
        if t in ("radio", "dropdown"):
            if raw not in (spec.get("choices") or []):
                raise ValueError("invalid choice")
            out[spec["tag"]] = raw
        elif t == "number":
            try:
                v = float(raw)
            except ValueError:
                raise ValueError("invalid number")
            if not math.isfinite(v):
                raise ValueError("invalid number")
            out[spec["tag"]] = v
        elif t == "url":
            try:
                u = urlsplit(raw)
            except ValueError:
                raise ValueError("invalid url")
            if (
                u.scheme not in ("http", "https")
                or not u.hostname
                or u.username is not None
                or u.password is not None
            ):
                raise ValueError("invalid url")
            out[spec["tag"]] = raw
        else:
            out[spec["tag"]] = raw
    return out
