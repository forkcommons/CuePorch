"""Stock asset license normalization and public credit helpers."""
import json
import unicodedata
from urllib.parse import urlsplit

from flask import abort

LICENSES = {
    "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
    "permission": "",
}
LIMITS = {
    "category": 80,
    "mood": 80,
    "genre": 80,
    "credit": 160,
    "source_url": 512,
    "permission_note": 500,
}


def source_link(value):
    """Return a clean http(s) URL with host and no userinfo, else ''."""
    if not isinstance(value, str):
        return ""
    if not value:
        return ""
    if len(value) > 512:
        return ""
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in value):
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https"):
            return ""
        if not parts.hostname:
            return ""
        if parts.username is not None or parts.password is not None:
            return ""
        _ = parts.port
    except ValueError:
        return ""
    return value


def _field(name, value):
    limit = LIMITS[name]
    if value is None:
        return ""
    if not isinstance(value, str):
        abort(422, description=f"{name} must be a string")
    if any(unicodedata.category(ch).startswith("C") for ch in value):
        abort(422, description=f"{name} contains control characters")
    value = value.strip()
    if len(value) > limit:
        abort(422, description=f"{name} exceeds {limit} characters")
    return value


def normalize_upload(raw_string):
    """Parse upload metadata JSON; return canonical dict, or None if omitted."""
    if raw_string is None or raw_string == "":
        return None
    if not isinstance(raw_string, str):
        abort(422, description="upload metadata must be a JSON string")
    if len(raw_string.encode("utf-8")) > 4096:
        abort(422, description="upload metadata exceeds 4096 bytes")
    try:
        data = json.loads(raw_string)
    except (TypeError, ValueError):
        abort(422, description="upload metadata is not valid JSON")
    if not isinstance(data, dict):
        abort(422, description="upload metadata must be a JSON object")

    category = _field("category", data.get("category"))
    credit = _field("credit", data.get("credit"))
    if not category or not credit:
        abort(422, description="category and credit are required")

    license_id = data.get("license")
    if not isinstance(license_id, str) or license_id not in LICENSES:
        abort(422, description="license must be CC0-1.0, CC-BY-4.0, or permission")

    raw_source_url = data.get("source_url")
    source_url = source_link(raw_source_url)
    if raw_source_url not in (None, "") and not source_url:
        abort(422, description="source_url is malformed")
    permission_note = _field("permission_note", data.get("permission_note"))
    if license_id != "permission" and not source_url:
        abort(422, description="source_url is required for Creative Commons licenses")
    if license_id == "permission" and not permission_note:
        abort(422, description="permission_note is required for permission license")

    return {
        "category": category,
        "credit": credit,
        "mood": _field("mood", data.get("mood")),
        "genre": _field("genre", data.get("genre")),
        "source_url": source_url,
        "license": license_id,
        "license_url": LICENSES[license_id],
        "permission_note": permission_note,
    }



def stock_credit(work, cut):
    metadata = cut.get("stock_asset")
    if not isinstance(metadata, dict):
        return None
    license_id = metadata.get("license")
    if not isinstance(license_id, str) or license_id not in LICENSES:
        return None
    files = cut.get("files")
    if not isinstance(files, list):
        files = []
    original_sha = next(
        (
            item.get("sha256")
            for item in files
            if isinstance(item, dict) and item.get("role") == "original"
        ),
        None,
    )
    sha = original_sha or work.get("source_upload_sha256") or work["id"]
    return {
        "source": "cueporch-stock",
        "id": "asset:" + str(work["id"]) + ":" + str(sha),
        "title": cut.get("title") or work.get("title"),
        "credit": metadata.get("credit"),
        "source_url": source_link(metadata.get("source_url")),
        "license": license_id,
        "license_url": LICENSES[license_id],
        "modified": True,
    }


def public_credits(work):
    def clean(v):
        if not isinstance(v, str):
            return ""
        return "".join(ch for ch in v if ord(ch) >= 32 and ord(ch) != 127)

    def safe_url(v):
        return source_link(v) if v else ""

    if not isinstance(work, dict):
        return []

    assets = list(work.get("editor_assets") or [])
    direct = stock_credit(work, work)
    if isinstance(direct, dict):
        direct = dict(direct)
        direct["modified"] = False
        assets.append(direct)

    out = []
    for a in assets:
        if not isinstance(a, dict) or a.get("source") != "cueporch-stock":
            continue
        lic = a.get("license")
        if not isinstance(lic, str) or lic not in LICENSES:
            continue
        lu = LICENSES.get(lic, "")
        out.append({
            "source": "cueporch-stock" if a.get("source") == "cueporch-stock" else "",
            "title": clean(a.get("title")),
            "credit": clean(a.get("credit")),
            "license": lic,
            "license_url": safe_url(lu),
            "source_url": safe_url(a.get("source_url")),
            "modified": bool(a.get("modified", False)),
        })
    return out


def credit_comment(assets):
    rows = public_credits({"editor_assets": assets})
    blocks = []
    for r in rows:
        parts = []
        if r["title"]:
            parts.append(r["title"])
        if r["credit"]:
            parts.append("Credit: " + r["credit"])
        if r["license"]:
            parts.append("License: " + r["license"])
        if r["source_url"]:
            parts.append("Source: " + r["source_url"])
        if r["license_url"]:
            parts.append("License URL: " + r["license_url"])
        if r["modified"]:
            parts.append("Adapted")
        if parts:
            blocks.append(" | ".join(parts))
    return "\n\n".join(blocks)


def write_credit_metadata(path, assets):
    text = credit_comment(assets)
    if not text:
        return None

    value = text.replace("\\", "\\\\")
    value = value.replace("=", "\\=")
    value = value.replace(";", "\\;")
    value = value.replace("#", "\\#")
    value = value.replace("\n", "\\" + "\n")

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(";FFMETADATA1\n")
        f.write("comment=" + value + "\n")

    return path
