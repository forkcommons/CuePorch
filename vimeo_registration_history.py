"""Read owner-supplied Vimeo registrant exports for the private archive."""

import hashlib
import json
import re
from datetime import datetime, timezone

from flask import abort

import registration_csv


COLUMNS = (
    "First name",
    "Last name",
    "Email",
    "Registration Date (UTC)",
    "Views",
    "% Watched",
    "Source Type",
    "Source",
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CELL_MAX = 2048


def _norm(name):
    return re.sub(r"\s+", " ", name.strip().casefold())


def read(upload):
    headers, source_rows = registration_csv.read_file(upload)

    targets = {_norm(col): col for col in COLUMNS}

    header_index = {}
    for idx, raw_header in enumerate(headers):
        name = _norm(raw_header)
        if name in targets:
            col = targets[name]
            if col in header_index:
                abort(422, "Duplicate column: %s" % col)
            header_index[col] = idx

    if "Email" not in header_index:
        abort(422, "Missing required column: Email")
    if "Registration Date (UTC)" not in header_index:
        abort(422, "Missing required column: Registration Date (UTC)")

    rows = []
    for line, cells in source_rows:
        row = {column: cells[header_index[column]] if column in header_index else "" for column in COLUMNS}

        for col in header_index:
            value = row[col]
            if len(value) > _CELL_MAX:
                abort(
                    422,
                    "Cell exceeds %d characters on line %s column %s"
                    % (_CELL_MAX, line, col),
                )
            if chr(0) in value:
                abort(
                    422,
                    "Null byte in cell on line %s column %s" % (line, col),
                )

        email = row["Email"].strip()
        if len(email) > 254:
            abort(422, "Email exceeds 254 characters on line %s" % line)
        if not _EMAIL_RE.fullmatch(email):
            abort(422, "Invalid email on line %s" % line)

        raw_ts = row["Registration Date (UTC)"].strip()
        ts_str = raw_ts.removesuffix(" UTC")
        if "T" not in ts_str and " " not in ts_str:
            abort(422, f"Enter a registration date and time on line {line}.")
        try:
            parsed = datetime.fromisoformat(ts_str)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            abort(422, f"Invalid registration date on line {line}.")

        row["Email"] = email
        row["Registration Date (UTC)"] = parsed.isoformat()
        rows.append(row)

    return rows


def row_key(row):
    payload = json.dumps(
        [
            row["Email"].casefold(),
            row["Registration Date (UTC)"],
            row["Source Type"].casefold(),
            row["Source"],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
