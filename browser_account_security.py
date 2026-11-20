"""Shared request and credential rules for CuePorch browser accounts."""

import hashlib
import ipaddress
import re
from urllib.parse import urlsplit

from flask import abort, current_app, make_response, request

from library_analytics import source_origin
from library_registration import EMAIL


TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


def private(body, status=None):
    response = make_response(body) if status is None else make_response(body, status)
    response.cache_control.no_store = True
    response.cache_control.private = True
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def email_key(value):
    if not isinstance(value, str):
        abort(400, "Enter a valid email address.")
    value = value.strip().casefold()
    if len(value) > 254 or not EMAIL.fullmatch(value) or any(ord(char) < 32 for char in value):
        abort(400, "Enter a valid email address.")
    return value


def password(body, key="password", new=False):
    value = body.get(key)
    if not isinstance(value, str) or not (12 if new else 1) <= len(value) <= 256:
        abort(400, "Use a password of 12 to 256 characters." if new else "Enter your password.")
    return value


def token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def cookie_session(name):
    token = request.cookies.get(name, "")
    return token_hash(token) if TOKEN.fullmatch(token) else None


def is_loopback(value):
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value or "").is_loopback
    except ValueError:
        return False


def secure_site(public_origin, required=True):
    site = source_origin(request.host_url)
    if site == "Unknown":
        if required:
            abort(400)
        return None
    host = urlsplit(site).hostname
    if request.is_secure or (public_origin.startswith("https://") and
                             source_origin("https://" + request.host) == public_origin):
        return True
    if not is_loopback(host) or not is_loopback(request.remote_addr):
        if required:
            abort(403, "Open CuePorch using its HTTPS address.")
        return None
    return False


def same_origin(secure):
    if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
        abort(403)
    origin = request.headers.get("Origin")
    expected = source_origin(("https://" if secure else "http://") + request.host)
    if origin is not None and source_origin(origin) != expected:
        abort(403)


def json_mutation(secure, marker):
    if not request.is_json or request.headers.get(marker) != "1":
        abort(403)
    same_origin(secure)
    body = request.get_json()
    if not isinstance(body, dict):
        abort(400)
    return body


def set_session_cookie(response, name, secure, lifetime, token):
    flags = dict(path="/", secure=secure, httponly=True, samesite="Lax")
    if token:
        response.set_cookie(name, token, max_age=lifetime, **flags)
    else:
        response.delete_cookie(name, **flags)
    return response


def account_cookie(kind, secure):
    return ("__Host-" if secure else "") + "cueporch-" + kind


def workspace_session(kind, secure):
    legacy = account_cookie(kind, secure)
    scoped = f"{legacy}-{current_app.config['WORKSPACE_COOKIE_SCOPE']}"
    return cookie_session(scoped) or cookie_session(legacy)


def workspace_session_cookie(response, kind, secure, lifetime, token):
    legacy = account_cookie(kind, secure)
    scoped = f"{legacy}-{current_app.config['WORKSPACE_COOKIE_SCOPE']}"
    if legacy in request.cookies:
        set_session_cookie(response, legacy, secure, lifetime, None)
    return set_session_cookie(response, scoped, secure, lifetime, token)
