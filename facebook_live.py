"""Separate Meta authorization for Facebook Live destinations."""

from dataclasses import replace
import re
from urllib.parse import urlsplit

from flask import abort
from werkzeug.exceptions import HTTPException

import facebook_upload
import team_accounts
from workspace_oauth import Connection


OAUTH = replace(
    facebook_upload.OAUTH,
    key="facebook-live",
    name="Facebook Live",
    scope=facebook_upload.OAUTH.scope + " publish_video",
)
INGEST_SERVER = "rtmps://live-api-s.facebook.com:443/rtmp"
STREAM_PATH = re.compile(r"/rtmp/([A-Za-z0-9_-]{8,256})")


class FacebookLiveUnconfirmed(Exception):
    """Facebook may have created the broadcast without confirming it."""


def parse_ingest(value):
    if not isinstance(value, str) or len(value) > 512 or not value.isascii() or not value.isprintable():
        raise FacebookLiveUnconfirmed
    try:
        address = urlsplit(value)
        port = address.port
    except ValueError as error:
        raise FacebookLiveUnconfirmed from error
    stream = STREAM_PATH.fullmatch(address.path)
    if (address.scheme != "rtmps" or address.hostname != "live-api-s.facebook.com"
            or port not in (None, 443) or address.username is not None or address.password is not None
            or address.query or address.fragment or not stream):
        raise FacebookLiveUnconfirmed
    return stream[1]


class PageLiveConnection(facebook_upload.PageConnection):
    def __init__(self, tx, operator, sharing_access):
        Connection.__init__(self, OAUTH, tx, operator)
        self.sharing_access = sharing_access

    def save_authorization(self, con, tokens, context):
        team_accounts.require_live_connection_manager(con, context, self.sharing_access)
        return super().save_authorization(con, tokens, context)

    def exchange_token(self, params):
        token = super().exchange_token(params)
        if params.get("grant_type") == "fb_exchange_token":
            result = self.graph_request("GET", facebook_upload.GRAPH + "/me/permissions", token, {"limit": 100})
            rows = result.get("data")
            if not isinstance(rows, list):
                abort(502, "Facebook returned an invalid permissions list.")
            if not any(isinstance(row, dict) and row.get("permission") == "publish_video"
                       and row.get("status") == "granted" for row in rows):
                abort(403, "Grant Facebook Live publishing access before connecting.")
        return token

    def public_pages(self, con):
        return {"items": [{"id": key, "name": row["name"]} for key, row in self.pages(con).items()]}

    def create_page_live(self, revision, page, title, opens, before_send):
        with self.tx() as con:
            access = self.tokens(self.authorize(con, revision))["pages"].get(page)
        if access is None:
            abort(403, "The Facebook Page is no longer connected.")
        before_send()
        try:
            data = self.graph_request(
                "POST", f"{facebook_upload.GRAPH}/{page}/live_videos", access["token"],
                json={"title": title, "status": "SCHEDULED_UNPUBLISHED", "event_params": {"start_time": opens},
                      "stop_on_delete_stream": True},
            )
            ident = facebook_upload.numeric(data.get("id"))
            key = parse_ingest(data.get("secure_stream_url"))
        except HTTPException as error:
            if error.code == 502:
                raise FacebookLiveUnconfirmed from error
            raise
        return {"id": ident, "server": INGEST_SERVER, "key": key}
