"""Address and authentication contract for the operator's MediaMTX RTMP relay."""

from dataclasses import dataclass
from hmac import compare_digest
from ipaddress import ip_address
import os
from urllib.parse import urlsplit

from flask import abort, request


@dataclass(frozen=True)
class Endpoints:
    read: str
    publish: str
    control: str
    secret: str


def endpoint(value, schemes):
    parsed = urlsplit(value)
    if (parsed.scheme not in schemes or not parsed.hostname or parsed.port is None or not 0 < parsed.port < 65536
            or parsed.username is not None or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("Use a relay origin with an explicit port.")
    return value.rstrip("/")


def configured():
    reader = os.environ.get("CUEPORCH_RTMP_RELAY_URL", "").strip()
    publisher = os.environ.get("CUEPORCH_RTMP_PUBLISH_URL", "").strip() or reader
    control = os.environ.get("CUEPORCH_RTMP_RELAY_API", "").strip()
    shared = os.environ.get("CUEPORCH_RTMP_RELAY_SECRET", "")
    if not reader or not control or len(shared) < 24 or not shared.isascii():
        return None
    try:
        api = endpoint(control, ("http", "https"))
        host = urlsplit(api).hostname
        if host != "localhost" and not ip_address(host).is_loopback:
            return None
        return Endpoints(endpoint(reader, ("rtmp", "rtmps")), endpoint(publisher, ("rtmp", "rtmps")),
                         api, shared)
    except ValueError:
        return None


def register(app, config, studio, interpreters=None):
    @app.post("/api/internal/rtmp/authorize")
    def authorize_rtmp():
        auth = request.authorization
        try:
            local = ip_address(request.remote_addr or "").is_loopback
        except ValueError:
            local = False
        if not config or not local or not auth or auth.type != "basic" or auth.username != "cueporch-relay":
            abort(404)
        if not compare_digest((auth.password or "").encode("utf8"), config.secret.encode("ascii")):
            abort(403)
        body = request.get_json()
        if not isinstance(body, dict) or body.get("protocol") != "rtmp":
            abort(403)
        if studio.authorize_rtmp(body) or (interpreters is not None and interpreters.authorize(body)):
            return "", 204
        abort(403)
