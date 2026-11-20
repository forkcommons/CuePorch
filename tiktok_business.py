"""TikTok for Business advertiser connection integration."""
import hashlib
import re
import uuid

from flask import abort, request
from werkzeug.exceptions import TooManyRequests

import workspace_oauth
from workspace_oauth import Config, Connection, provider_request

API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
OAUTH = Config(
    "tiktok",
    "TikTok for Business",
    "https://ads.tiktok.com/marketing_api/auth",
    "https://business-api.tiktok.com/open_api/v2.0/oauth2/access_token/",
    "",
    "",
    pkce=False,
)

class AdvertiserConnection(Connection):
    def __init__(self, tx, operator):
        super().__init__(OAUTH, tx, operator)

    def authorization_query(self, redirect_uri, state, verifier):
        return {"app_id": self.client, "redirect_uri": redirect_uri, "state": state}

    def callback_code(self):
        return request.args.get("auth_code", "")

    def exchange(self, fields, previous_refresh=None):
        payload = {
            "app_id": self.client,
            "secret": self.secret,
            "auth_code": fields["code"],
            "return_advertiser_ids": True,
            "is_long_term": True,
        }
        with provider_request("POST", OAUTH.token_url, json=payload) as response:
            data = api_data(response)
        if not isinstance(data, dict):
            abort(502)
        token = data.get("access_token")
        ids = data.get("advertiser_ids")
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,4096}", token):
            abort(502)
        if not isinstance(ids, list) or not ids:
            abort(502)
        if any(not isinstance(a, str) or not re.fullmatch(r"[0-9]{1,30}", a) for a in ids):
            abort(502)
        ids = list(dict.fromkeys(ids))
        return {"access_token": token, "advertiser_ids": ids}

    def pages(self, con):
        row = self.connection(con)
        if not row:
            return {}
        out = {}
        for adv in self.tokens(row).get("advertiser_ids", []):
            out[adv] = {"name": adv}
        return out

    def advertiser_token(self, revision, advertiser):
        with self.tx() as con:
            row = self.authorize(con, revision)
            tok = self.tokens(row)
            if advertiser not in tok.get("advertiser_ids", []):
                abort(403)
            return tok["access_token"]


def api_data(response):
    status = response.status_code

    if status == 429:
        retry_after = 1
        raw_retry = (response.headers or {}).get("Retry-After")
        if isinstance(raw_retry, str):
            value = raw_retry.strip()
            if re.fullmatch(r"[0-9]{1,6}", value):
                retry_after = max(1, min(int(value), 3600))
        raise TooManyRequests(
            description="Rate limit exceeded. Please retry later.",
            retry_after=retry_after,
        )

    if status in (401, 403):
        abort(
            403,
            description="Authorization failed. Please reconnect the TikTok Business account.",
        )

    if status in (400, 404, 422):
        abort(
            422,
            description="The request was rejected by TikTok. Check the parameters and try again.",
        )

    if status != 200:
        abort(502, description="TikTok Business API returned an unexpected response.")

    payload = workspace_oauth.response_object(response)

    code = payload.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        abort(502, description="TikTok Business API returned an invalid response.")

    if code != 0:
        abort(422, description="TikTok Business API rejected the request.")

    return payload.get("data")


class MultipartVideo:
    _CHUNK = 1024 * 1024

    def __init__(self, media, size, fields, media_type):
        self.media = media
        self.size = size
        boundary = uuid.uuid4().hex
        filename = "clip.mov" if media_type == "video/quicktime" else "clip.mp4"
        parts = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="video_file"; filename="{filename}"\r\nContent-Type: {media_type}\r\n\r\n'.encode("utf-8")
        )
        self._prefix = b"".join(parts)
        self._suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        self.content_type = f"multipart/form-data; boundary={boundary}"

    def __len__(self):
        return len(self._prefix) + self.size + len(self._suffix)

    def __iter__(self):
        yield self._prefix
        remaining = self.size
        while remaining:
            chunk = self.media.read(min(remaining, self._CHUNK))
            if not chunk:
                raise OSError("media truncated")
            remaining -= len(chunk)
            yield chunk
        yield self._suffix


MAX_BYTES = 500_000_000
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def upload(connection, revision, advertiser, title, source, size, sha256, media_type):
    if not isinstance(size, int) or size <= 0 or size > MAX_BYTES:
        abort(409, "invalid size")

    f = None
    try:
        try:
            f = source.open("rb")
            md5 = hashlib.md5(usedforsecurity=False)
            sha = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    abort(409, "short read")
                md5.update(chunk)
                sha.update(chunk)
                remaining -= len(chunk)
            if f.read(1):
                abort(409, "size mismatch")
            if sha.hexdigest() != sha256:
                abort(409, "sha256 mismatch")
            f.seek(0)
        except OSError:
            abort(409, "The retained TikTok video is unavailable.")

        token = connection.advertiser_token(revision, advertiser)
        fields = dict(
            advertiser_id=advertiser,
            upload_type="UPLOAD_BY_FILE",
            file_name=title,
            video_signature=md5.hexdigest(),
        )
        body = MultipartVideo(f, size, fields, media_type)
        with provider_request(
            "POST",
            API_BASE + "/file/video/ad/upload/",
            headers={"Access-Token": token, "Content-Type": body.content_type},
            data=body,
        ) as response:
            data = api_data(response)
            if isinstance(data, list):
                if len(data) != 1:
                    abort(502, "invalid response")
                data = data[0]
            if not isinstance(data, dict):
                abort(502, "invalid response")
            video_id = data.get("video_id")
            if not isinstance(video_id, str) or not _VIDEO_ID_RE.fullmatch(video_id):
                abort(502, "invalid video_id")
            return video_id
    finally:
        if f is not None:
            f.close()
