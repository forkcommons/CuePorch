"""Validate deployment-approved hostnames and scope their routes."""

import ipaddress
from urllib.parse import urlsplit

from flask import abort, request
from library_privacy import normalize_domain


def _clean_alias(host):
    if ":" in host:
        raise ValueError(f"host {host!r} must not include a port")
    if "*" in host or "." not in host:
        raise ValueError(f"host {host!r} must be a bare DNS name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError(f"host {host!r} must not be an IP address")


def configured_hosts(canonical, raw):
    items = [item.strip() for item in (raw or '').split(',')]
    items = [item for item in items if item]
    if not items:
        return ()
    parts = urlsplit(canonical)
    if parts.scheme != 'https' or not parts.hostname:
        raise ValueError('Configure an HTTPS workspace address before enabling Event Series hostnames.')
    canonical_host = normalize_domain(parts.hostname)
    hosts = set()
    for item in items:
        hosts.add(_clean_alias(normalize_domain(item)))
    if canonical_host in hosts:
        raise ValueError('Event Series hostnames must not include the workspace hostname.')
    return tuple(sorted(hosts))


def requested_host(value, allowed, visibility):
    if value == '':
        return ''
    if not isinstance(value, str):
        abort(422, 'Event Series hostname must be a string.')
    if visibility == 'team':
        abort(422, 'Event Series hostnames cannot be configured for team-only series.')
    try:
        host = _clean_alias(normalize_domain(value))
    except (ValueError, UnicodeError):
        abort(422, 'Event Series hostname is not a valid domain name.')
    if host not in allowed:
        abort(422, 'Event Series hostname is not permitted for this workspace.')
    return host


def current_host(allowed):
    if not allowed:
        return None
    try:
        parts = urlsplit('//' + request.host)
        if not parts.hostname:
            return None
        host = _clean_alias(normalize_domain(parts.hostname))
    except (ValueError, UnicodeError):
        return None
    return host if host in allowed else None


def guard(app, allowed, serve_alias):
    if not allowed:
        return
    allowed_set = frozenset(allowed)

    @app.before_request
    def _host_guard():
        if current_host(allowed_set) is None:
            return None
        if request.endpoint == "static":
            return None
        if request.path == "/" and request.method in ("GET", "HEAD", "POST"):
            return serve_alias()
        abort(404)
