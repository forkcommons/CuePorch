"""Workspace cloud connections and verified private video imports."""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from threading import Lock
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import abort, redirect, render_template, request
from werkzeug.exceptions import HTTPException, TooManyRequests

from browser_account_security import (
    TOKEN, json_mutation, private, same_origin, secure_site, token_hash,
    workspace_session, workspace_session_cookie,
)
from library_analytics import source_origin
from library_upload import VIDEO_INPUT, max_upload_bytes

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_drive_attempt(
 id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL, binding TEXT NOT NULL,
 verifier TEXT NOT NULL, redirect_uri TEXT NOT NULL, expires INTEGER NOT NULL,
 claimed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS workspace_drive_connection(
 id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL, token BLOB NOT NULL);
"""
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE = "https://oauth2.googleapis.com/revoke"
FILES = "https://www.googleapis.com/drive/v3/files"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DIRECTORY = "/operator/integrations"
CALLBACK = "/integration/oauth/google-drive/callback"
MARKER = "X-CuePorch-Integration"
COOKIE = "drive-oauth"
FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")
FIELDS = "id,name,mimeType,trashed,size,version,md5Checksum,sha256Checksum,capabilities(canDownload)"
ATTEMPT_TTL = 600


def provider_request(method, url, **kwargs):
    # Provider URLs are fixed. In particular, never follow a download redirect carrying a token.
    try:
        return requests.request(method, url, timeout=(8, 45), allow_redirects=False, **kwargs)
    except requests.RequestException:
        abort(502, "Google Drive is unavailable. Try again.")


def response_object(response):
    try:
        data = response.json()
    except ValueError:
        abort(502, "Google Drive returned an invalid response.")
    if not isinstance(data, dict):
        abort(502, "Google Drive returned an invalid response.")
    return data


def check_provider(response):
    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "")
        raise TooManyRequests("Google Drive is busy. Wait and try again.",
                              retry_after=int(retry) if re.fullmatch(r"\d{1,8}", retry) else None)
    if response.status_code == 404:
        abort(404, "This file is no longer available on Google Drive.")
    if response.status_code == 403:
        abort(403, "Google Drive denied access to this file or request.")
    if response.status_code != 200:
        abort(502, "Google Drive could not complete the request. Try again.")


def video_file(row):
    return (
        isinstance(row, dict)
        and isinstance(row.get("id"), str) and FILE_ID.fullmatch(row["id"])
        and isinstance(row.get("name"), str) and 0 < len(row["name"]) <= 512
        and row["name"].rpartition(".")[2].lower() in VIDEO_INPUT
        and isinstance(row.get("mimeType"), str) and row["mimeType"].startswith("video/")
        and not row.get("trashed")
        and isinstance(row.get("capabilities"), dict) and row["capabilities"].get("canDownload") is True
    )


def register(app, tx, staff, operator, origin, import_video):
    client = os.environ.get("CUEPORCH_GOOGLE_DRIVE_CLIENT_ID", "").strip()
    secret = os.environ.get("CUEPORCH_GOOGLE_DRIVE_CLIENT_SECRET", "").strip()
    cipher = Fernet(base64.urlsafe_b64encode(hmac.digest(
        operator.encode(), b"CuePorch Google Drive OAuth", "sha256",
    )))
    refresh_lock = Lock()

    def site():
        secure = secure_site(origin)
        current = source_origin(("https://" if secure else "http://") + request.host)
        if origin != "Unknown" and current != origin:
            abort(403, "Open Integrations using the configured CuePorch address.")
        return secure, current

    def connection(con):
        return con.execute("SELECT * FROM workspace_drive_connection WHERE id=1").fetchone()

    def token_data(row):
        try:
            return json.loads(cipher.decrypt(row["token"]))
        except (InvalidToken, ValueError):
            abort(409, "Disconnect and reconnect Google Drive with the current workspace credentials.")

    def token_exchange(fields, previous_refresh=None):
        with provider_request("POST", TOKEN_URL, data=fields | {
            "client_id": client, "client_secret": secret,
        }) as response:
            if response.status_code == 429:
                abort(429, "Google Drive is busy. Wait and try again.")
            data = response_object(response)
            if data.get("error") == "invalid_grant":
                abort(409, "Google Drive authorization expired or was withdrawn. Reconnect to continue.")
            if response.status_code != 200:
                abort(502, "Google Drive authorization could not be completed.")
        refresh = data.get("refresh_token", previous_refresh)
        access = data.get("access_token")
        expires = data.get("expires_in")
        scope = data.get("scope", SCOPE)
        if not (isinstance(access, str) and 1 <= len(access) <= 4096 and access.isascii()
                and access.isprintable() and isinstance(refresh, str) and 1 <= len(refresh) <= 4096
                and isinstance(expires, int) and 1 <= expires <= 86400
                and isinstance(scope, str) and SCOPE in scope.split()
                and str(data.get("token_type", "")).casefold() == "bearer"):
            abort(502, "Google Drive did not provide the offline read access needed for imports.")
        return dict(access=access, refresh=refresh, expires=int(time.time()) + expires)

    def authorize(con, revision):
        row = connection(con)
        if not row or row["revision"] != revision:
            abort(409, "The Google Drive connection changed. Reopen the integration and try again.")
        return row

    def access_token(revision, rejected=None):
        with refresh_lock:
            with tx() as con:
                row = authorize(con, revision)
                saved = token_data(row)
            if saved["expires"] > time.time() + 30 and saved["access"] != rejected:
                return saved["access"]
            updated = token_exchange(
                {"grant_type": "refresh_token", "refresh_token": saved["refresh"]}, saved["refresh"],
            )
            with tx() as con:
                authorize(con, revision)
                con.execute("UPDATE workspace_drive_connection SET token=? WHERE id=1",
                            (cipher.encrypt(json.dumps(updated).encode()),))
            return updated["access"]

    def drive_request(revision, url, params, stream=False):
        access = access_token(revision)
        response = provider_request("GET", url, params=params, stream=stream,
                                    headers={"Authorization": "Bearer " + access})
        if response.status_code == 401:
            response.close()
            access = access_token(revision, rejected=access)
            response = provider_request("GET", url, params=params, stream=stream,
                                        headers={"Authorization": "Bearer " + access})
        return response

    def read_api():
        secure, _ = site()
        same_origin(secure)
        return staff()

    def mutate():
        secure, current = site()
        body = json_mutation(secure, MARKER)
        return staff(), body, secure, current

    def connected():
        if not client or not secret:
            abort(503, "Set up the workspace's Google Drive OAuth application to continue.")
        with tx() as con:
            row = connection(con)
            if not row:
                abort(409, "Connect Google Drive to continue.")
            return row["revision"]

    @app.get(DIRECTORY)
    def integration_directory():
        site()
        return private(render_template("library_integrations.html"))

    @app.after_request
    def protect_connection_responses(response):
        if request.path == CALLBACK or request.path.startswith("/api/integrations"):
            response.cache_control.no_store = True
            response.cache_control.private = True
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/integrations")
    def integrations():
        read_api()
        with tx() as con:
            linked = connection(con) is not None
        return private(dict(integrations=[dict(
            id="google-drive", name="Google Drive", category="Cloud storage",
            description="Browse and copy videos into your private Library.",
            configured=bool(client and secret), connected=linked,
        )]))

    @app.post("/api/integrations/google-drive/connect")
    def connect_google_drive():
        _, _, secure, current = mutate()
        if not client or not secret:
            abort(503, "Set up the workspace's Google Drive OAuth application to continue.")
        state = secrets.token_urlsafe(32)
        browser = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        redirect_uri = current + CALLBACK
        with tx() as con:
            if connection(con):
                abort(409, "Disconnect Google Drive before connecting another account.")
            con.execute("INSERT OR REPLACE INTO workspace_drive_attempt VALUES(1,?,?,?,?,?,0)", (
                token_hash(state), token_hash(browser), verifier, redirect_uri,
                int(time.time()) + ATTEMPT_TTL,
            ))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        url = AUTH + "?" + urlencode(dict(
            client_id=client, response_type="code", redirect_uri=redirect_uri, scope=SCOPE,
            access_type="offline", prompt="consent", state=state,
            code_challenge=challenge, code_challenge_method="S256",
        ))
        return workspace_session_cookie(private(dict(authorization_url=url)), COOKIE, secure, ATTEMPT_TTL, browser)

    @app.get(CALLBACK)
    def google_drive_callback():
        secure, current = site()
        state = request.args.get("state", "")
        binding = workspace_session(COOKIE, secure)
        if not binding or not TOKEN.fullmatch(state):
            abort(403, "This Google Drive connection did not start in this browser.")
        with tx() as con:
            attempt = con.execute("SELECT * FROM workspace_drive_attempt WHERE id=1").fetchone()
            if not (attempt and not attempt["claimed"] and attempt["expires"] >= time.time()
                    and hmac.compare_digest(attempt["state"], token_hash(state))
                    and hmac.compare_digest(attempt["binding"], binding)
                    and attempt["redirect_uri"] == current + CALLBACK):
                abort(403, "This Google Drive connection request has expired or already been used.")
            con.execute("UPDATE workspace_drive_attempt SET claimed=1 WHERE id=1")
        result = "cancelled"
        try:
            code = request.args.get("code", "")
            if not request.args.get("error") and 1 <= len(code) <= 4096:
                with refresh_lock:
                    tokens = token_exchange(dict(grant_type="authorization_code", code=code,
                                                 code_verifier=attempt["verifier"], redirect_uri=attempt["redirect_uri"]))
                    with tx() as con:
                        if not con.execute("SELECT 1 FROM workspace_drive_attempt WHERE state=? AND claimed=1",
                                           (attempt["state"],)).fetchone():
                            abort(409, "This connection request was replaced or cancelled.")
                        con.execute("INSERT INTO workspace_drive_connection VALUES(1,?,?)", (
                            uuid.uuid4().hex, cipher.encrypt(json.dumps(tokens).encode()),
                        ))
                result = "connected"
        finally:
            with tx() as con:
                con.execute("DELETE FROM workspace_drive_attempt WHERE state=?", (attempt["state"],))
        response = private(redirect(DIRECTORY + "?drive=" + result, code=303))
        response.headers["Referrer-Policy"] = "no-referrer"
        return workspace_session_cookie(response, COOKIE, secure, ATTEMPT_TTL, None)

    @app.post("/api/integrations/google-drive/disconnect")
    def disconnect_google_drive():
        mutate()
        revoked = True
        with refresh_lock:
            with tx() as con:
                row = connection(con)
                con.execute("DELETE FROM workspace_drive_attempt")
                con.execute("DELETE FROM workspace_drive_connection")
            if row:
                try:
                    with provider_request("POST", REVOKE, data={"token": token_data(row)["refresh"]}) as response:
                        revoked = response.status_code == 200
                except HTTPException:
                    revoked = False
        return private(dict(connected=False, provider_revoked=revoked))

    @app.get("/api/integrations/google-drive/files")
    def google_drive_files():
        read_api()
        revision = connected()
        search = request.args.get("q", "")[:160].strip().replace("\\", "\\\\").replace("'", "\\'")
        cursor = request.args.get("cursor", "")
        if len(cursor) > 2048:
            abort(400, "The Google Drive page token is invalid.")
        query = "trashed = false and mimeType contains 'video/'"
        if search:
            query += f" and name contains '{search}'"
        params = dict(q=query, pageSize=50, fields=f"nextPageToken,files({FIELDS})",
                      orderBy="modifiedTime desc", supportsAllDrives="true", includeItemsFromAllDrives="true")
        if cursor:
            params["pageToken"] = cursor
        with drive_request(revision, FILES, params) as response:
            check_provider(response)
            rows = response_object(response)
        files, next_page = rows.get("files", []), rows.get("nextPageToken")
        if not isinstance(files, list) or (next_page is not None and
                                          (not isinstance(next_page, str) or len(next_page) > 2048)):
            abort(502, "Google Drive returned an invalid file listing.")
        with tx() as con:
            authorize(con, revision)
        return private(dict(
            files=[dict(id=row["id"], name=row["name"]) for row in files if video_file(row)],
            cursor=next_page,
        ))

    @app.post("/api/integrations/google-drive/import")
    def google_drive_import():
        actor, body, _, _ = mutate()
        item, folder = body.get("file"), body.get("folder")
        if not isinstance(item, str) or not FILE_ID.fullmatch(item):
            abort(400, "Choose a Google Drive video.")
        if folder is not None and (not isinstance(folder, str) or not re.fullmatch(r"[a-f0-9]{32}", folder)):
            abort(400, "Choose a Library destination folder.")
        revision = connected()
        url = FILES + "/" + item
        with drive_request(revision, url, dict(fields=FIELDS, supportsAllDrives="true")) as response:
            check_provider(response)
            row = response_object(response)
        if row.get("id") != item or not video_file(row):
            abort(415, "Choose a supported video that its owner allows you to download.")
        size, version = str(row.get("size", "")), str(row.get("version", ""))
        sha, md5 = row.get("sha256Checksum", ""), row.get("md5Checksum", "")
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha):
            checksum, expected = hashlib.sha256(), sha
        elif isinstance(md5, str) and re.fullmatch(r"[0-9a-f]{32}", md5):
            checksum, expected = hashlib.md5(usedforsecurity=False), md5
        else:
            abort(422, "Google Drive did not provide a checksum for this video.")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", size) or not re.fullmatch(r"[0-9]{1,20}", version):
            abort(422, "Google Drive did not provide a verifiable video revision.")
        size = int(size)
        if size > max_upload_bytes():
            abort(413, "The video exceeds the workspace upload limit.")

        def save(destination):
            with drive_request(revision, url, dict(alt="media", supportsAllDrives="true"), stream=True) as response:
                check_provider(response)
                total = 0
                try:
                    with destination.open("wb") as stream:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            total += len(chunk)
                            if total > size:
                                abort(422, "The Drive video changed during transfer. Try again.")
                            checksum.update(chunk)
                            stream.write(chunk)
                except requests.RequestException:
                    abort(502, "Google Drive interrupted the transfer. Try again.")
                if total != size or not hmac.compare_digest(checksum.hexdigest(), expected):
                    abort(422, "The Drive video did not match its listed size and checksum. Try again.")

        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cueporch/google-drive/{item}/{version}/{expected}"))
        receipt, status = import_video(actor, identity, row["name"], folder, save,
                                       lambda con: authorize(con, revision))
        return private(receipt, status)
