"""Browser-bound OAuth for the workspace's cloud storage connections."""

import base64
from copy import copy
from dataclasses import dataclass, field
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
from flask import abort, redirect, request
from werkzeug.exceptions import HTTPException, TooManyRequests

from browser_account_security import (
    TOKEN, private, token_hash, workspace_session, workspace_session_cookie,
)

DIRECTORY = "/operator/integrations"
CALLBACK_ROOT = "/integration/oauth/"
ATTEMPT_TTL = 600
SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_integration_attempt(
 provider TEXT PRIMARY KEY, state TEXT NOT NULL, binding TEXT NOT NULL,
 verifier TEXT NOT NULL, redirect_uri TEXT NOT NULL, expires INTEGER NOT NULL,
 claimed INTEGER NOT NULL DEFAULT 0, context TEXT);
CREATE TABLE IF NOT EXISTS workspace_integration_connection(
 provider TEXT PRIMARY KEY, revision TEXT NOT NULL, token BLOB NOT NULL);
"""


def migrate_drive_connection(con):
    columns = {row[1] for row in con.execute("PRAGMA table_info(workspace_integration_attempt)")}
    if "context" not in columns:
        con.execute("ALTER TABLE workspace_integration_attempt ADD COLUMN context TEXT")
    # Existing workspaces retain the encrypted Drive connection across the source update.
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_drive_connection'").fetchone():
        con.execute("""INSERT OR IGNORE INTO workspace_integration_connection
                       SELECT 'google-drive',revision,token FROM workspace_drive_connection WHERE id=1""")
        con.execute("DROP TABLE workspace_drive_connection")
        con.execute("DROP TABLE IF EXISTS workspace_drive_attempt")


@dataclass(frozen=True)
class Config:
    key: str
    name: str
    authorization_url: str
    token_url: str
    revoke_url: str
    scope: str
    authorization_parameters: dict = field(default_factory=dict)
    pkce: bool = True
    revoke_style: str = "refresh-form"
    public_client: bool = False
    authorize_scope: bool = True
    refresh_optional: bool = False
    implicit_bearer: bool = False
    nonexpiring_access: bool = False
    token_basic_query: bool = False


def provider_request(method, url, **kwargs):
    try:
        return requests.request(method, url, timeout=(8, 45), allow_redirects=False, **kwargs)
    except requests.RequestException:
        abort(502, "The storage provider is unavailable. Try again.")


def response_object(response):
    try:
        data = response.json()
    except ValueError:
        abort(502, "The storage provider returned an invalid response.")
    if not isinstance(data, dict):
        abort(502, "The storage provider returned an invalid response.")
    return data


def check_provider(response):
    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "")
        raise TooManyRequests("The storage provider is busy. Wait and try again.",
                              retry_after=int(retry) if re.fullmatch(r"\d{1,8}", retry) else None)
    if response.status_code == 404:
        abort(404, "This file is no longer available.")
    if response.status_code == 409:
        abort(409, "The requested file or folder is unavailable. Refresh the picker and try again.")
    if response.status_code == 403:
        abort(403, "The storage provider denied access to this file or request.")
    if response.status_code != 200:
        abort(502, "The storage provider could not complete the request. Try again.")


class Connection:
    def __init__(self, config, tx, operator):
        self.config = config
        self.tx = tx
        self.lock = Lock()
        environment = "CUEPORCH_" + config.key.replace("-", "_").upper()
        self.client = os.environ.get(environment + "_CLIENT_ID", "").strip()
        self.secret = os.environ.get(environment + "_CLIENT_SECRET", "").strip()
        self.configured = bool(self.client and (self.secret or config.public_client))
        self.callback = CALLBACK_ROOT + config.key + "/callback"
        self.cookie = config.key + "-oauth"
        self.key = config.key
        self.owner = ""
        self.seed = hmac.digest(
            operator.encode(), ("CuePorch " + config.name + " OAuth").encode(), "sha256",
        )
        self.cipher = Fernet(base64.urlsafe_b64encode(self.seed))

    def for_member(self, uid):
        if not uid:
            return self
        scoped = copy(self)
        scoped.owner = uid
        scoped.key = f"{self.config.key}:{uid}"
        seed = hmac.digest(self.seed, uid.encode(), "sha256")
        scoped.cipher = Fernet(base64.urlsafe_b64encode(seed))
        return scoped

    def started(self, con, state):
        return TOKEN.fullmatch(state) and con.execute(
            "SELECT 1 FROM workspace_integration_attempt WHERE provider=? AND state=?",
            (self.key, token_hash(state)),
        ).fetchone() is not None

    def connection(self, con):
        return con.execute("SELECT * FROM workspace_integration_connection WHERE provider=?",
                           (self.key,)).fetchone()

    def authorize(self, con, revision):
        row = self.connection(con)
        if not row or row["revision"] != revision:
            abort(409, "The provider connection changed. Reopen Integrations and try again.")
        return row

    def tokens(self, row):
        try:
            return json.loads(self.cipher.decrypt(row["token"]))
        except (InvalidToken, ValueError):
            abort(409, "Disconnect and reconnect using the current workspace credentials.")

    def exchange(self, fields, previous_refresh=None):
        request_kwargs = {}
        if self.config.token_basic_query:
            request_kwargs = dict(
                params=fields,
                auth=(self.client, self.secret),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        else:
            request_kwargs = dict(data=fields | self.client_fields())
        with provider_request("POST", self.config.token_url, **request_kwargs) as response:
            if response.status_code == 429:
                check_provider(response)
            data = response_object(response)
            if data.get("error") == "invalid_grant":
                abort(409, "Authorization expired or was withdrawn. Disconnect and reconnect to continue.")
            if response.status_code != 200:
                abort(502, "Authorization could not be completed.")
        refresh = data.get("refresh_token", previous_refresh)
        access = data.get("access_token")
        expires = data.get("expires_in")
        scope = data.get("scope", self.config.scope)
        valid_refresh = (isinstance(refresh, str) and 1 <= len(refresh) <= 4096
                         or refresh is None and self.config.refresh_optional)
        nonexpiring = self.config.nonexpiring_access and (expires is None or (type(expires) is int and expires == 0))
        token_type = data.get("token_type", "Bearer" if self.config.implicit_bearer else "")
        if not (isinstance(access, str) and 1 <= len(access) <= 4096 and access.isascii()
                and access.isprintable() and valid_refresh
                and (nonexpiring or (type(expires) is int and 1 <= expires <= 90 * 86400))
                and isinstance(scope, str) and set(self.config.scope.split()) <= set(scope.split())
                and str(token_type).casefold() == "bearer"):
            abort(502, "The provider did not grant the access requested for this integration.")
        return dict(access=access, refresh=refresh, expires=None if nonexpiring else int(time.time()) + expires)

    def client_fields(self):
        fields = {"client_id": self.client}
        if not self.config.public_client:
            fields["client_secret"] = self.secret
        return fields

    def require_configured(self):
        if not self.configured:
            abort(503, "Set up this integration's workspace OAuth application to continue.")

    def revision(self):
        self.require_configured()
        with self.tx() as con:
            row = self.connection(con)
            if not row:
                abort(409, "Connect this provider to continue.")
            return row["revision"]

    def authorization_query(self, redirect_uri, state, verifier):
        params = self.config.authorization_parameters | dict(
            client_id=self.client, response_type="code", redirect_uri=redirect_uri, state=state,
        )
        if self.config.scope and self.config.authorize_scope:
            params["scope"] = self.config.scope
        if verifier:
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            params.update(code_challenge=challenge, code_challenge_method="S256")
        return params

    def prepare(self, secure, current, context=None):
        self.require_configured()
        state = secrets.token_urlsafe(32)
        browser = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64) if self.config.pkce else ""
        redirect_uri = current + self.callback
        with self.tx() as con:
            if self.connection(con):
                abort(409, "Disconnect this provider before connecting another account.")
            con.execute("""INSERT OR REPLACE INTO workspace_integration_attempt
                           (provider,state,binding,verifier,redirect_uri,expires,context) VALUES(?,?,?,?,?,?,?)""", (
                self.key, token_hash(state), token_hash(browser), verifier,
                redirect_uri, int(time.time()) + ATTEMPT_TTL, json.dumps(context) if context is not None else None,
            ))
        params = self.authorization_query(redirect_uri, state, verifier)
        url = self.config.authorization_url + "?" + urlencode(params)
        return workspace_session_cookie(private(dict(authorization_url=url)), self.cookie, secure, ATTEMPT_TTL, browser)

    def save_authorization(self, con, tokens, context):
        con.execute("INSERT INTO workspace_integration_connection VALUES(?,?,?)", (
            self.key, uuid.uuid4().hex, self.cipher.encrypt(json.dumps(tokens).encode()),
        ))

    def return_path(self, result, context):
        path = "/team/integrations" if self.owner else DIRECTORY
        return path + "?" + urlencode({"connection": self.config.key, "result": result})

    def callback_code(self):
        return request.args.get("code", "")

    def complete(self, secure, current, allow=None):
        state = request.args.get("state", "")
        binding = workspace_session(self.cookie, secure)
        if not binding or not TOKEN.fullmatch(state):
            abort(403, "This cloud connection did not start in this browser.")
        with self.tx() as con:
            attempt = con.execute("SELECT * FROM workspace_integration_attempt WHERE provider=?",
                                  (self.key,)).fetchone()
            if not (attempt and not attempt["claimed"] and attempt["expires"] >= time.time()
                    and hmac.compare_digest(attempt["state"], token_hash(state))
                    and hmac.compare_digest(attempt["binding"], binding)
                    and attempt["redirect_uri"] == current + self.callback):
                abort(403, "This connection request expired or was already used.")
            con.execute("UPDATE workspace_integration_attempt SET claimed=1 WHERE provider=?", (self.key,))
        result = "cancelled"
        context = json.loads(attempt["context"]) if attempt["context"] else None
        selection = (self.key, attempt["state"])
        try:
            code = self.callback_code()
            if not request.args.get("error") and 1 <= len(code) <= 4096:
                fields = dict(grant_type="authorization_code", code=code, redirect_uri=attempt["redirect_uri"])
                if attempt["verifier"]:
                    fields["code_verifier"] = attempt["verifier"]
                with self.lock:
                    tokens = self.exchange(fields)
                    with self.tx() as con:
                        if allow is not None:
                            allow(con)
                        if not con.execute("""SELECT 1 FROM workspace_integration_attempt
                                             WHERE provider=? AND state=? AND claimed=1""", selection).fetchone():
                            abort(409, "This connection request was replaced or cancelled.")
                        self.save_authorization(con, tokens, context)
                result = "connected"
        finally:
            with self.tx() as con:
                con.execute("DELETE FROM workspace_integration_attempt WHERE provider=? AND state=?", selection)
        response = private(redirect(self.return_path(result, context), code=303))
        return workspace_session_cookie(response, self.cookie, secure, ATTEMPT_TTL, None)

    def refresh(self, saved):
        return self.exchange({"grant_type": "refresh_token", "refresh_token": saved["refresh"]}, saved["refresh"])

    def disconnect(self, clear_related=None):
        with self.lock:
            with self.tx() as con:
                row = self.connection(con)
                con.execute("DELETE FROM workspace_integration_attempt WHERE provider=?", (self.key,))
                con.execute("DELETE FROM workspace_integration_connection WHERE provider=?", (self.key,))
                if clear_related:
                    clear_related(con)
            if not row:
                return True
            if not self.config.revoke_url:
                return False
            try:
                saved = self.tokens(row)
                if self.config.revoke_style != "refresh-form":
                    if saved["expires"] is not None and saved["expires"] <= time.time() + 30:
                        saved = self.refresh(saved)
                    if self.config.revoke_style == "access-bearer":
                        fields = dict(headers={"Authorization": "Bearer " + saved["access"]})
                    else:
                        fields = dict(data=self.client_fields() | {"token": saved["access"]})
                else:
                    fields = dict(data=self.client_fields() | {"token": saved["refresh"]})
                with provider_request("POST", self.config.revoke_url, **fields) as response:
                    return 200 <= response.status_code < 300
            except HTTPException:
                return False

    def access_token(self, revision, rejected=None):
        with self.lock:
            with self.tx() as con:
                saved = self.tokens(self.authorize(con, revision))
            if ((saved["expires"] is None or saved["expires"] > time.time() + 30)
                    and saved["access"] != rejected):
                return saved["access"]
            if not saved["refresh"]:
                abort(409, "Authorization expired or was withdrawn. Reconnect this integration to continue.")
            updated = self.refresh(saved)
            with self.tx() as con:
                self.authorize(con, revision)
                con.execute("UPDATE workspace_integration_connection SET token=? WHERE provider=?",
                            (self.cipher.encrypt(json.dumps(updated).encode()), self.key))
            return updated["access"]

    def api(self, revision, method, url, headers=None, **kwargs):
        access = self.access_token(revision)
        headers = dict(headers or {})
        response = provider_request(method, url, headers=headers | {"Authorization": "Bearer " + access}, **kwargs)
        if response.status_code == 401:
            response.close()
            access = self.access_token(revision, rejected=access)
            response = provider_request(method, url, headers=headers | {"Authorization": "Bearer " + access}, **kwargs)
        return response
