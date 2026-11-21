"""Separate Meta authorization for Facebook Live destinations."""

from dataclasses import replace

from flask import abort

import facebook_upload
import team_accounts
from workspace_oauth import Connection


OAUTH = replace(
    facebook_upload.OAUTH,
    key="facebook-live",
    name="Facebook Live",
    scope=facebook_upload.OAUTH.scope + " publish_video",
)


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
