import re

from flask import abort

from workspace_oauth import Config, Connection as OAuthConnection, check_provider

OAUTH = Config(
    key="campaign-monitor",
    name="Campaign Monitor",
    authorization_url="https://api.createsend.com/oauth",
    token_url="https://api.createsend.com/oauth/token",
    revoke_url="",
    scope="ManageLists,ImportSubscribers",
    authorization_parameters={"type": "web_server"},
    pkce=False,
    implicit_bearer=True,
    refresh_client_fields=False,
)

API_BASE = "https://api.createsend.com/api/v3.3"
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


class CampaignMonitorConnection(OAuthConnection):
    def __init__(self, tx, operator):
        super().__init__(OAUTH, tx, operator)

    def authorization_query(self, redirect_uri, state, verifier):
        query = super().authorization_query(redirect_uri, state, verifier)
        query.pop("response_type", None)
        return query


def identifier(value):
    return isinstance(value, str) and _HEX32.fullmatch(value) is not None


def _named_rows(connection, revision, path, id_key):
    with connection.api(revision, "GET", API_BASE + path) as response:
        check_provider(response)
        try:
            data = response.json()
        except ValueError:
            abort(502)

    if not isinstance(data, list):
        abort(502)

    rows, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            abort(502)

        raw_id = item.get(id_key)
        if not identifier(raw_id):
            abort(502)

        name = item.get("Name")
        if not isinstance(name, str) or len(name) > 255 or not name.isprintable():
            abort(502)

        cleaned = name.strip()
        if not cleaned:
            abort(502)

        normalized = raw_id.lower()
        if normalized in seen:
            abort(502)

        seen.add(normalized)
        rows.append({"id": normalized, "name": cleaned})

    return rows


def clients(connection, revision):
    return {"clients": _named_rows(connection, revision, "/clients.json", "ClientID")}


def lists(connection, revision, client_id):
    if not identifier(client_id):
        abort(422)

    client_id = client_id.lower()
    return {
        "lists": _named_rows(
            connection, revision, f"/clients/{client_id}/lists.json", "ListID"
        )
    }

def read_registration(connection, revision, list_id, email):
    from browser_account_security import email_key
    from workspace_oauth import response_object

    if not identifier(list_id):
        raise ValueError("invalid list id")

    normalized = email_key(email)
    url = API_BASE + f"/subscribers/{list_id.lower()}.json"

    with connection.api(revision, "GET", url, params={"email": normalized}) as response:
        if response.status_code == 400 and response_object(response).get("Code") == 203:
            return None
        check_provider(response)
        data = response_object(response)

    email_address = data.get("EmailAddress")
    if not isinstance(email_address, str) or email_address.strip().casefold() != normalized:
        abort(502)

    state = data.get("State")
    if state == "Active":
        return "sent"
    if state == "Unconfirmed":
        return "pending_confirmation"
    if state in ("Unsubscribed", "Bounced", "Deleted"):
        return "skipped"
    abort(502)


def _submission_state(response, normalized_email):
    from workspace_oauth import response_object

    status = response.status_code

    if status == 201:
        try:
            email = response.json()
        except ValueError:
            abort(502)

        if not isinstance(email, str) or email.casefold() != normalized_email:
            abort(502)

        return "submitted"

    if status == 400:
        code = response_object(response).get("Code")

        if code in (204, 205, 206, 207):
            return "skipped"
        if code == 208:
            return "pending_confirmation"

        abort(422)

    check_provider(response)

    if status == 200:
        abort(502)


def send_registration(connection, revision, list_id, email, first, last, *, before_send):
    from browser_account_security import email_key

    if not identifier(list_id):
        raise ValueError("invalid list id")

    if not isinstance(first, str) or not isinstance(last, str):
        raise ValueError("invalid name")

    first = first.strip()
    last = last.strip()

    for name in (first, last):
        if not name or not name.isprintable() or len(name) > 80:
            raise ValueError("invalid name")

    normalized = email_key(email)
    state = read_registration(connection, revision, list_id, normalized)
    if state is not None:
        return state

    url = API_BASE + f"/subscribers/{list_id.lower()}.json"
    with connection.api(
        revision,
        "POST",
        url,
        json={
            "EmailAddress": normalized,
            "Name": f"{first} {last}",
            "Resubscribe": False,
            "ConsentToTrack": "No",
        },
        before_send=before_send,
    ) as response:
        return _submission_state(response, normalized)

def contact_list(connection, revision, list_id):
    from workspace_oauth import response_object

    if not identifier(list_id):
        abort(422)

    list_id = list_id.lower()
    with connection.api(revision, "GET", API_BASE + f"/lists/{list_id}.json") as response:
        check_provider(response)
        payload = response_object(response)

    provider_id = payload.get("ListID")
    title = payload.get("Title")

    if not isinstance(provider_id, str) or provider_id.lower() != list_id:
        abort(502)

    if not isinstance(title, str):
        abort(502)

    title = title.strip()
    if not title or len(title) > 255 or not title.isprintable():
        abort(502)

    return {"id": list_id, "name": title}
