"""Helpers for Shopify product media domain and OAuth validation."""

import copy
import dataclasses
import hashlib
import hmac
import json
import re
from urllib.parse import urlsplit

from flask import abort, request
from workspace_oauth import Connection, Config

_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SHOP_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com\Z")
_KEY_RE = re.compile(r"[A-Za-z0-9_]+\Z")
_HEX_RE = re.compile(r"[0-9a-f]{64}\Z")
_BAD_VAL = re.compile(r"[&\r\n\x00]")


def shop_domain(value):
    if not isinstance(value, str):
        abort(400, "shop must be a string")
    if not value.isascii():
        abort(400, "invalid shop domain")
    v = value.strip().lower()
    suffix = ".myshopify.com"
    slug = v[: -len(suffix)] if v.endswith(suffix) else v
    if not _SLUG_RE.fullmatch(slug):
        abort(400, "invalid shop domain")
    return slug + suffix


def verified_shop(args, secret):
    for key in args:
        if len(args.getlist(key)) != 1:
            abort(403, "duplicate OAuth parameter")
    for key, val in args.items():
        if not _KEY_RE.fullmatch(key) or _BAD_VAL.search(val):
            abort(403, "unsafe OAuth parameter")
    sig = args.get("hmac")
    shop = args.get("shop")
    if (
        not sig
        or not _HEX_RE.fullmatch(sig)
        or not shop
        or not _SHOP_RE.fullmatch(shop)
    ):
        abort(403, "invalid OAuth signature")
    msg = "&".join(f"{k}={args[k]}" for k in sorted(k for k in args if k != "hmac"))
    expected = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        abort(403, "invalid OAuth signature")
    return shop


OAUTH = Config(
    "shopify",
    "Shopify",
    "",
    "",
    "",
    "read_products,write_products,write_files",
    pkce=False,
)


