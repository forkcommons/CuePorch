"""Read attendee CSV and match source columns to an enabled registration form."""

import csv
from io import StringIO
import json
import re

from flask import abort


MAX_BYTES = 6 * 1024 * 1024
MAX_ATTENDEES = 1000


def read_file(upload):
    if not upload:
        abort(422, "Choose an attendee CSV file.")
    payload = upload.stream.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        abort(413, "Use a CSV file of up to 6 MiB.")
    try:
        table = csv.reader(StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
        headers = [cell.strip() for cell in next(table, ())]
        if (not 1 <= len(headers) <= 100 or any(not name or len(name) > 240 for name in headers)
                or len(set(headers)) != len(headers)):
            abort(422, "Use distinct column names in the first row of your CSV.")
        rows = []
        for values in table:
            if not any(cell.strip() for cell in values):
                continue
            if len(values) != len(headers):
                abort(422, f"CSV line {table.line_num} has a different number of columns than the header.")
            if len(rows) == MAX_ATTENDEES:
                abort(422, "Use up to 1000 attendee rows per CSV.")
            rows.append((table.line_num, values))
    except (UnicodeError, csv.Error):
        abort(422, "Save the attendee list as a valid UTF-8 comma-separated CSV.")
    if not rows:
        abort(422, "Include at least one attendee row.")
    return headers, rows


def destinations(form):
    columns = [dict(id="email", label="Email", required=True, type="email")]
    for key, label in (("first_name", "First name"), ("last_name", "Last name")):
        if form[key] != "off":
            columns.append(dict(id=key, label=label, required=form[key] == "required", type="text"))
    return columns + [field for field in form["fields"] if field["type"] != "description"]


def suggested(columns, headers):
    def normalized(label):
        return re.sub(r"[\W_]+", "", label.casefold())

    aliases = {"email": ("email address", "e-mail"), "first_name": ("given name",), "last_name": ("family name", "surname")}
    source_names = [normalized(header) for header in headers]
    result = {}
    for column in columns:
        accepted = {normalized(value) for value in (column["id"], column["label"], *aliases.get(column["id"], ()))}
        matches = [index for index, value in enumerate(source_names) if value in accepted]
        if len(matches) == 1:
            result[column["id"]] = matches[0]
    return result


def mapping(raw, columns, headers):
    if raw is None:
        raw = suggested(columns, headers)
    else:
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            abort(422, "Send the CSV column mapping as a JSON object.")
    fields = {column["id"]: column for column in columns}
    if not isinstance(raw, dict) or any(key not in fields or type(index) is not int or not 0 <= index < len(headers)
                                        for key, index in raw.items()):
        abort(422, "Match each attendee field to an available CSV column.")
    missing = [column["label"] for column in columns if column["required"] and column["id"] not in raw]
    if missing:
        abort(422, "Match the required columns: " + ", ".join(missing) + ".")
    return {key: (index, fields[key]["type"]) for key, index in raw.items()}


def attendee_fields(line, cells, columns):
    fields = {key: cells[index].strip() for key, (index, _) in columns.items()}
    for key, (_, kind) in columns.items():
        if kind == "checkbox":
            value = fields[key].casefold()
            if value not in ("", "no", "false", "0", "yes", "true", "1"):
                abort(422, f"CSV line {line} has a checkbox value outside yes/no, true/false or 1/0.")
            fields[key] = "yes" if value in ("yes", "true", "1") else "no"
    return fields
