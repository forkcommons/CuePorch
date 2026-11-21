"""Workspace LinkedIn Live grant and its configured Company Page."""

from dataclasses import replace
import os

from flask import abort

import linkedin_upload
import team_accounts
from workspace_oauth import Connection, response_object


OAUTH = replace(linkedin_upload.OAUTH, key="linkedin-live", name="LinkedIn Live",
                scope="r_organization_live w_organization_live r_organization_admin")
API = "https://api.linkedin.com/rest"
PAGE_SIZE = 10
PAGE_LIMIT = 20


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
