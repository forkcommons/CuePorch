"""Workspace LinkedIn Live grant and its configured Company Page."""

from dataclasses import replace
import os
import re
from urllib.parse import urlsplit

from flask import abort
from werkzeug.exceptions import HTTPException

import linkedin_upload
import team_accounts
from workspace_oauth import Connection, response_object


OAUTH = replace(linkedin_upload.OAUTH, key="linkedin-live", name="LinkedIn Live",
                scope="r_organization_live w_organization_live r_organization_admin")
MEMBER_OAUTH = replace(OAUTH, key="linkedin-member-live", name="LinkedIn Profile Live",
                       scope="r_member_live w_member_live r_liteprofile")
API = "https://api.linkedin.com/rest"
V2_API = "https://api.linkedin.com/v2"
V2_HEADERS = {"X-Restli-Protocol-Version": "2.0.0"}
LIVE_VIDEO_ID = re.compile(r"[0-9]{1,20}")
ANNOUNCEMENT_URN = re.compile(r"urn:li:ugcPost:[0-9]{1,30}")
ASSET_URN = re.compile(r"urn:li:digitalmediaAsset:([A-Za-z0-9_-]{1,128})")
LIVE_RECIPE = "urn:li:digitalmediaRecipe:feedshare-live-video"
INGEST_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.channel\.media\.azure\.net|live-ingest\.linkedin\.com)")
INGEST_PATH = re.compile(r"/live/([A-Za-z0-9_-]{1,256})")
PAGE_SIZE = 10
PAGE_LIMIT = 20
SCHEDULE_PAGE_SIZE = 15
SCHEDULE_MAX_START = 999_990


class LinkedInWriteUnconfirmed(Exception):
    """LinkedIn may have applied a write without confirming its identifier."""


def page_start(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,6}", value):
        abort(400, "Enter a valid LinkedIn events page.")
    start = int(value)
    if start % SCHEDULE_PAGE_SIZE or start > SCHEDULE_MAX_START:
        abort(400, "Enter a valid LinkedIn events page.")
    return start


def parse_ingest(value):
    if (not isinstance(value, str) or not value.isascii() or not value.isprintable()
            or len(value) > 512 or any(char.isspace() or char in "?#@" for char in value)):
        raise ValueError
    endpoint = urlsplit(value)
    stream = INGEST_PATH.fullmatch(endpoint.path)
    if (endpoint.scheme != "rtmps" or not INGEST_HOST.fullmatch(endpoint.hostname or "")
            or endpoint.port not in (None, 443, 2935, 2936) or not stream):
        raise ValueError
    return f"rtmps://{endpoint.netloc}/live", stream[1]


