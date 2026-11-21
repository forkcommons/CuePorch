"""Bounded YouTube Data API adapter for a direct YouTube live destination."""

import dataclasses
import re
from flask import abort
from werkzeug.exceptions import TooManyRequests
from browser_account_security import secure_site
import team_accounts
from team_accounts import MANAGERS
from workspace_oauth import Connection, response_object
import youtube_upload

OAUTH = dataclasses.replace(youtube_upload.OAUTH, key="youtube-live", name="YouTube Live",
                            scope="https://www.googleapis.com/auth/youtube.force-ssl")
API = "https://www.googleapis.com/youtube/v3"
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")
CURSOR = re.compile(r"[A-Za-z0-9_=+/.-]{1,1024}")


class WorkspaceConnection(Connection):
    def __init__(self, tx, operator, access):
        super().__init__(OAUTH, tx, operator)
        self.access = access

    def save_authorization(self, con, tokens, context):
        member_id = context.get("member")     # None => raw installation bearer
        if member_id is not None:
            current = team_accounts.current(con, secure_site(context["origin"]))
            if (not current or current["id"] != member_id
                    or current["role"] not in MANAGERS
                    or not self.access(con, current)["outbound"]):
                abort(403, "Your workspace session cannot connect this YouTube channel.")
        return super().save_authorization(con, tokens, context)


def _fail(response, action):
    if response.status_code == 429:
        raise TooManyRequests(f"YouTube is rate-limiting {action}.", retry_after=youtube_upload.delay(response))
    if response.status_code in (401, 403):
        abort(403, f"YouTube refused this {action}. Check the connected account and its live permissions.")
    if 400 <= response.status_code < 500:
        abort(422, f"YouTube rejected this {action}.")
    abort(502, "The YouTube live service is unavailable.")


def _get(connection, revision, resource, params, action):
    with connection.api(revision, "GET", f"{API}/{resource}", params=params) as response:
        if response.status_code != 200:
            _fail(response, action)
        return response_object(response)


def channels(connection, revision, cursor=None):
    params = {"mine": "true", "part": "id,snippet", "maxResults": "50"}
    if cursor is not None:
        if not isinstance(cursor, str) or not CURSOR.fullmatch(cursor):
            abort(400, "Invalid channel list cursor.")
        params["pageToken"] = cursor
    result = _get(connection, revision, "channels", params, "channel listing")
    items = result.get("items") if isinstance(result, dict) else None
    nxt = result.get("nextPageToken") if isinstance(result, dict) else None
    if (not isinstance(items, list) or len(items) > 50
            or (nxt is not None and (not isinstance(nxt, str) or not CURSOR.fullmatch(nxt)))):
        abort(502, "YouTube returned a malformed channel list or cursor.")
    rows = []
    for item in items:
        snippet = item.get("snippet") if isinstance(item, dict) else None
        snippet = snippet if isinstance(snippet, dict) else None
        ident = item.get("id") if isinstance(item, dict) else None
        title = snippet.get("title") if snippet else None
        if (not isinstance(ident, str) or not IDENTIFIER.fullmatch(ident)
                or not isinstance(title, str) or not title or len(title) > 256):
            abort(502, "YouTube returned a malformed channel list.")
        rows.append({"id": ident, "title": title})
    return {"items": rows, "next": nxt}
