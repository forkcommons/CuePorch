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
PROFILE_OAUTH = replace(
    OAUTH,
    key="facebook-profile-live",
    name="Facebook Profile Live",
    scope="publish_video",
)
INGEST_SERVER = "rtmps://live-api-s.facebook.com:443/rtmp"
STREAM_PATH = re.compile(r"/rtmp/([A-Za-z0-9_-]{8,256})")
PROFILE_PRIVACY = frozenset(("EVERYONE", "ALL_FRIENDS", "SELF"))


class FacebookLiveUnconfirmed(Exception):
    """Facebook may have created the broadcast without confirming it."""


def normalize_profile_privacy(value):
    if value is None:
        return None
    if not isinstance(value, str) or value not in PROFILE_PRIVACY:
        abort(422, "Unsupported Facebook profile privacy value.")
    return value


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

    def create_live(self, owner, token, title, opens, before_send, *, live_now=False, privacy=None):
        before_send()
        payload = {"title": title, "status": "LIVE_NOW" if live_now else "SCHEDULED_UNPUBLISHED",
                   "stop_on_delete_stream": True}
        if not live_now:
            payload["event_params"] = {"start_time": opens}
        if privacy is not None:
            payload["privacy"] = {"value": privacy}
        try:
            data = self.graph_request(
                "POST", f"{facebook_upload.GRAPH}/{owner}/live_videos", token,
                json=payload,
            )
            ident = facebook_upload.numeric(data.get("id"))
            key = parse_ingest(data.get("secure_stream_url"))
        except HTTPException as error:
            if error.code == 502:
                raise FacebookLiveUnconfirmed from error
            raise
        return {"id": ident, "server": INGEST_SERVER, "key": key}

    def create_page_live(self, revision, page, title, opens, before_send, *, live_now=False):
        with self.tx() as con:
            access = self.tokens(self.authorize(con, revision))["pages"].get(page)
        if access is None:
            abort(403, "The Facebook Page is no longer connected.")
        return self.create_live(page, access["token"], title, opens, before_send, live_now=live_now)


class ProfileLiveConnection(PageLiveConnection):
    def __init__(self, tx, operator, sharing_access):
        Connection.__init__(self, PROFILE_OAUTH, tx, operator)
        self.sharing_access = sharing_access

    def exchange(self, fields, previous_refresh=None):
        short = self.exchange_token({key: fields[key] for key in ("code", "redirect_uri")})
        user = self.exchange_token(dict(grant_type="fb_exchange_token", fb_exchange_token=short))
        data = self.graph_request("GET", facebook_upload.GRAPH + "/me", user, {"fields": "id,name"})
        ident = facebook_upload.numeric(data.get("id"))
        name = data.get("name")
        if not isinstance(name, str) or not name:
            abort(502, "Facebook returned an invalid profile name.")
        return {"profile": {"id": ident, "name": name, "token": user}}

    def public_profiles(self, con):
        row = self.connection(con)
        profile = self.tokens(row).get("profile") if row else None
        if not isinstance(profile, dict):
            return {"items": []}
        return {"items": [{"id": profile.get("id"), "name": profile.get("name")}]}

    def create_profile_live(self, revision, owner, title, opens, before_send, *, live_now=False, privacy=None):
        with self.tx() as con:
            profile = self.tokens(self.authorize(con, revision)).get("profile")
        owner = facebook_upload.numeric(owner)
        if not isinstance(profile, dict) or profile.get("id") != owner:
            abort(403, "The Facebook Profile is no longer connected.")
        return self.create_live(owner, profile["token"], title, opens, before_send, live_now=live_now,
                                privacy=normalize_profile_privacy(privacy))
