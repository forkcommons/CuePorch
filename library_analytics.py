"""Playback-session metrics for CuePorch-controlled video players."""

import csv
from datetime import date, datetime, time, timedelta, timezone
import hmac
from io import StringIO
from ipaddress import ip_address
import json
import math
import re
from urllib.parse import urlsplit

from flask import Response, abort, request

from catalog import video_version_key


SCHEMA = """
CREATE TABLE IF NOT EXISTS video_analytics_session(
 id TEXT PRIMARY KEY, work TEXT NOT NULL, version TEXT NOT NULL, surface TEXT NOT NULL,
 created REAL NOT NULL, sequence INTEGER NOT NULL, played INTEGER NOT NULL, completed INTEGER NOT NULL,
 watched INTEGER NOT NULL, intervals TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS video_analytics_work_date ON video_analytics_session(work,created);
CREATE INDEX IF NOT EXISTS video_analytics_date ON video_analytics_session(created);
CREATE TABLE IF NOT EXISTS video_analytics_context(
 session TEXT PRIMARY KEY, source TEXT NOT NULL, device TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS video_analytics_region(
 session TEXT PRIMARY KEY, region TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS video_analytics_viewer(
 session TEXT PRIMARY KEY, viewer TEXT NOT NULL);
"""
SURFACES = ("watch", "embed")
DEVICES = ("Desktop", "Phone", "Tablet", "TV", "Unknown")
REGION_CODE = re.compile(r"[A-Z]{2}(?:-[A-Z0-9]{1,3})?")
COUNTS = """count(*) AS impressions, coalesce(sum(played),0) AS views,
            coalesce(sum(completed),0) AS finishes, coalesce(sum(watched),0) AS watched"""


def source_origin(referrer):
    try:
        url = urlsplit(referrer or "")
        if url.scheme not in ("http", "https") or not url.hostname or url.username is not None:
            return "Unknown"
        host = url.hostname.rstrip(".").lower()
        if any(char.isspace() or char == "\\" for char in host) or len(host) > 253:
            return "Unknown"
        authority = f"[{host}]" if ":" in host else host
        default = 443 if url.scheme == "https" else 80
        if url.port is not None and url.port != default:
            authority += f":{url.port}"
        return f"{url.scheme}://{authority}"
    except ValueError:
        return "Unknown"


def device_type(agent):
    key = (agent or "").lower()
    if not key:
        return "Unknown"
    if any(marker in key for marker in ("smart-tv", "smarttv", "hbbtv", "tizen", "webos", "appletv", "roku")):
        return "TV"
    if "ipad" in key or "tablet" in key or ("android" in key and "mobile" not in key):
        return "Tablet"
    if any(marker in key for marker in ("iphone", "android", "mobile")):
        return "Phone"
    if any(marker in key for marker in ("windows", "macintosh", "x11", "linux", "chrome", "firefox")):
        return "Desktop"
    return "Unknown"


def permitted():
    return request.headers.get("DNT") != "1" and request.headers.get("Sec-GPC") != "1"


def observed_region(trusted_proxies):
    if not trusted_proxies:
        return None
    try:
        peer = ip_address(request.remote_addr)
    except ValueError:
        return None
    if peer not in trusted_proxies:
        return None
    code = request.headers.get("X-CuePorch-Region", "").strip().upper()
    return code if REGION_CODE.fullmatch(code) else None


def record(con, work, nonce, surface, body, context=None, *, viewer_key=None):
    if not isinstance(body, dict):
        abort(400, "Enter a playback observation.")
    seq, played, finished = body.get("sequence"), body.get("played"), body.get("completed")
    watched, intervals = body.get("watched"), body.get("intervals")
    if (type(seq) is not int or not 1 <= seq < 10**9 or type(watched) is not int
            or not 0 <= watched <= 4 * 3600 * 4000 or type(played) is not bool
            or type(finished) is not bool or not isinstance(intervals, list) or len(intervals) > 512):
        abort(422, "Invalid playback observation.")
    duration = work.get("duration_seconds")
    limit = (math.ceil(duration * 1000) + 2000) if duration else 7 * 86400 * 1000
    edges = []
    prior = 0
    for pair in intervals:
        if (not isinstance(pair, list) or len(pair) != 2 or any(type(value) is not int for value in pair)
                or not prior <= pair[0] < pair[1] <= limit):
            abort(422, "Invalid played segment.")
        edges.append(pair)
        prior = pair[1]
    watched = max(watched, sum(end - start for start, end in edges))
    if not played and (watched or finished):
        abort(422, "A play is required for viewing progress.")
    now = datetime.now(timezone.utc).timestamp()
    con.execute(
        """INSERT INTO video_analytics_session VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET sequence=excluded.sequence,played=max(played,excluded.played),
              completed=max(completed,excluded.completed),watched=max(watched,excluded.watched),
              intervals=excluded.intervals WHERE excluded.sequence>video_analytics_session.sequence""",
        (nonce, work["id"], video_version_key(work), surface, now, seq, played, finished, watched, json.dumps(edges)),
    )
    if context:
        source, device, *region = context
        con.execute("INSERT OR IGNORE INTO video_analytics_context VALUES(?,?,?)", (nonce, source, device))
        if region and region[0]:
            con.execute("INSERT OR IGNORE INTO video_analytics_region VALUES(?,?)", (nonce, region[0]))
    viewer = body.get("viewer")
    if viewer_key and isinstance(viewer, str) and re.fullmatch(r"[a-f0-9]{32}", viewer):
        identity = hmac.digest(viewer_key, b"cueporch-video-analytics:" + viewer.encode("ascii"), "sha256").hex()
        con.execute("INSERT OR IGNORE INTO video_analytics_viewer VALUES(?,?)", (nonce, identity))


