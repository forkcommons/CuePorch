"""Publish a native video clip to a connected public X account."""

import re
from time import time

from flask import abort
from werkzeug.exceptions import TooManyRequests

from workspace_oauth import Config, Connection, provider_request, response_object


API = "https://api.x.com/2"
OAUTH = Config(
    "x", "X", "https://x.com/i/oauth2/authorize", API + "/oauth2/token", API + "/oauth2/revoke",
    "users.read tweet.read tweet.write media.write offline.access", public_client=True,
)
ID = re.compile(r"[1-9][0-9]{0,18}")
CHUNK = 4 * 1024 * 1024
MAX_BYTES = 512 * 1024 * 1024


def x_response(response, codes):
    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "")
        reset = response.headers.get("x-rate-limit-reset", "")
        delay = int(retry) if re.fullmatch(r"[0-9]{1,6}", retry) else None
        if delay is None and re.fullmatch(r"[0-9]{1,12}", reset):
            delay = max(1, int(reset) - int(time()))
        raise TooManyRequests("X is rate-limiting requests.", retry_after=delay)
    if response.status_code == 402:
        abort(402, "The X developer app needs available API credits.")
    if response.status_code in (401, 403):
        abort(403, "X refused the request. Check the account's API permissions and video eligibility.")
    if 400 <= response.status_code < 500:
        abort(422, "X rejected the selected video, post text or upload session.")
    if response.status_code not in codes:
        abort(502, "The X publishing API is unavailable.")
    if response.status_code == 204 or not response.content:
        return {}
    result = response_object(response)
    data = result.get("data")
    if not isinstance(data, dict) or result.get("errors"):
        abort(502, "X did not return a usable response.")
    return data


def profile(data):
    uid, name, protected = (data.get(key) for key in ("id", "username", "protected"))
    if (not isinstance(uid, str) or not ID.fullmatch(uid) or not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", name) or type(protected) is not bool):
        abort(502, "X did not return the connected account's identity and visibility.")
    if protected:
        abort(409, "A protected X account cannot receive a public post from CuePorch.")
    return dict(id=uid, name="@" + name)


class AccountConnection(Connection):
    def __init__(self, tx, operator):
        super().__init__(OAUTH, tx, operator)

    def exchange(self, fields, previous_refresh=None):
        tokens = super().exchange(fields, previous_refresh)
        if previous_refresh is None:
            with provider_request("GET", API + "/users/me", params={"user.fields": "protected"},
                                  headers={"Authorization": "Bearer " + tokens["access"]}) as response:
                tokens["account"] = profile(x_response(response, (200,)))
        return tokens

    def refresh(self, saved):
        return super().refresh(saved) | {"account": saved["account"]}

    def pages(self, con):
        row = self.connection(con)
        if not row:
            return {}
        account = self.tokens(row)["account"]
        return {account["id"]: account}


def account(connection, revision, expected):
    with connection.api(revision, "GET", API + "/users/me", params={"user.fields": "protected"}) as response:
        data = profile(x_response(response, (200,)))
    if data["id"] != expected:
        abort(409, "The connected X account changed. Open a new publication request.")


def start(connection, revision, size):
    payload = dict(media_type="video/mp4", media_category="tweet_video", total_bytes=size)
    with connection.api(revision, "POST", API + "/media/upload/initialize", json=payload) as response:
        data = x_response(response, (200, 201))
    uid, life = data.get("id"), data.get("expires_after_secs")
    if not isinstance(uid, str) or not ID.fullmatch(uid) or type(life) is not int or life <= 0:
        abort(502, "X did not confirm the video upload session or its lifetime.")
    return dict(id=uid, expires=int(time()) + life, segment=0, finalized=False)


def send_part(connection, revision, session, body):
    with connection.api(revision, "POST", API + f"/media/upload/{session['id']}/append",
                        data={"segment_index": session["segment"]},
                        files={"media": ("video-part.mp4", body, "video/mp4")}) as response:
        x_response(response, (200, 204))


def processing(connection, revision, session):
    uid = session["id"]
    if not session["finalized"]:
        response = connection.api(revision, "POST", API + f"/media/upload/{uid}/finalize")
    else:
        response = connection.api(revision, "GET", API + "/media/upload", params=dict(command="STATUS", media_id=uid))
    with response:
        data = x_response(response, (200, 201))
    if data.get("id") != uid:
        abort(502, "X returned a processing status for an unexpected video.")
    progress = data.get("processing_info")
    if progress is None and not session["finalized"]:
        return 0
    if not isinstance(progress, dict):
        abort(502, "X returned an invalid video processing status.")
    state = progress.get("state")
    if state == "succeeded":
        return 0
    if state == "failed":
        abort(422, "X could not process this video.")
    if state not in ("pending", "in_progress"):
        abort(502, "X returned an unknown video processing status.")
    delay = progress.get("check_after_secs")
    return min(3600, max(1, delay)) if type(delay) is int else 5


def post(connection, revision, session, text):
    payload = dict(media={"media_ids": [session["id"]]})
    if text:
        payload["text"] = text
    with connection.api(revision, "POST", API + "/tweets", json=payload) as response:
        data = x_response(response, (201,))
    uid = data.get("id")
    if not isinstance(uid, str) or not ID.fullmatch(uid):
        abort(502, "X did not return the published post's ID.")
    return uid


def link(uid):
    return "https://x.com/i/web/status/" + uid
