"""SCIM 2.0 provisioning for the configured workspace identity provider."""

import hmac
import json
import re
import secrets
from datetime import UTC, datetime
from time import time
from uuid import uuid4

from flask import abort, request
from werkzeug.exceptions import HTTPException

from browser_account_security import TOKEN, email_key, private, same_origin, secure_site, token_hash
from team_saml import canonical_https, configured, require_owner


SCHEMA = """
CREATE TABLE IF NOT EXISTS team_scim_token(
 slot INTEGER PRIMARY KEY CHECK(slot=1), issuer TEXT NOT NULL, token_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS team_scim_user(
 member TEXT PRIMARY KEY REFERENCES team_member(id), issuer TEXT NOT NULL, external_id TEXT NOT NULL,
 given_name TEXT NOT NULL, family_name TEXT NOT NULL, locale TEXT NOT NULL,
 created REAL NOT NULL, modified REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS team_scim_external_user ON team_scim_user(issuer,external_id)
 WHERE external_id<>'' AND deleted=0;
CREATE TABLE IF NOT EXISTS team_scim_group(
 team_group TEXT PRIMARY KEY REFERENCES team_group(id) ON DELETE CASCADE, issuer TEXT NOT NULL,
 external_id TEXT NOT NULL, created REAL NOT NULL, modified REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS team_scim_external_group ON team_scim_group(issuer,external_id) WHERE external_id<>'';
"""
PREFIX = "/scim/v2"
CORE = "urn:ietf:params:scim:schemas:core:2.0:"
MSG = "urn:ietf:params:scim:api:messages:2.0:"
FILTER = re.compile(r'(userName|displayName|externalId|id|emails\[type eq "work"\]\.value|emails\.value)\s+eq\s+("(?:[^"\\]|\\.)*")',
                    re.IGNORECASE)
MEMBER_PATH = re.compile(r'members\[value\s+eq\s+("(?:[^"\\]|\\.)*")\]', re.IGNORECASE)
SCALAR = {"username": "userName", "displayname": "displayName", "externalid": "externalId",
          "active": "active", "locale": "locale", "emails": "emails", "name": "name", "members": "members"}
USER_ROWS = """SELECT m.*,u.issuer,u.external_id,u.given_name,u.family_name,u.locale,u.created,u.modified
               FROM team_scim_user u JOIN team_member m ON m.id=u.member
               WHERE u.issuer=? AND u.deleted=0"""
GROUP_ROWS = """SELECT g.*,s.external_id,s.created,s.modified FROM team_scim_group s
                JOIN team_group g ON g.id=s.team_group WHERE s.issuer=?"""


class SCIMError(Exception):
    def __init__(self, status, detail, error_type=None):
        self.status, self.detail, self.error_type = status, detail, error_type


def scim_response(body, status=200):
    response = private(body, status)
    if status != 204:
        response.mimetype = "application/scim+json"
    return response


def value_error(detail):
    raise SCIMError(400, detail, "invalidValue")


def text(value, limit, *, required=False):
    if (not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value)
            or (required and not value.strip())):
        value_error("Enter a value of the expected type and length.")
    return value.strip()


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise SCIMError(400, "Send a SCIM JSON object.", "invalidSyntax")
    return value


def same_fields(value):
    return {str(key).lower(): item for key, item in value.items()}


def valid_email(value, domains):
    try:
        address = email_key(value)
    except HTTPException as error:
        value_error(error.description)
    if address.rsplit("@", 1)[-1] not in domains:
        value_error("The user must be on a trusted email domain.")
    return address


