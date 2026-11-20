"""Aggregate media response bytes served through this CuePorch installation."""

from datetime import datetime, timezone
import json
import logging
import sqlite3

from flask import request
from werkzeug.wsgi import ClosingIterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS media_bandwidth(
 day TEXT NOT NULL, item TEXT NOT NULL, kind TEXT NOT NULL, bytes INTEGER NOT NULL,
 PRIMARY KEY(day,item,kind));
"""
COUNTS = """coalesce(sum(bytes) FILTER (WHERE kind='live'),0) AS live,
            coalesce(sum(bytes) FILTER (WHERE kind='vod'),0) AS vod"""
LOGGER = logging.getLogger(__name__)


def measure(response, tx, item, kind="vod"):
    if request.method != "GET" or response.status_code not in (200, 206):
        return response
    day = datetime.now(timezone.utc).date().isoformat()
    body = response.iter_encoded()
    close_source = getattr(response.response, "close", None)

    def stream():
        sent = 0
        try:
            for chunk in body:
                sent += len(chunk)
                yield chunk
        finally:
            if sent:
                try:
                    with tx() as con:
                        con.execute("""INSERT INTO media_bandwidth VALUES(?,?,?,?)
                            ON CONFLICT(day,item,kind) DO UPDATE SET bytes=bytes+excluded.bytes""",
                                    (day, item, kind, sent))
                except sqlite3.Error:
                    LOGGER.exception("Could not record media bandwidth")

    response.response = ClosingIterator(stream(), close_source)
    return response


def report(con, first, last, selected, group, titles, events):
    where = "WHERE day BETWEEN ? AND ?"
    params = [first.isoformat(), last.isoformat()]
    if selected:
        where += " AND item IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(selected))

    def totals(row):
        return dict(live_bytes=row["live"], vod_bytes=row["vod"], total_bytes=row["live"] + row["vod"])

    overall = totals(con.execute(f"SELECT {COUNTS} FROM media_bandwidth {where}", params).fetchone())
    if group == "video":
        videos = con.execute(f"""SELECT item,{COUNTS} FROM media_bandwidth {where}
                                 GROUP BY item ORDER BY live+vod DESC,item""", params)
        rows = [dict(label=titles.get(row["item"], events.get(row["item"], row["item"])),
                     item=row["item"], video=row["item"] if row["item"] in titles else None, **totals(row))
                for row in videos]
    else:
        months = con.execute(f"""SELECT substr(day,1,7) AS bucket,{COUNTS} FROM media_bandwidth {where}
                                GROUP BY bucket ORDER BY bucket""", params)
        rows = [dict(label=row["bucket"], video=None, item=None, **totals(row)) for row in months]
    return dict(group=group, totals=overall, rows=rows)