def bounds(filters=None):
    filters = request.args if filters is None else filters
    today = datetime.now(timezone.utc).date()
    try:
        start = date.fromisoformat(filters["from"]) if "from" in filters else today - timedelta(days=29)
        end = date.fromisoformat(filters["to"]) if "to" in filters else today
        end_time = datetime.combine(end + timedelta(days=1), time.min, timezone.utc)
    except (TypeError, ValueError, OverflowError):
        abort(400, "Enter valid start and end dates.")
    if start > end:
        abort(400, "Choose an end date on or after the start.")
    start_time = datetime.combine(start, time.min, timezone.utc)
    return start, end, start_time.timestamp(), end_time.timestamp()


def metrics(row):
    impressions, views = row["impressions"], row["views"]
    return dict(impressions=impressions, views=views, finishes=row["finishes"],
                view_rate=views / impressions if impressions else 0,
                watched_seconds=row["watched"] / 1000)


def summarize(con, work, start, end, surface):
    clause = "WHERE work=? AND created>=? AND created<?"
    params = (work["id"], start, end)
    if surface:
        clause += " AND surface=?"
        params += (surface,)
    totals = metrics(con.execute(f"SELECT {COUNTS} FROM video_analytics_session {clause}", params).fetchone())
    days = con.execute(
        f"""SELECT date(created, 'unixepoch') AS day,surface,{COUNTS}
            FROM video_analytics_session {clause} GROUP BY day,surface ORDER BY day,surface""", params,
    )
    daily = [dict(day=row["day"], surface=row["surface"], **metrics(row)) for row in days]
    retention = []
    duration = work.get("duration_seconds")
    if duration and duration > 0:
        current = params + (video_version_key(work),)
        plays = con.execute(
            f"SELECT intervals FROM video_analytics_session {clause} AND version=? AND played=1", current,
        )
        bin_size = max(1, math.ceil(duration / 120))
        points = list(range(0, math.ceil(duration), bin_size))
        viewers = [0] * len(points)
        session_count = 0
        bucket_ms = 1000 * bin_size
        for row in plays:
            session_count += 1
            visited = set()
            for first, last in json.loads(row["intervals"]):
                visited.update(range(first // bucket_ms, min(len(points), (last - 1) // bucket_ms + 1)))
            for index in visited:
                viewers[index] += 1
        retention = [
            dict(second=second, percent=viewers[index] / session_count if session_count else 0)
            for index, second in enumerate(points)
        ]
    return dict(totals=totals, daily=daily, retention=retention, duration=duration,
                current=video_version_key(work))


def register(app, tx, staff, get_work, title_for):
    @app.get("/api/library/work/<uid>/analytics")
    def video_analytics(uid):
        staff()
        first, last, start, end = bounds()
        surface = request.args.get("surface", "")
        if surface not in ("", *SURFACES):
            abort(400, "Choose a playback surface.")
        with tx() as con:
            work = get_work(con, uid)
            if work.get("asset_type", "video") != "video":
                abort(404)
            result = summarize(con, work, start, end, surface)
            title = title_for(con, work)
        if request.args.get("format") == "csv":
            output = StringIO()
            writer = csv.writer(output)
            fields = ["day", "surface", "impressions", "views", "view_rate", "finishes", "watched_seconds"]
            writer.writerow(fields)
            writer.writerows([row[field] for field in fields] for row in result["daily"])
            return Response(output.getvalue(), mimetype="text/csv", headers={
                "Content-Disposition": "attachment; filename=cueporch-video-analytics.csv",
                "Cache-Control": "no-store",
            })
        return dict(result, title=title, date_from=first.isoformat(), date_to=last.isoformat(), surface=surface)