def active_state(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.casefold() in ("true", "false"):
        return value.casefold() == "true"
    value_error("active must be true or false.")


def timestamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def meta(origin, row, kind):
    return dict(resourceType=kind, location=f"{origin}{PREFIX}/{kind}s/{row['id']}",
                created=timestamp(row["created"]), lastModified=timestamp(row["modified"]))


def user_view(con, origin, row):
    groups = con.execute("""SELECT g.id,g.name FROM team_group g JOIN team_group_member m ON m.team_group=g.id
                            JOIN team_scim_group s ON s.team_group=g.id
                            WHERE m.member=? AND s.issuer=? ORDER BY g.name,g.id""", (row["id"], row["issuer"])).fetchall()
    result = dict(schemas=[CORE + "User"], id=row["id"], userName=row["email"], displayName=row["name"],
                  active=row["removed"] is None, name=dict(formatted=row["name"], givenName=row["given_name"],
                                                          familyName=row["family_name"]),
                  emails=[dict(value=row["email"], primary=True, type="work")],
                  groups=[{"value": group["id"], "display": group["name"],
                           "$ref": f"{origin}{PREFIX}/Groups/{group['id']}"} for group in groups],
                  meta=meta(origin, row, "User"))
    if row["external_id"]:
        result["externalId"] = row["external_id"]
    if row["locale"]:
        result["locale"] = row["locale"]
    return result


def group_view(con, origin, row):
    members = con.execute("""SELECT m.id,m.name FROM team_member m JOIN team_group_member g ON g.member=m.id
                             WHERE g.team_group=? AND m.removed IS NULL ORDER BY m.id""", (row["id"],)).fetchall()
    result = dict(schemas=[CORE + "Group"], id=row["id"], displayName=row["name"], meta=meta(origin, row, "Group"),
                  members=[{"value": member["id"], "display": member["name"],
                            "$ref": f"{origin}{PREFIX}/Users/{member['id']}"} for member in members])
    if row["external_id"]:
        result["externalId"] = row["external_id"]
    return result


def attributes(resource):
    include = set(value.strip() for value in request.args.get("attributes", "").lower().split(","))
    exclude = set(value.strip() for value in request.args.get("excludedAttributes", "").lower().split(","))
    required = {"schemas", "id"}
    return {key: value for key, value in resource.items()
            if key.lower() in required or ((include == {""} or key.lower() in include) and key.lower() not in exclude)}


def lookup(con, kind, issuer, uid):
    base, field = (USER_ROWS, "m.id") if kind == "Users" else (GROUP_ROWS, "g.id")
    row = con.execute(f"{base} AND {field}=?", (issuer, uid)).fetchone()
    if not row:
        raise SCIMError(404, "The resource was not found.")
    return row


def list_resources(con, origin, kind, issuer):
    base, id_field, filters, view = (
        (USER_ROWS, "m.id", {"username": "m.email", "emails[type eq \"work\"].value": "m.email",
                              "emails.value": "m.email", "externalid": "u.external_id", "id": "m.id"}, user_view)
        if kind == "Users" else
        (GROUP_ROWS, "g.id", {"displayname": "g.name", "externalid": "s.external_id", "id": "g.id"}, group_view)
    )
    params = [issuer]
    if condition := request.args.get("filter"):
        match = FILTER.fullmatch(condition.strip()) if len(condition) <= 1000 else None
        field = filters.get(match[1].lower()) if match else None
        if not field:
            raise SCIMError(400, "Use an equality filter on a supported attribute.", "invalidFilter")
        try:
            query = json.loads(match[2])
        except ValueError:
            raise SCIMError(400, "The filter value is invalid.", "invalidFilter") from None
        base += f" AND {field}=?"
        params.append(query.casefold() if field == "m.email" else query)
    try:
        start = max(1, int(request.args.get("startIndex", 1)))
        count = max(0, min(int(request.args.get("count", 100)), 100))
        if start > 2**31 - 1:
            raise ValueError
    except ValueError:
        value_error("Use an integer startIndex and count.")
    total = con.execute("SELECT count(*) FROM (" + base + ")", params).fetchone()[0]
    rows = con.execute(f"{base} ORDER BY {id_field} LIMIT ? OFFSET ?", (*params, count, start - 1)).fetchall()
    return dict(schemas=[MSG + "ListResponse"], totalResults=total, startIndex=start, itemsPerPage=len(rows),
                Resources=[attributes(view(con, origin, row)) for row in rows])


def user_values(values, settings, previous=None):
    incoming = same_fields(values)
    address = incoming.get("username", previous["userName"] if previous else None)
    email_rows = incoming.get("emails")
    if email_rows is not None:
        if not isinstance(email_rows, list) or not email_rows or not all(isinstance(item, dict) for item in email_rows):
            value_error("Send a work email address in emails.")
        work = next((item for item in email_rows if item.get("primary") is True or item.get("type") == "work"), email_rows[0])
        mailed = valid_email(work.get("value"), settings["domains"])
        if "username" in incoming and valid_email(address, settings["domains"]) != mailed:
            value_error("Use the same email for userName and the work email.")
        address = mailed
    address = valid_email(address, settings["domains"])
    previous_name = previous["name"] if previous else {}
    parts = incoming.get("name", {})
    if not isinstance(parts, dict):
        value_error("Send an object for name.")
    parts = same_fields(parts)
    given = text(parts.get("givenname", previous_name.get("givenName", "")), 120)
    family = text(parts.get("familyname", previous_name.get("familyName", "")), 120)
    formatted = text(parts.get("formatted", ""), 120)
    combined = " ".join(part for part in (given, family) if part)
    default_name = formatted or (combined[:120] if parts and combined else None) or (previous or {}).get("displayName") or address[:120]
    display = text(incoming.get("displayname", default_name), 120, required=True)
    external = text(incoming.get("externalid", (previous or {}).get("externalId", "")), 256)
    locale = text(incoming.get("locale", (previous or {}).get("locale", "")), 32)
    active = active_state(incoming.get("active", (previous or {}).get("active", True)))
    return address, display, given, family, locale, external, active


def save_user(con, origin, settings, values, revoke, note, current=None):
    issuer = settings["entity_id"]
    previous = user_view(con, origin, current) if current else None
    address, name, given, family, locale, external, active = user_values(values, settings, previous)
    uid = current["id"] if current else str(uuid4())
    if con.execute("SELECT 1 FROM team_member WHERE email=? AND id<>? AND removed IS NULL", (address, uid)).fetchone():
        raise SCIMError(409, "The email is already assigned to a workspace member.", "uniqueness")
    if external and con.execute("""SELECT 1 FROM team_scim_user WHERE issuer=? AND external_id=?
                                 AND member<>? AND deleted=0""", (issuer, external, uid)).fetchone():
        raise SCIMError(409, "The external ID is already assigned to a workspace member.", "uniqueness")
    now = time()
    if current:
        if current["role"] == "owner":
            raise SCIMError(409, "The connection cannot manage the workspace Owner.")
        removed = None if active else current["removed"] or now
        con.execute("UPDATE team_member SET email=?,name=?,removed=? WHERE id=?", (address, name, removed, uid))
        con.execute("""UPDATE team_scim_user SET external_id=?,given_name=?,family_name=?,locale=?,modified=?
                       WHERE member=?""", (external, given, family, locale, now, uid))
        if not active and current["removed"] is None:
            revoke(con, uid)
        elif current["email"] != address:
            con.execute("DELETE FROM team_session WHERE member=?", (uid,))
    else:
        con.execute("INSERT INTO team_member VALUES(?,?,?,?,?,?,?)",
                    (uid, address, name, settings["default_role"], "", now, None if active else now))
        con.execute("INSERT INTO team_scim_user VALUES(?,?,?,?,?,?,?,?,0)",
                    (uid, issuer, external, given, family, locale, now, now))
        con.execute("DELETE FROM team_invitation WHERE email=?", (address,))
    action = "scim_provisioned" if not current else "scim_deactivated" if not active else "scim_updated"
    note(con, "scim", action, uid)
    return lookup(con, "Users", issuer, uid)


def member_ids(members):
    if not isinstance(members, list) or len(members) > 10000 or not all(isinstance(item, dict) for item in members):
        value_error("Send the group members as a list of ID objects.")
    return {text(member.get("value"), 128, required=True) for member in members}


def save_group(con, issuer, values, note, current=None):
    incoming = same_fields(values)
    name = text(incoming.get("displayname", current["name"] if current else ""), 120, required=True)
    external = text(incoming.get("externalid", current["external_id"] if current else ""), 256)
    uid = current["id"] if current else str(uuid4())
    if (con.execute("SELECT 1 FROM team_group WHERE name=? COLLATE NOCASE AND id<>?", (name, uid)).fetchone()
            or (external and con.execute("""SELECT 1 FROM team_scim_group WHERE issuer=? AND external_id=?
                                           AND team_group<>?""", (issuer, external, uid)).fetchone())):
        raise SCIMError(409, "A group already uses that name or external ID.", "uniqueness")
    refs = None
    if "members" in incoming:
        refs = member_ids(incoming["members"])
        eligible = {row[0] for row in con.execute("SELECT id FROM (" + USER_ROWS + ") WHERE removed IS NULL", (issuer,))}
        if refs - eligible:
            value_error("Each group member must be an active user managed by this connection.")
    now, revision = time(), secrets.token_hex(16)
    if current:
        con.execute("UPDATE team_group SET name=?,revision=? WHERE id=?", (name, revision, uid))
        con.execute("UPDATE team_scim_group SET external_id=?,modified=? WHERE team_group=?", (external, now, uid))
    else:
        con.execute("INSERT INTO team_group VALUES(?,?,?)", (uid, name, revision))
        con.execute("INSERT INTO team_scim_group VALUES(?,?,?,?,?)", (uid, issuer, external, now, now))
    if refs is not None:
        con.execute("DELETE FROM team_group_member WHERE team_group=?", (uid,))
        con.executemany("INSERT INTO team_group_member VALUES(?,?)", [(uid, member) for member in sorted(refs)])
    note(con, "scim", "scim_group_updated" if current else "scim_group_created", uid)
    return lookup(con, "Groups", issuer, uid)


def patch_values(resource, payload, kind):
    operations = same_fields(payload).get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100 or not all(isinstance(op, dict) for op in operations):
        raise SCIMError(400, "Supply the SCIM patch operations.", "invalidSyntax")
    updates = {}
    for item in operations:
        op = same_fields(item)
        operation, path, value = op.get("op"), op.get("path", ""), op.get("value")
        if not isinstance(operation, str) or not isinstance(path, str) or len(path) > 256:
            raise SCIMError(400, "Supply a valid patch operation and path.", "invalidPath")
        action, field = operation.lower(), path.lower()
        if action not in ("add", "replace", "remove"):
            raise SCIMError(400, "Unsupported patch operation.", "invalidSyntax")
        if not field and action in ("add", "replace") and isinstance(value, dict):
            for key, property_value in same_fields(value).items():
                if key in SCALAR and (key != "members" or kind == "Groups"):
                    updates[SCALAR[key]] = property_value
            continue
        if kind == "Groups" and field.startswith("members"):
            previous = updates.get("members", resource["members"])
            members = member_ids(previous)
            if field == "members" and action in ("add", "replace"):
                replacement = member_ids(value)
                members = (members | replacement) if action == "add" else replacement
            elif field == "members" and action == "remove":
                members = members - member_ids(value) if value is not None else set()
            elif action == "remove" and (match := MEMBER_PATH.fullmatch(path)):
                try:
                    members.discard(json.loads(match[1]))
                except ValueError:
                    raise SCIMError(400, "The group member filter is invalid.", "invalidPath") from None
            else:
                raise SCIMError(400, "Unsupported group member operation.", "invalidPath")
            updates["members"] = [{"value": uid} for uid in sorted(members)]
            continue
        if kind == "Users" and field in ("name.givenname", "name.familyname", "name.formatted"):
            subkey = dict(givenname="givenName", familyname="familyName", formatted="formatted")[field[5:]]
            update = updates.setdefault("name", {})
            if not isinstance(update, dict):
                value_error("Send an object for name.")
            update[subkey] = "" if action == "remove" else value
            continue
        if kind == "Users" and field in ('emails[type eq "work"].value', 'emails.value') and action != "remove":
            updates["emails"] = [{"value": value, "type": "work"}]
            continue
        allowed = ("displayname", "externalid") if kind == "Groups" else ("username", "displayname", "externalid", "active", "locale", "name", "emails")
        if field not in allowed or (action == "remove" and field not in ("externalid", "locale")):
            raise SCIMError(400, "Unsupported patch attribute.", "invalidPath")
        updates[SCALAR[field]] = "" if action == "remove" else value
    return updates


def scim_fault(error):
    body = dict(schemas=[MSG + "Error"], status=str(error.status), detail=error.detail)
    if error.error_type:
        body["scimType"] = error.error_type
    response = scim_response(body, error.status)
    if error.status == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="CuePorch SCIM"'
    return response


def register(app, tx, origin, operator, ownership, mutation, note, revoke, delete_group):
    @app.route("/api/team/identity/provisioning", methods=("GET", "POST", "DELETE"))
    def provisioning_settings():
        secure = secure_site(origin)
        same_origin(secure)
        if request.method not in ("GET", "HEAD"):
            mutation(secure)
        with tx() as con:
            actor = require_owner(con, operator, ownership)
            _, settings = configured(con)
            enabled = settings["enabled"]
            issuer = settings["entity_id"]
            active = bool(enabled and con.execute("SELECT 1 FROM team_scim_token WHERE issuer=?", (issuer,)).fetchone())
            payload = dict(available=enabled, active=active, endpoint=origin + PREFIX if origin.startswith("https://") else "")
            if request.method == "POST":
                if not enabled:
                    abort(409, "Enable team single sign-on before connecting a provisioning service.")
                token = secrets.token_urlsafe(32)
                con.execute("INSERT OR REPLACE INTO team_scim_token VALUES(1,?,?)", (issuer, token_hash(token)))
                note(con, actor, "scim_token_rotated" if active else "scim_enabled", "workspace")
                payload.update(active=True, token=token)
            elif request.method == "DELETE":
                con.execute("DELETE FROM team_scim_token")
                if active:
                    note(con, actor, "scim_disabled", "workspace")
                payload["active"] = False
        return private(payload, 201 if request.method == "POST" else 200)

    def authorization(con):
        if not canonical_https(origin):
            raise SCIMError(404, "Provisioning is unavailable on this address.")
        _, settings = configured(con)
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        registered = con.execute("SELECT token_hash FROM team_scim_token WHERE issuer=?", (settings["entity_id"],)).fetchone()
        if (not settings["enabled"] or not TOKEN.fullmatch(supplied) or not registered
                or not hmac.compare_digest(token_hash(supplied), registered["token_hash"])):
            raise SCIMError(401, "Use the current workspace provisioning token.")
        return settings

    @app.get(PREFIX + "/ServiceProviderConfig")
    def service_provider_configuration():
        try:
            with tx() as con:
                authorization(con)
            return scim_response(dict(
                schemas=[CORE + "ServiceProviderConfig"],
                patch=dict(supported=True), bulk=dict(supported=False, maxOperations=0, maxPayloadSize=0),
                filter=dict(supported=True, maxResults=100), changePassword=dict(supported=False),
                sort=dict(supported=False), etag=dict(supported=False),
                authenticationSchemes=[dict(name="Bearer token", type="oauthbearertoken",
                                            description="Workspace provisioning bearer token", primary=True)],
                meta=dict(resourceType="ServiceProviderConfig", location=origin + PREFIX + "/ServiceProviderConfig"),
            ))
        except SCIMError as error:
            return scim_fault(error)

    @app.route(PREFIX + "/<kind>", methods=("GET", "POST"))
    @app.route(PREFIX + "/<kind>/<uid>", methods=("GET", "PUT", "PATCH", "DELETE"))
    def scim_resource(kind, uid=None):
        kinds = {"users": "Users", "groups": "Groups"}
        kind = kinds.get(kind.lower())
        if not kind:
            return scim_fault(SCIMError(404, "The resource type was not found."))
        method = request.method
        request.max_content_length = 1024 * 1024
        try:
            with tx() as con:
                settings = authorization(con)
                issuer = settings["entity_id"]
                current = lookup(con, kind, issuer, uid) if uid else None
                view = user_view if kind == "Users" else group_view
                if method in ("GET", "HEAD"):
                    result = attributes(view(con, origin, current)) if current else list_resources(con, origin, kind, issuer)
                    return scim_response(result)
                if method == "DELETE":
                    if kind == "Users":
                        if current["role"] == "owner":
                            raise SCIMError(409, "The connection cannot remove the workspace Owner.")
                        con.execute("UPDATE team_member SET removed=coalesce(removed,?) WHERE id=?", (time(), uid))
                        con.execute("UPDATE team_scim_user SET deleted=1,modified=? WHERE member=?", (time(), uid))
                        revoke(con, uid)
                    else:
                        delete_group(con, uid)
                    note(con, "scim", "scim_user_deleted" if kind == "Users" else "scim_group_deleted", uid)
                    return scim_response("", 204)
                payload = body()
                if method == "PATCH":
                    payload = patch_values(view(con, origin, current), payload, kind)
                if kind == "Users":
                    saved = save_user(con, origin, settings, payload, revoke, note, current)
                else:
                    saved = save_group(con, issuer, payload, note, current)
                resource = view(con, origin, saved)
            if kind == "Groups" and method == "PATCH":
                return scim_response("", 204)
            response = scim_response(attributes(resource), 201 if method == "POST" else 200)
            if method == "POST":
                response.headers["Location"] = resource["meta"]["location"]
            return response
        except SCIMError as error:
            return scim_fault(error)
        except HTTPException as error:
            return scim_fault(SCIMError(error.code, error.description))