class ProfileLiveConnection(Connection):
    def __init__(self, tx, operator, sharing_access):
        super().__init__(MEMBER_OAUTH, tx, operator)
        self.sharing_access = sharing_access

    def save_authorization(self, con, tokens, context):
        team_accounts.require_live_connection_manager(con, context, self.sharing_access)
        return super().save_authorization(con, tokens, context)

    def authorized_profile(self, revision):
        with self.api(revision, "GET", V2_API + "/me", headers=V2_HEADERS) as response:
            if response.status_code != 200:
                linkedin_upload.denied(response)
            raw = response_object(response).get("id")
        if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw):
            abort(502, "LinkedIn returned an invalid profile ID.")
        return {"id": raw, "urn": "urn:li:person:" + raw}


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

    def register_ingest(self, revision, owner, before_send):
        payload = {"registerLiveEventRequest": {
            "owner": owner, "recipes": [LIVE_RECIPE],
            "region": os.environ.get("CUEPORCH_LINKEDIN_LIVE_REGION", "WEST_US"),
        }}
        try:
            with self.api(revision, "POST", V2_API + "/liveAssetActions", headers=V2_HEADERS,
                          params={"action": "register"}, before_send=before_send, json=payload) as response:
                if response.status_code != 200:
                    if 400 <= response.status_code < 500 and response.status_code != 408:
                        linkedin_upload.denied(response)
                    raise LinkedInWriteUnconfirmed
                data = response_object(response).get("value")
                if not isinstance(data, dict):
                    raise LinkedInWriteUnconfirmed
                asset, candidates = data.get("asset"), data.get("ingestUrls")
                if (not isinstance(asset, str) or not ASSET_URN.fullmatch(asset)
                        or not isinstance(candidates, list) or len(candidates) > 16):
                    raise LinkedInWriteUnconfirmed
                for candidate in candidates:
                    if not isinstance(candidate, dict) or candidate.get("ingestProtocol") != "RTMPS":
                        continue
                    try:
                        server, key = parse_ingest(candidate.get("url"))
                    except ValueError:
                        continue
                    return {"asset": asset, "server": server, "key": key}
                raise LinkedInWriteUnconfirmed
        except HTTPException as error:
            if error.code == 502:
                raise LinkedInWriteUnconfirmed from error
            raise

    def asset_available(self, revision, asset):
        identifier = ASSET_URN.fullmatch(asset)
        if not identifier:
            raise ValueError("Invalid LinkedIn Live asset identifier.")
        with self.api(revision, "GET", f"{V2_API}/assets/{identifier[1]}", headers=V2_HEADERS) as response:
            if response.status_code != 200:
                linkedin_upload.denied(response)
            recipes = response_object(response).get("recipes")
            if not isinstance(recipes, list):
                abort(502, "LinkedIn returned an invalid asset status.")
            return any(isinstance(row, dict) and row.get("recipe") == LIVE_RECIPE
                       and row.get("status") == "AVAILABLE" for row in recipes)

    def bound_asset(self, revision, video):
        if not LIVE_VIDEO_ID.fullmatch(video):
            raise ValueError("Invalid LinkedIn Live broadcast identifier.")
        with self.api(revision, "GET", f"{V2_API}/liveVideos/{video}", headers=V2_HEADERS) as response:
            if response.status_code != 200:
                linkedin_upload.denied(response)
            obj = response_object(response).get("liveVideoAsset")
        if obj is None:
            return None
        media = obj.get("media") if isinstance(obj, dict) else None
        if not isinstance(media, str) or not ASSET_URN.fullmatch(media):
            abort(502, "LinkedIn returned an invalid bound asset.")
        return media

    def bind_asset(self, revision, video, asset, before_send):
        if not LIVE_VIDEO_ID.fullmatch(video) or not ASSET_URN.fullmatch(asset):
            raise ValueError("Invalid LinkedIn Live broadcast or asset identifier.")
        headers = V2_HEADERS | {"X-Restli-Method": "PARTIAL_UPDATE"}
        payload = {"patch": {"$set": {"liveVideoAsset": {"media": asset}}}}
        with self.api(revision, "POST", f"{V2_API}/liveVideos/{video}", headers=headers,
                      before_send=before_send, json=payload) as response:
            if response.status_code != 204:
                linkedin_upload.denied(response)

    def end_asset(self, revision, asset):
        if not ASSET_URN.fullmatch(asset):
            raise ValueError("Invalid LinkedIn Live asset identifier.")
        with self.api(revision, "POST", V2_API + "/liveAssetActions", headers=V2_HEADERS,
                      params={"action": "end"}, json={"asset": asset}) as response:
            if response.status_code != 200:
                linkedin_upload.denied(response)

    def scheduled_events(self, revision, owner, scheduled_ms, start):
        params = {"q": "author", "author": owner, "count": SCHEDULE_PAGE_SIZE, "start": start}
        with self.api(revision, "GET", V2_API + "/liveVideos", params=params, headers=V2_HEADERS) as response:
            if response.status_code != 200:
                linkedin_upload.denied(response)
            data = response_object(response)
        elements, paging = data.get("elements"), data.get("paging", {})
        if not isinstance(elements, list) or len(elements) > SCHEDULE_PAGE_SIZE or not isinstance(paging, dict):
            abort(502, "LinkedIn returned an invalid scheduled events list.")
        links, total = paging.get("links", []), paging.get("total")
        if not isinstance(links, list) or (total is not None and (type(total) is not int or total < 0)):
            abort(502, "LinkedIn returned invalid scheduled events paging.")
        items = []
        for event in elements:
            if not isinstance(event, dict):
                abort(502, "LinkedIn returned an invalid scheduled event.")
            if event.get("scheduledAt") != scheduled_ms or event.get("state", "PRE_LIVE") != "PRE_LIVE":
                continue
            if event.get("liveVideoAsset") or event.get("author", {"organization": owner}) != {"organization": owner}:
                continue
            ident = event.get("id")
            if type(ident) is int:
                ident = str(ident)
            if not isinstance(ident, str) or not LIVE_VIDEO_ID.fullmatch(ident):
                continue
            title = event.get("name")
            post = event.get("ugcPost")
            items.append({"id": ident,
                          "title": title.strip()[:75] if isinstance(title, str) and title.strip() else "LinkedIn Live",
                          "post_urn": post if isinstance(post, str) and ANNOUNCEMENT_URN.fullmatch(post) else None})
        following = start + SCHEDULE_PAGE_SIZE
        more = ((total is not None and total > following)
                or any(isinstance(link, dict) and link.get("rel") == "next" for link in links))
        return {"items": items, "next": following if more and following <= SCHEDULE_MAX_START else None}
