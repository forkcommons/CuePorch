"""Fetch a selected asset into the existing library intake staging area."""

import hashlib
import os
import time
from pathlib import Path

import requests
from stock_commons import HEADERS, MAX_BYTES, safe_https
from flask import abort



def _max_upload_bytes():
    cap = MAX_BYTES
    raw = os.environ.get("CUEPORCH_MAX_UPLOAD_BYTES")
    if raw:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return cap
        if value > 0:
            cap = min(cap, value)
    return cap


def download(entry, target):
    cap = _max_upload_bytes()
    kind = entry.get("kind")
    if kind == "audio":
        host = "www.scottbuckley.com.au"
        prefix = "/library/wp-content/uploads/"
    else:
        host = "upload.wikimedia.org"
        prefix = "/wikipedia/commons/"

    url = entry["url"]
    if not safe_https(url, host, prefix):
        abort(502, "The catalog media address is invalid.")
    target_path = Path(target)
    started = time.monotonic()
    total = 0
    digest = hashlib.sha1()

    try:
        with requests.get(
            url,
            stream=True,
            allow_redirects=False,
            timeout=(5, 15),
            headers={**HEADERS, "Accept-Encoding": "identity"},
        ) as response:
            if response.status_code != 200:
                abort(502)

            content_length = None
            header = response.headers.get("Content-Length")
            if header is not None:
                try:
                    content_length = int(header)
                except ValueError:
                    content_length = None
                if content_length is not None and content_length > cap:
                    abort(413)

            if time.monotonic() - started > 60:
                abort(502)

            with target_path.open("wb") as out:
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > cap:
                        abort(413)
                    if time.monotonic() - started > 60:
                        abort(502)
                    out.write(chunk)
                    digest.update(chunk)

        if not total or (content_length is not None and total != content_length):
            abort(502)

        if entry.get("size") is not None:
            try:
                expected_size = int(entry["size"])
            except (TypeError, ValueError):
                abort(502)
            if total != expected_size:
                abort(502)

        expected_sha1 = entry.get("sha1")
        if expected_sha1:
            if digest.hexdigest().lower() != str(expected_sha1).lower():
                abort(502)

    except requests.RequestException:
        abort(502)
    except OSError:
        abort(502)
