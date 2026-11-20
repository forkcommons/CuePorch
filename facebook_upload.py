"""Publish a retained video to a Page through the versioned Facebook Graph API."""

import hmac
import re

from flask import abort
from werkzeug.exceptions import TooManyRequests

from workspace_oauth import Config, Connection, provider_request, response_object


GRAPH = "https://graph.facebook.com/v27.0"
MEDIA = "https://graph-video.facebook.com/v27.0"
OAUTH = Config(
    "facebook", "Facebook Pages", "https://www.facebook.com/v27.0/dialog/oauth", GRAPH + "/oauth/access_token",
    "", "pages_show_list pages_read_engagement pages_manage_posts", pkce=False,
)
ID = re.compile(r"[1-9][0-9]{0,29}")
MAX_BYTES = 4 * 1000**3
MAX_PART = 64 * 1024 * 1024


def numeric(value, pattern=ID):
    if (isinstance(value, str) or type(value) is int) and pattern.fullmatch(str(value)):
        return str(value)
    abort(502, "Facebook returned an invalid upload or Page identifier.")


def graph_object(response):
    if response.status_code >= 500 or 300 <= response.status_code < 400:
        abort(502, "Facebook's publishing service is unavailable.")
    header = response.headers.get("Retry-After", "")
    if response.status_code == 429:
        raise TooManyRequests("Facebook is rate-limiting the integration.",
                              retry_after=int(header) if re.fullmatch(r"[0-9]{1,6}", header) else None)
    data = response_object(response)
    if response.status_code != 200 or "error" in data:
        error = data.get("error")
        error = error if isinstance(error, dict) else {}
        if error.get("code") in (4, 17, 32, 613, 80001):
            raise TooManyRequests("Facebook is rate-limiting the integration.",
                                  retry_after=int(header) if re.fullmatch(r"[0-9]{1,6}", header) else None)
        if error.get("code") == 190:
            abort(409, "Facebook authorization expired or was withdrawn. Reconnect it in Integrations.")
        if response.status_code == 403 or error.get("code") in (10, 200):
            abort(403, "Facebook did not grant the required permissions on this Page.")
        if error.get("is_transient") is True:
            abort(502, "Facebook's publishing service is temporarily unavailable.")
        abort(422, "Facebook rejected this authorization, Page or upload request.")
    return data


def access_token(item):
    token = item.get("access_token")
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,4096}", token):
        abort(502, "Facebook returned an invalid access token.")
    return token


class PageConnection(Connection):
    def __init__(self, tx, operator):
        super().__init__(OAUTH, tx, operator)

    def graph_request(self, method, url, token, params=None, **kwargs):
        proof = hmac.digest(self.secret.encode(), token.encode(), "sha256").hex()
        with provider_request(method, url, params=dict(params or {}) | {"appsecret_proof": proof},
                              headers={"Authorization": "Bearer " + token}, **kwargs) as response:
            return graph_object(response)

    def exchange_token(self, params):
        with provider_request("GET", self.config.token_url, params=params | self.client_fields()) as response:
            return access_token(graph_object(response))

    def exchange(self, fields, previous_refresh=None):
        # The Graph API uses a code and redirect URI for the first exchange and
        # its own grant type for a long-lived User token.
        short = self.exchange_token({key: fields[key] for key in ("code", "redirect_uri")})
        user = self.exchange_token(dict(grant_type="fb_exchange_token", fb_exchange_token=short))
        params = dict(fields="id,name,access_token,tasks", limit=100)
        pages = {}
        seen = set()
        for _ in range(10):
            data = self.graph_request("GET", GRAPH + "/me/accounts", user, params)
            rows = data.get("data")
            if not isinstance(rows, list):
                abort(502, "Facebook returned an invalid account list.")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("tasks"), list) or "CREATE_CONTENT" not in row["tasks"]:
                    continue
                page = numeric(row.get("id"))
                name = row.get("name")
                pages[page] = dict(name=name[:200] if isinstance(name, str) else page, token=access_token(row))
                if len(pages) == 25:
                    break
            paging = data.get("paging") or {}
            if len(pages) == 25 or not isinstance(paging, dict) or not paging.get("next"):
                break
            cursors = paging.get("cursors")
            cursor = cursors.get("after") if isinstance(cursors, dict) else None
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen:
                abort(502, "Facebook returned an invalid account continuation.")
            seen.add(cursor)
            params["after"] = cursor
        if not pages:
            abort(403, "Connect with a Facebook account that can publish on at least one Page.")
        return {"pages": pages}

    def pages(self, con):
        row = self.connection(con)
        return self.tokens(row)["pages"] if row else {}

    def video_request(self, revision, page, fields, files=None):
        with self.tx() as con:
            saved = self.tokens(self.authorize(con, revision))
        if page not in saved["pages"]:
            abort(403, "This Facebook Page is no longer connected.")
        return self.graph_request("POST", f"{MEDIA}/{page}/videos", saved["pages"][page]["token"],
                                  data=fields, files=files)


def offsets(data, size, expected_start):
    def byte_offset(key):
        raw = data.get(key)
        if (isinstance(raw, str) or type(raw) is int) and re.fullmatch(r"[0-9]{1,12}", str(raw)):
            return int(raw)
        abort(502, "Facebook did not confirm the next video byte range.")

    start, end = byte_offset("start_offset"), byte_offset("end_offset")
    if (start != expected_start or not start <= end <= size or end - start > MAX_PART
            or (start == end and end != size)):
        abort(502, "Facebook returned an invalid video byte range.")
    return start, end


def start(connection, revision, page, size):
    result = connection.video_request(revision, page, dict(upload_phase="start", published="false", file_size=size))
    _, end = offsets(result, size, 0)
    return dict(id=numeric(result.get("upload_session_id")), video=numeric(result.get("video_id")), end=end)


def send_part(connection, revision, page, session, size, offset, data):
    fields = dict(upload_phase="transfer", upload_session_id=session["id"], start_offset=offset)
    result = connection.video_request(revision, page, fields, files={
        "video_file_chunk": ("video-part", data, "application/octet-stream"),
    })
    return offsets(result, size, session["end"])


def post(connection, revision, page, session, metadata):
    result = connection.video_request(revision, page, dict(
        upload_phase="finish", upload_session_id=session["id"], published="true", secret="false",
        title=metadata["title"], description=metadata["description"],
    ))
    if result.get("success") is not True:
        abort(502, "Facebook did not confirm that the upload was published.")
    return session["video"]


def link(page, video):
    return f"https://www.facebook.com/{page}/videos/{video}/"
