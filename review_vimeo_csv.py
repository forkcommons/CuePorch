"""Read owner-exported Vimeo review notes without contacting Vimeo."""

import csv
import hashlib
import io
import json
import math
import re
from datetime import datetime, timezone


def read_vimeo_review_csv(data: bytes):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValueError("Invalid UTF-8 input") from None

    alias_map = {
        'video version': 'version',
        'version': 'version',
        'timecode': 'seconds',
        'timestamp': 'seconds',
        'seconds': 'seconds',
        'name': 'author',
        'username': 'author',
        'author': 'author',
        'commenter': 'author',
        'note': 'note',
        'comment': 'note',
        'reply': 'reply',
        'date added': 'date',
        'created at': 'date',
        'created': 'date',
        'resolved': 'resolved',
    }

    reader = None
    try:
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)

        headers = None
        header_line = None
        for row in reader:
            if not row or all(not cell.strip() for cell in row):
                continue
            headers = row
            header_line = reader.line_num
            break

        if headers is None:
            line = reader.line_num if reader is not None and reader.line_num else 1
            raise ValueError(f"Row {line}: missing header")

        seen = set()
        field_idx = {}
        for i, header in enumerate(headers):
            normalized = header.strip().casefold()
            if normalized in seen:
                raise ValueError(f"Row {header_line}: duplicate header")
            seen.add(normalized)
            field = alias_map.get(normalized)
            if field is not None:
                if field in field_idx:
                    raise ValueError(f"Row {header_line}: duplicate header")
                field_idx[field] = i

        for required in ('version', 'seconds', 'author', 'note', 'date'):
            if required not in field_idx:
                raise ValueError(f"Row {header_line}: missing required header")

        def cell(row, field):
            idx = field_idx.get(field)
            return '' if idx is None else row[idx].strip()

        groups = {}
        active_version = None
        row_count = 0

        for row in reader:
            line_num = reader.line_num
            if not row or all(not cell_value.strip() for cell_value in row):
                continue

            row_count += 1
            if row_count > 10000:
                raise ValueError(f"Row {line_num}: too many rows")

            if len(row) != len(headers):
                raise ValueError(f"Row {line_num}: mismatched field count")

            version_raw = cell(row, 'version')
            reply = cell(row, 'reply')

            if version_raw:
                active_version = version_raw
                version = version_raw
            else:
                if active_version is None or not reply or reply == '--':
                    raise ValueError(f"Row {line_num}: invalid blank version continuation")
                version = active_version

            item = {
                'line': line_num,
                'version': version,
                'seconds': cell(row, 'seconds'),
                'author': cell(row, 'author'),
                'note': cell(row, 'note'),
                'reply': reply,
                'date': cell(row, 'date'),
                'resolved': cell(row, 'resolved'),
            }

            groups.setdefault(version, []).append(item)

        return groups

    except csv.Error:
        line = reader.line_num if reader is not None and reader.line_num else 1
        raise ValueError(f"Row {line}: CSV error") from None


def vimeo_note_seconds(value, line):
    err = f'Row {line}: enter a timecode in seconds, MM:SS or HH:MM:SS.'
    if value is None:
        raise ValueError(err)
    s = str(value).strip()
    if not s:
        raise ValueError(err)
    try:
        if ':' in s:
            parts = s.split(':')
            if len(parts) == 2:
                m = int(parts[0])
                sec = float(parts[1])
                if m < 0 or sec < 0 or sec >= 60:
                    raise ValueError(err)
                total = m * 60 + sec
            elif len(parts) == 3:
                h = int(parts[0])
                m = int(parts[1])
                sec = float(parts[2])
                if h < 0 or m < 0 or m >= 60 or sec < 0 or sec >= 60:
                    raise ValueError(err)
                total = h * 3600 + m * 60 + sec
            else:
                raise ValueError(err)
        else:
            total = float(s)
        if not math.isfinite(total) or total < 0 or total > 30 * 86400:
            raise ValueError(err)
        return total
    except (ValueError, TypeError):
        raise ValueError(err)


def vimeo_note_date(value, line):
    err = f'Row {line}: creation date is missing or unsupported.'
    if value is None:
        raise ValueError(err)
    s = str(value).strip()
    if not s:
        raise ValueError(err)
    dt = None
    iso = s[:-1] + '+00:00' if s.endswith('Z') else s
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        dt = None
    if dt is None:
        for fmt in ('%A, %B %d, %Y At %I:%M %p', '%m/%d/%Y %I:%M %p'):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise ValueError(err)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.timestamp()
    except (ValueError, OSError, OverflowError):
        raise ValueError(err)


def vimeo_note_version(label):
    match = re.fullmatch(r'(?:v(?:ersion)?\s*)?([0-9]{1,9})', label, re.IGNORECASE)
    return int(match.group(1)) if match else None


def select_vimeo_note_cut(groups, selected, number):
    if not selected:
        if len(groups) == 1:
            selected = next(iter(groups))
        else:
            labels = [
                label
                for label in groups
                if number is not None and vimeo_note_version(label) == number
            ]
            if len(labels) != 1:
                return None
            selected = labels[0]

    if selected not in groups:
        raise KeyError(selected)

    label_number = vimeo_note_version(selected)
    if number is not None and label_number is not None and number != label_number:
        raise ValueError('Source cut does not match the selected video version.')

    return selected


def store_review_notes(con, uid, version, cut, rows):
    duration = cut.get("duration_seconds")
    roots = {}
    occurrences = {}
    imported = 0
    for row in rows:
        line = row["line"]
        seconds = vimeo_note_seconds(row["seconds"], line)
        created = vimeo_note_date(row["date"], line)
        if duration is not None and seconds > duration + 1:
            raise ValueError(f"Row {line}: timecode is outside the selected cut.")
        reply = row["reply"] not in ("", "--")
        body = row["reply"] if reply else row["note"]
        author = row["author"] if row["author"] not in ("", "--") else "Vimeo reviewer"
        if not body or len(body) > 12000 or len(author) > 80 or "\0" in author + body:
            raise ValueError(f"Row {line}: check the note text and author.")
        parent = roots.get((seconds, row["note"])) if reply else None
        if reply and parent is None:
            raise ValueError(f"Row {line}: the reply has no matching preceding note.")
        state = row["resolved"].casefold()
        if state not in ("yes", "true", "1", "no", "false", "0", "", "--"):
            raise ValueError(f"Row {line}: check the resolved value.")
        resolved = state in ("yes", "true", "1")
        identity = (parent, author, body, seconds, created)
        occurrence = occurrences.get(identity, 0)
        occurrences[identity] = occurrence + 1
        encoded = json.dumps((uid, version, identity, occurrence), ensure_ascii=False).encode()
        comment = hashlib.sha256(encoded).hexdigest()[:32]
        imported += con.execute(
            """INSERT OR IGNORE INTO review_comment
               (id,link,version,parent,author,body,seconds,created,resolved)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (comment, uid, version, parent, author, body, seconds, created, resolved),
        ).rowcount
        if not reply:
            roots[(seconds, body)] = comment
    return dict(imported=imported, existing=len(rows) - imported)
