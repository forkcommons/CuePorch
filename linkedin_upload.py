"""Native video and public Company Page posts using LinkedIn's versioned APIs."""

import os
import re
from time import time
from urllib.parse import quote, urlsplit

from flask import abort
from werkzeug.exceptions import TooManyRequests

from workspace_oauth import Config, Connection, provider_request, response_object


OAUTH = Config(
    "linkedin", "LinkedIn Company Page", "https://www.linkedin.com/oauth/v2/authorization",
    "https://www.linkedin.com/oauth/v2/accessToken", "", "w_organization_social",
    pkce=False, refresh_optional=True, implicit_bearer=True,
)
API = "https://api.linkedin.com/rest"
UPLOAD_HOST = "www.linkedin.com"
HEADERS = {"LinkedIn-Version": "202611", "X-Restli-Protocol-Version": "2.0.0"}
VIDEO = re.compile(r"urn:li:video:([A-Za-z0-9_-]{1,128})")
POST = re.compile(r"urn:li:(?:share|ugcPost):[0-9]{1,30}")
COMPANY = re.compile(r"[1-9][0-9]{0,19}")
MAX_BYTES = 500_000_000
MIN_BYTES = 75_000
MAX_PART = 16 * 1024 * 1024
POLL_SECONDS = 5


class CompanyConnection(Connection):
    def __init__(self, tx, operator):
        super().__init__(OAUTH, tx, operator)
        self.company = os.environ.get("CUEPORCH_LINKEDIN_COMPANY_ID", "").strip()
        self.configured = self.configured and bool(COMPANY.fullmatch(self.company))


def denied(response):
    status = response.status_code
    if status == 429:
        value = response.headers.get("Retry-After", "")
        raise TooManyRequests("LinkedIn is rate-limiting this request.",
                              retry_after=int(value) if re.fullmatch(r"[0-9]{1,6}", value) else None)
    if status in (401, 403):
        abort(403, "LinkedIn refused this request. Check the connected account and Page permissions.")
    if 400 <= status < 500:
        abort(422, "LinkedIn rejected the video or its post settings.")
    abort(502, "The LinkedIn publishing service is unavailable.")


def upload_url(value, video):
    try:
        path = VIDEO.fullmatch(video)[1]
        target = urlsplit(value)
        if (len(value) <= 8192 and value.isascii() and not any(ord(char) < 33 for char in value)
                and "\\" not in value and not target.fragment and target.scheme == "https"
                and target.netloc == UPLOAD_HOST and target.path == f"/dms-uploads/{path}/uploadedVideo"
                and target.query):
            return value
    except (TypeError, ValueError):
        pass
    abort(502, "LinkedIn returned an unexpected address for the video.")


def start(connection, revision, company, size):
    payload = dict(initializeUploadRequest=dict(
        owner="urn:li:organization:" + company, fileSizeBytes=size, uploadCaptions=False, uploadThumbnail=False,
    ))
    with connection.api(revision, "POST", API + "/videos", params={"action": "initializeUpload"},
                        headers=HEADERS, json=payload) as response:
        if response.status_code != 200:
            denied(response)
        value = response_object(response).get("value")
    if not isinstance(value, dict):
        abort(502, "LinkedIn did not return a video upload session.")
    video, parts, token, expires = (value.get(key) for key in
                                   ("video", "uploadInstructions", "uploadToken", "uploadUrlsExpireAt"))
    if (not isinstance(video, str) or not VIDEO.fullmatch(video) or not isinstance(parts, list)
            or not 1 <= len(parts) <= 512 or not isinstance(token, str) or len(token) > 4096
            or type(expires) is not int or expires <= time() * 1000):
        abort(502, "LinkedIn returned incomplete video upload instructions.")
    offset = 0
    instructions = []
    for part in parts:
        if not isinstance(part, dict):
            abort(502, "LinkedIn returned an invalid video part.")
        first, last = part.get("firstByte"), part.get("lastByte")
        if (type(first) is not int or type(last) is not int or first != offset
                or not first <= last < size or last - first + 1 > MAX_PART):
            abort(502, "LinkedIn returned an invalid video byte range.")
        instructions.append([first, last, upload_url(part.get("uploadUrl"), video)])
        offset = last + 1
    if offset != size:
        abort(502, "LinkedIn's upload instructions do not cover the retained video.")
    return dict(video=video, parts=instructions, token=token, expires=expires, tags=[], finalized=False)


def send_part(session, address, data):
    with provider_request("PUT", upload_url(address, session["video"]), data=data,
                          headers={"Content-Type": "application/octet-stream"}) as response:
        if response.status_code not in (200, 201):
            denied(response)
        tag = response.headers.get("ETag", "").strip('"')
    if not re.fullmatch(r"[A-Za-z0-9_+=/-]{1,256}", tag):
        abort(502, "LinkedIn did not supply a valid receipt for the video part.")
    return tag


def status(connection, revision, session, company):
    video = session["video"]
    with connection.api(revision, "GET", API + "/videos/" + quote(video, safe=""), headers=HEADERS) as response:
        if response.status_code != 200:
            denied(response)
        item = response_object(response)
    if item.get("id") != video or item.get("owner") != "urn:li:organization:" + company:
        abort(409, "The uploaded video does not belong to the selected LinkedIn Company Page.")
    value = item.get("status")
    if value == "PROCESSING_FAILED":
        abort(422, "LinkedIn could not process the video. Check its format and duration.")
    if value not in ("WAITING_UPLOAD", "PROCESSING", "AVAILABLE"):
        abort(502, "LinkedIn returned an unknown video processing state.")
    return value


def finalize(connection, revision, session):
    payload = dict(finalizeUploadRequest=dict(
        video=session["video"], uploadToken=session["token"], uploadedPartIds=session["tags"],
    ))
    with connection.api(revision, "POST", API + "/videos", params={"action": "finalizeUpload"},
                        headers=HEADERS, json=payload) as response:
        if response.status_code not in (200, 204):
            denied(response)


def post(connection, revision, video, metadata):
    payload = dict(
        author="urn:li:organization:" + metadata["company"], commentary=metadata["description"],
        visibility="PUBLIC", lifecycleState="PUBLISHED",
        distribution=dict(feedDistribution="MAIN_FEED", targetEntities=[], thirdPartyDistributionChannels=[]),
        content=dict(media=dict(id=video, title=metadata["title"])), isReshareDisabledByAuthor=False,
    )
    with connection.api(revision, "POST", API + "/posts", headers=HEADERS, json=payload) as response:
        if response.status_code != 201:
            denied(response)
        post_id = response.headers.get("x-restli-id", "")
    if not POST.fullmatch(post_id):
        abort(502, "LinkedIn did not return a usable post ID.")
    return post_id


def link(post_id):
    return "https://www.linkedin.com/feed/update/" + post_id + "/"