class StoreConnection(Connection):
    def __init__(self, tx, operator):
        super().__init__(OAUTH, tx, operator)

    def store_origin(self, shop):
        return "https://" + shop_domain(shop)

    def prepare(self, secure, current, context=None):
        shop = shop_domain((context or {}).get("shop"))
        c = copy.copy(self)
        c.config = dataclasses.replace(
            self.config,
            authorization_url=self.store_origin(shop) + "/admin/oauth/authorize",
        )
        return Connection.prepare(c, secure, current, {"shop": shop})

    def complete(self, secure, current, allow=None):
        shop = verified_shop(request.args, self.secret)
        state = request.args.get("state", "")

        with self.tx() as con:
            if not self.started(con, state):
                abort(403, description="OAuth state mismatch or expired")

            row = con.execute(
                "SELECT context FROM workspace_integration_attempt WHERE provider=?",
                (self.key,),
            ).fetchone()
            if not row:
                abort(403, description="OAuth attempt not found")

            if json.loads(row[0]).get("shop") != shop:
                abort(403, description="OAuth shop mismatch")

        c = copy.copy(self)
        c.authorizing_shop = shop
        return Connection.complete(c, secure, current, allow)

    def store_for(self, con):
        row = self.connection(con)
        return self.tokens(row)["shop"] if row else ""

    def _token_exchange(self, shop, fields):
        from time import time
        from workspace_oauth import provider_request, response_object, check_provider

        with provider_request(
            "POST",
            self.store_origin(shop) + "/admin/oauth/access_token",
            data=self.client_fields() | fields,
            headers={"Accept": "application/json"},
        ) as response:
            if response.status_code in (400, 401, 403):
                abort(
                    409,
                    description="Reconnect required: Shopify rejected the authorization request or it has expired.",
                )
            check_provider(response)

            data = response_object(response)
            access = data.get("access_token")
            refresh = data.get("refresh_token")
            if not (isinstance(access, str) and re.fullmatch(r"[!-~]{1,4096}", access)):
                abort(502, description="Provider returned malformed access token.")
            if not (
                isinstance(refresh, str) and re.fullmatch(r"[!-~]{1,4096}", refresh)
            ):
                abort(502, description="Provider returned malformed refresh token.")
            expires_in = data.get("expires_in")
            refresh_expires_in = data.get("refresh_token_expires_in")
            if not (
                isinstance(expires_in, int)
                and not isinstance(expires_in, bool)
                and expires_in > 0
            ):
                abort(502, description="Provider returned malformed token expiry.")
            if not (
                isinstance(refresh_expires_in, int)
                and not isinstance(refresh_expires_in, bool)
                and refresh_expires_in > 0
            ):
                abort(
                    502, description="Provider returned malformed refresh token expiry."
                )

            scope = data.get("scope")
            if not isinstance(scope, str):
                abort(502, description="Provider returned malformed scope.")
            granted = {s.strip() for s in scope.split(",") if s.strip()}
            if "write_products" in granted:
                granted.add("read_products")

            required_scope = self.config.scope or ""
            if not isinstance(required_scope, str):
                abort(
                    500,
                    description="Server configuration error: required scopes are malformed.",
                )
            required = {s.strip() for s in required_scope.split(",") if s.strip()}
            if not required.issubset(granted):
                missing = ", ".join(sorted(required - granted))
                abort(
                    403,
                    description=f"Missing required Shopify scopes; reconnect and grant access to: {missing}.",
                )

            now = int(time())
            return dict(
                shop=shop,
                access=access,
                refresh=refresh,
                expires=now + expires_in,
                refresh_expires=now + refresh_expires_in,
            )

    def exchange(self, fields, previous_refresh=None):
        return self._token_exchange(
            self.authorizing_shop,
            {"code": fields["code"], "expiring": "1"},
        )

    def refresh(self, saved):
        from time import time

        if saved["refresh_expires"] <= int(time()):
            abort(409, description="Reconnect required: the refresh token has expired.")
        return self._token_exchange(
            saved["shop"],
            {"grant_type": "refresh_token", "refresh_token": saved["refresh"]},
        )

    def graphql(self, revision, query, variables):
        from flask import abort
        from werkzeug.exceptions import TooManyRequests
        from workspace_oauth import provider_request, response_object, check_provider

        with self.tx() as con:
            row = self.authorize(con, revision)
            shop = self.tokens(row)["shop"]
            endpoint = self.store_origin(shop) + "/admin/api/2026-10/graphql.json"
            body = {"query": query, "variables": variables}
        token = self.access_token(revision)
        headers = {"X-Shopify-Access-Token": token, "Accept": "application/json"}
        payload = None
        for attempt in range(2):
            with provider_request("POST", endpoint, headers=headers, json=body) as resp:
                status = resp.status_code
                if status == 200:
                    payload = response_object(resp)
                elif status not in (401, 403):
                    check_provider(resp)
                if status != 401 or attempt:
                    break
            token = self.access_token(revision, rejected=token)
            headers["X-Shopify-Access-Token"] = token
        if status in (401, 403):
            message = (
                "Shopify rejected the stored credential; reconnect your store to continue."
                if status == 401
                else "The Shopify app is not permitted to perform this request; reconnect or review granted scopes."
            )
            abort(403, description=message)
        if not isinstance(payload, dict):
            abort(502, description="Shopify returned an unusable response.")
        errors = payload.get("errors")
        code = None
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            ext = errors[0].get("extensions")
            code = ext.get("code") if isinstance(ext, dict) else None
        if errors:
            if code == "THROTTLED":
                raise TooManyRequests("Shopify rate limit reached; retry shortly.")
            if code == "ACCESS_DENIED":
                abort(
                    403,
                    description="The Shopify app lacks permission for this request.",
                )
            abort(
                502,
                description="Shopify reported an error while processing the request.",
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            abort(502, description="Shopify returned an invalid data payload.")
        return data


PRODUCT_ID = re.compile(r"gid://shopify/Product/[1-9][0-9]{0,29}")


_PRODUCTS_QUERY = """
query products($query: String, $cursor: String) {
  products(first: 30, after: $cursor, query: $query) {
    nodes { id title }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_PRODUCT_QUERY = """
query product($id: ID!) {
  product(id: $id) {
    id
    title
  }
}
"""


def _clean_text(value, field, max_length):
    if not isinstance(value, str):
        abort(400, description=f"Invalid {field}")
    if len(value) > max_length:
        abort(400, description=f"{field} exceeds {max_length} characters")
    if "\x00" in value or "\r" in value or "\n" in value:
        abort(400, description=f"{field} contains invalid characters")
    return value


def _validated_product(node, expected_id=None):
    if not isinstance(node, dict):
        abort(502, description="Malformed product")
    node_id = node.get("id")
    if not isinstance(node_id, str) or not PRODUCT_ID.fullmatch(node_id):
        abort(502, description="Malformed product id")
    if expected_id is not None and node_id != expected_id:
        abort(502, description="Product id mismatch")
    title = node.get("title")
    if not isinstance(title, str):
        abort(502, description="Malformed product title")
    return {"id": node_id, "title": title}


def products(connection, revision, query, cursor):
    query = _clean_text(query, "query", 120)
    if cursor is not None:
        cursor = _clean_text(cursor, "cursor", 512)

    variables = {"query": query}
    if cursor is not None:
        variables["cursor"] = cursor

    data = connection.graphql(revision, _PRODUCTS_QUERY, variables)
    if not isinstance(data, dict):
        abort(502, description="Malformed GraphQL response")

    products_data = data.get("products")
    if not isinstance(products_data, dict):
        abort(502, description="Malformed GraphQL response")

    nodes = products_data.get("nodes")
    if not isinstance(nodes, list):
        abort(502, description="Malformed GraphQL response")

    page_info = products_data.get("pageInfo")
    if not isinstance(page_info, dict):
        abort(502, description="Malformed GraphQL response")

    items = [_validated_product(node) for node in nodes]

    has_next = page_info.get("hasNextPage")
    if not isinstance(has_next, bool):
        abort(502, description="Malformed pageInfo")

    next_cursor = None
    if has_next:
        end_cursor = page_info.get("endCursor")
        if not isinstance(end_cursor, str) or not end_cursor or len(end_cursor) > 512:
            abort(502, description="Malformed endCursor")
        next_cursor = end_cursor

    return {"products": items, "cursor": next_cursor}


def product(connection, revision, uid):
    if not isinstance(uid, str) or not PRODUCT_ID.fullmatch(uid):
        abort(400, description="Invalid product id")

    data = connection.graphql(revision, _PRODUCT_QUERY, {"id": uid})
    if not isinstance(data, dict):
        abort(502, description="Malformed GraphQL response")

    if "product" not in data:
        abort(502, description="Malformed GraphQL response")

    prod = data["product"]
    if prod is None:
        abort(404)

    return _validated_product(prod, expected_id=uid)


_STAGED_UPLOADS_MUTATION = """
mutation StagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters {
        name
        value
      }
    }
    userErrors {
      field
      message
    }
  }
}
"""

_VIDEO_HOST = "shopify-video-production-core-originals.storage.googleapis.com"
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
_UNSAFE_URL_RE = re.compile(r"[\x00-\x20\x7f#]")


def _valid_https_url(value):
    if not isinstance(value, str) or not value:
        return False
    if not value.isascii():
        return False
    if _UNSAFE_URL_RE.search(value):
        return False
    try:
        p = urlsplit(value)
    except ValueError:
        return False
    if p.scheme != "https":
        return False
    if p.netloc != _VIDEO_HOST:
        return False
    if p.fragment:
        return False
    return True


def mutation_result(connection, revision, query, variables, field):
    result = connection.graphql(revision=revision, query=query, variables=variables)
    if not isinstance(result, dict):
        abort(502, "Invalid Shopify response")
    payload = result.get(field)
    if not isinstance(payload, dict):
        abort(502, "Invalid Shopify response")
    user_errors = payload.get("userErrors")
    if not isinstance(user_errors, list):
        abort(502, "Invalid Shopify response")
    if user_errors:
        abort(422, "Shopify rejected the request")
    return payload


def staged_target(connection, revision, size, media_type):
    if media_type == "video/mp4":
        filename = "clip.mp4"
    elif media_type == "video/quicktime":
        filename = "clip.mov"
    elif media_type == "video/webm":
        filename = "clip.webm"
    else:
        raise ValueError("unsupported media_type")

    variables = {
        "input": [
            {
                "resource": "VIDEO",
                "httpMethod": "POST",
                "fileSize": str(size),
                "filename": filename,
                "mimeType": media_type,
            }
        ]
    }

    payload = mutation_result(
        connection,
        revision,
        _STAGED_UPLOADS_MUTATION,
        variables,
        "stagedUploadsCreate",
    )

    targets = payload.get("stagedTargets")
    if not isinstance(targets, list) or len(targets) != 1:
        abort(502, "Invalid Shopify response")
    target = targets[0]
    if not isinstance(target, dict):
        abort(502, "Invalid Shopify response")

    upload_url = target.get("url")
    resource_url = target.get("resourceUrl")
    if not _valid_https_url(upload_url) or not _valid_https_url(resource_url):
        abort(502, "Invalid Shopify response")

    params = target.get("parameters")
    if not isinstance(params, list):
        abort(502, "Invalid Shopify response")

    fields = {}
    for p in params:
        if not isinstance(p, dict):
            abort(502, "Invalid Shopify response")
        name = p.get("name")
        value = p.get("value")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            abort(502, "Invalid Shopify response")
        if name.lower() == "file":
            abort(502, "Invalid Shopify response")
        if name in fields:
            abort(502, "Invalid Shopify response")
        if not isinstance(value, str):
            abort(502, "Invalid Shopify response")
        if "\x00" in value:
            abort(502, "Invalid Shopify response")
        if len(value.encode("utf-8")) > 64 * 1024:
            abort(502, "Invalid Shopify response")
        fields[name] = value

    return upload_url, resource_url, fields


def stage_media(connection, revision, source, size, sha256, media_type):
    from pathlib import Path

    import tiktok_business
    from workspace_oauth import check_provider, provider_request

    if media_type not in ("video/mp4", "video/quicktime", "video/webm"):
        abort(409, description="Unsupported media type")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 1 <= size <= 1_000_000_000
    ):
        abort(409, description="Invalid media size")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        abort(409, description="Invalid media SHA256")

    try:
        media = Path(source).open("rb")
    except OSError:
        abort(409, description="The retained media is unavailable.")

    with media:
        try:
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = media.read(min(1 << 20, remaining))
                if not chunk:
                    abort(409, description="Media size mismatch")
                digest.update(chunk)
                remaining -= len(chunk)
            if media.read(1):
                abort(409, description="Media size mismatch")
        except OSError:
            abort(409, description="The retained media is unavailable.")
        if digest.hexdigest() != sha256:
            abort(409, description="Media SHA256 mismatch")
        try:
            media.seek(0)
        except OSError:
            abort(409, description="The retained media is unavailable.")

        upload_url, resource_url, fields = staged_target(
            connection, revision, size, media_type
        )
        body = tiktok_business.MultipartVideo(
            media, size, fields, media_type, file_field="file"
        )
        with provider_request(
            "POST",
            upload_url,
            headers={"Content-Type": body.content_type},
            data=body,
        ) as response:
            if response.status_code not in (200, 201, 204):
                check_provider(response)
                abort(502, description="Shopify media upload failed")
        return resource_url


_VIDEO_RE = re.compile(r"gid://shopify/Video/[1-9][0-9]{0,29}\Z")
_STATUSES = {"UPLOADED", "PROCESSING", "READY", "FAILED"}


def _bad_shape(msg="Invalid remote response shape"):
    abort(502, description=msg)


def _valid_video_gid(v):
    return isinstance(v, str) and _VIDEO_RE.fullmatch(v) is not None


def _valid_product_gid(v):
    return isinstance(v, str) and PRODUCT_ID.fullmatch(v) is not None


def _valid_status(s):
    return isinstance(s, str) and s in _STATUSES


def _single_file(field):
    if not isinstance(field, dict):
        _bad_shape()
    files = field.get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        _bad_shape()
    return files[0]


def create_video(connection, revision, resource_url, title):
    if not _valid_https_url(resource_url):
        abort(400, description="resource_url must be a valid HTTPS URL")
    if not isinstance(title, str) or not title.strip():
        abort(400, description="title is required")

    query = """
    mutation fileCreate($files: [FileCreateInput!]!) {
      fileCreate(files: $files) {
        files { id fileStatus }
        userErrors { field message }
      }
    }
    """
    variables = {
        "files": [
            {
                "originalSource": resource_url,
                "contentType": "VIDEO",
                "alt": title,
            }
        ]
    }
    field = mutation_result(connection, revision, query, variables, "fileCreate")
    f = _single_file(field)
    vid, status = f.get("id"), f.get("fileStatus")
    if not _valid_video_gid(vid):
        _bad_shape("Invalid video ID in response")
    if not _valid_status(status):
        _bad_shape("Invalid file status in response")
    return vid, status


def file_status(connection, revision, video_id):
    if not _valid_video_gid(video_id):
        abort(400, description="Invalid video ID")

    query = """
    query($id: ID!) {
      node(id: $id) {
        id
        ... on File { fileStatus }
      }
    }
    """
    resp = connection.graphql(revision, query, {"id": video_id})
    if not isinstance(resp, dict):
        _bad_shape()
    node = resp.get("node")
    if node is None:
        abort(409, description="Video not found")
    if not isinstance(node, dict) or node.get("id") != video_id:
        _bad_shape("Invalid node in response")
    status = node.get("fileStatus")
    if not _valid_status(status):
        _bad_shape("Invalid file status in response")
    return status


def attach_product(connection, revision, video_id, product_id):
    if not _valid_video_gid(video_id):
        abort(400, description="Invalid video ID")
    if not _valid_product_gid(product_id):
        abort(400, description="Invalid product ID")

    query = """
    mutation fileUpdate($files: [FileUpdateInput!]!) {
      fileUpdate(files: $files) {
        files { id }
        userErrors { field message }
      }
    }
    """
    variables = {
        "files": [
            {
                "id": video_id,
                "referencesToAdd": [product_id],
            }
        ]
    }
    field = mutation_result(connection, revision, query, variables, "fileUpdate")
    f = _single_file(field)
    rid = f.get("id")
    if not _valid_video_gid(rid) or rid != video_id:
        _bad_shape("Mismatched video ID in response")
    return rid
