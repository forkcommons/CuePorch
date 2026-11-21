"""Workspace LinkedIn Live grant and its configured Company Page."""

from dataclasses import replace
import os
import re

from flask import abort
from werkzeug.exceptions import HTTPException

import linkedin_upload
import team_accounts
from workspace_oauth import Connection, response_object


OAUTH = replace(linkedin_upload.OAUTH, key="linkedin-live", name="LinkedIn Live",
                scope="r_organization_live w_organization_live r_organization_admin")
API = "https://api.linkedin.com/rest"
V2_API = "https://api.linkedin.com/v2"
V2_HEADERS = {"X-Restli-Protocol-Version": "2.0.0"}
LIVE_VIDEO_ID = re.compile(r"[0-9]{1,20}")
ANNOUNCEMENT_URN = re.compile(r"urn:li:ugcPost:[0-9]{1,30}")
PAGE_SIZE = 10
PAGE_LIMIT = 20


class LinkedInWriteUnconfirmed(Exception):
    """LinkedIn may have applied a write without confirming its identifier."""


class LiveConnection(Connection):
    def __init__(self, tx, operator, sharing_access):
        super().__init__(OAUTH, tx, operator)
        self.sharing_access = sharing_access
        self.company = os.environ.get("CUEPORCH_LINKEDIN_LIVE_COMPANY_ID", "").strip()
        self.configured = self.configured and bool(linkedin_upload.COMPANY.fullmatch(self.company))

    def save_authorization(self, con, tokens, context):
        team_accounts.require_live_connection_manager(con, context, self.sharing_access)
        return super().save_authorization(con, tokens, context)

    def authorized_company(self, revision):
        if not self.configured:
            abort(409, "Configure a LinkedIn Live Company Page first.")
        urn = f"urn:li:organization:{self.company}"
        for start in range(0, PAGE_SIZE * PAGE_LIMIT, PAGE_SIZE):
            params = {"q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED",
                      "count": PAGE_SIZE, "start": start}
            with self.api(revision, "GET", f"{API}/organizationAcls", params=params,
                          headers=linkedin_upload.HEADERS) as response:
                if response.status_code != 200:
                    linkedin_upload.denied(response)
                data = response_object(response)
            entries, paging = data.get("elements"), data.get("paging", {})
            links = paging.get("links", []) if isinstance(paging, dict) else None
            if not isinstance(entries, list) or len(entries) > PAGE_SIZE or not isinstance(links, list):
                abort(502, "LinkedIn returned an invalid Page permissions list.")
            for entry in entries:
                if not isinstance(entry, dict):
                    abort(502, "LinkedIn returned an invalid Page permission.")
                company = entry.get("organization", entry.get("organizationTarget"))
                if (company == urn and entry.get("organizationTarget", company) == urn
                        and entry.get("role") == "ADMINISTRATOR" and entry.get("state") == "APPROVED"):
                    return {"id": self.company, "urn": urn}
            if not any(isinstance(link, dict) and link.get("rel") == "next" for link in links):
                abort(403, "The connected LinkedIn member is not an approved administrator of the configured Page.")
        abort(502, "LinkedIn Page permission lookup reached its page limit.")

    def _post_id(self, revision, resource, payload, before_send, id_format):
        try:
            with self.api(revision, "POST", V2_API + resource, headers=V2_HEADERS,
                          before_send=before_send, json=payload) as response:
                status = response.status_code
                if status == 201:
                    identifier = response.headers.get("x-restli-id", "")
                    if id_format.fullmatch(identifier):
                        return identifier
                elif 400 <= status < 500 and status != 408:
                    linkedin_upload.denied(response)
                raise LinkedInWriteUnconfirmed
        except HTTPException as error:
            if error.code == 502:
                raise LinkedInWriteUnconfirmed from error
            raise

    def create_scheduled(self, revision, owner, title, start_ms, before_send):
        payload = {"author": {"organization": owner}, "scheduledAt": start_ms, "name": title}
        return self._post_id(revision, "/liveVideos", payload, before_send, LIVE_VIDEO_ID)

    def announce_scheduled(self, revision, owner, video_id, title, before_send):
        payload = {
            "author": owner,
            "lifecycleState": "PUBLISHED",
            "specificContent": {"com.linkedin.ugc.ShareContent": {
                "media": [{"media": "urn:li:liveVideo:" + video_id, "status": "READY"}],
                "shareCommentary": {"text": title},
                "shareMediaCategory": "URN_REFERENCE",
            }},
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "externalDistributionChannels": [],
                "distributedViaFollowFeed": True,
            },
        }
        return self._post_id(revision, "/ugcPosts", payload, before_send, ANNOUNCEMENT_URN)
