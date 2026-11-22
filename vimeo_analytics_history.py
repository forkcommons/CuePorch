"""Store Vimeo Video report snapshots separately from CuePorch playback."""

import hashlib
import json
from collections import Counter

from flask import jsonify, request

from vimeo_analytics_api import fetch_video_reports
from vimeo_analytics_api_fields import normalize
from vimeo_analytics_csv import parse, valid_utc_date
from vimeo_transfer import INTERRUPTED, Source, SourceFailure


SCHEMA = """
CREATE TABLE IF NOT EXISTS vimeo_analytics_snapshot (
  id TEXT PRIMARY KEY, first TEXT NOT NULL, last TEXT NOT NULL, created INTEGER NOT NULL,
  columns_json TEXT NOT NULL, total_json TEXT NOT NULL, sources_json TEXT NOT NULL, rows_json TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'csv' CHECK(source IN ('csv','api'))
);
"""
MAX_BYTES = 2 * 1024 * 1024
PAGE_SIZE = 100
FIELDS = "id,first,last,columns_json,total_json,sources_json,source"


def initialize(con):
    existing = {row[1] for row in con.execute("PRAGMA table_info(vimeo_analytics_snapshot)")}
    if "source" not in existing:
        con.execute(
            "ALTER TABLE vimeo_analytics_snapshot "
            "ADD COLUMN source TEXT NOT NULL DEFAULT 'csv'"
        )


def digest_for(normalized, account, first, last, source_type):
    material = json.dumps(
        {"account": account, "first": first, "last": last,
         "source": source_type, "report": normalized},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def bad_token(token):
    if not isinstance(token, str):
        return True
    try:
        token.encode("ascii")
    except UnicodeEncodeError:
        return True
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in token)


def save_snapshot(con, record_id, first, last, columns, total, sources, rows, source):
    return con.execute(
        """INSERT INTO vimeo_analytics_snapshot
           (id,first,last,created,columns_json,total_json,sources_json,rows_json,source)
           VALUES (?,?,?,unixepoch(),?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
        (record_id, first, last, columns, total, sources, rows, source),
    )


def present_works(con, works):
    deleted = {row[0] for row in con.execute("SELECT item FROM library_deleted_item WHERE kind='work'")}
    titles = dict(con.execute("SELECT work,title FROM library_title"))
    return {
        str(work["source_id"]): (work["id"], titles.get(work["id"], work["title"]))
        for work in works
        if work.get("provider") == "vimeo" and work.get("asset_type", "video") == "video"
        and work.get("source_id") and work["id"] not in deleted
    }


def summary(row, works):
    counts = json.loads(row["sources_json"])
    count = sum(counts.values())
    matched = sum(size for source, size in counts.items() if source in works)
    return dict(id=row["id"], first=row["first"], last=row["last"], source=row["source"],
                columns=json.loads(row["columns_json"]), total=json.loads(row["total_json"]),
                count=count, matched=matched, unmatched=count - matched)


def private(value, status=200):
    response = jsonify(value)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def register(app, tx, staff, read_works, simulation):
    @app.get("/api/analytics/vimeo-history")
    def list_vimeo_history():
        staff()
        with tx() as con:
            works = present_works(con, read_works())
            records = con.execute(
                f"SELECT {FIELDS} FROM vimeo_analytics_snapshot ORDER BY created DESC,rowid DESC"
            )
            return private({"reports": [summary(record, works) for record in records]})

    @app.post("/api/analytics/vimeo-history")
    def import_vimeo_history():
        staff()
        request.max_content_length = MAX_BYTES + 16384
        upload = request.files.get("file")
        if upload is None:
            return private({"error": "Choose a Vimeo analytics CSV."}, 400)
        raw = upload.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return private({"error": "The report exceeds 2 MiB."}, 413)
        first = request.form.get("first", "")
        last = request.form.get("last", "")
        try:
            report = parse(raw, first, last)
        except ValueError as error:
            return private({"error": str(error)}, 400)
        digest = hashlib.sha256(first.encode() + last.encode() + raw).hexdigest()
        columns = json.dumps(report["columns"])
        total = json.dumps(report["total"])
        sources = json.dumps(Counter(row["source_id"] for row in report["rows"]))
        record = dict(id=digest, first=first, last=last, columns_json=columns,
                      total_json=total, sources_json=sources, source="csv")
        preview = request.args.get("preview") == "1"
        with tx() as con:
            works = present_works(con, read_works())
            if preview:
                return private(summary(record, works) | {"id": None})
            result = save_snapshot(con, digest, first, last, columns, total, sources,
                                   json.dumps(report["rows"]), "csv")
            return private(summary(record, works), 201 if result.rowcount else 200)

    @app.post("/api/analytics/vimeo-history/import-source")
    def import_vimeo_source():
        staff()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return private({"error": "JSON body required."}, 400)
        first = payload.get("first", "")
        last = payload.get("last", "")
        token = payload.get("token")
        if not all(isinstance(value, str) and valid_utc_date(value) for value in (first, last)) or first > last:
            return private({"error": "Enter a valid UTC date range."}, 400)
        if token is not None and bad_token(token):
            return private({"error": "token contains invalid characters."}, 400)
        try:
            source = Source(simulation, token)
        except ValueError:
            return private({"error": "Enter an Enterprise Vimeo Analytics API token or configure the server token."}, 400)
        try:
            account, video_rows, total_row = fetch_video_reports(source, first, last)
        except SourceFailure as error:
            if error.status in (401, 403):
                return private({"error": "Vimeo did not grant Analytics API access for this token."}, error.status)
            return private({"error": "Vimeo could not return the analytics report."}, 502)
        except (*INTERRUPTED, OSError):
            return private({"error": "The Vimeo analytics connection was interrupted."}, 502)
        except ValueError:
            return private({"error": "Vimeo returned an invalid or incomplete analytics report."}, 502)
        try:
            normalized = normalize(video_rows, total_row)
        except ValueError:
            return private({"error": "Vimeo provider returned unexpected data."}, 502)
        digest = digest_for(normalized, account, first, last, "api")
        columns = json.dumps(normalized["columns"])
        total = json.dumps(normalized["total"])
        sources = json.dumps(Counter(row["source_id"] for row in normalized["rows"]))
        record = dict(id=digest, first=first, last=last, columns_json=columns,
                      total_json=total, sources_json=sources, source="api")
        with tx() as con:
            works = present_works(con, read_works())
            result = save_snapshot(con, digest, first, last, columns, total, sources,
                                   json.dumps(normalized["rows"]), "api")
            return private(summary(record, works), 201 if result.rowcount else 200)

    @app.get("/api/analytics/vimeo-history/<uid>")
    def read_vimeo_history(uid):
        staff()
        page = request.args.get("page", default=1, type=int)
        if not 1 <= page <= 200:
            return private({"error": "Choose a valid report page."}, 400)
        with tx() as con:
            record = con.execute(
                f"SELECT {FIELDS},rows_json FROM vimeo_analytics_snapshot WHERE id=?", (uid,)
            ).fetchone()
            if record is None:
                return private({"error": "Vimeo analytics report was not found."}, 404)
            works = present_works(con, read_works())
            offset = (page - 1) * PAGE_SIZE
            rows = json.loads(record["rows_json"])[offset:offset + PAGE_SIZE]
            for row in rows:
                row["work"], row["title"] = works.get(row["source_id"], (None, None))
            return private(summary(record, works) | {"page": page, "rows": rows})
