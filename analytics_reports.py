"""Aggregate the institution's observed playback across videos."""

import csv
from io import StringIO
import json
import re

from flask import Response, abort, request

from library_analytics import COUNTS, DEVICES, SURFACES, bounds, metrics
from library_registration import csv_cell


DIMENSIONS = {
    "video": "s.work",
    "date": None,
    "device": "coalesce(c.device, 'Unknown')",
    "source": "coalesce(c.source, 'Unknown')",
    "surface": "s.surface",
}
PERIODS = {
    "day": "date(s.created, 'unixepoch')",
    "week": "date(s.created,'unixepoch','-'||((CAST(strftime('%w',s.created,'unixepoch') AS INTEGER)+6)%7)||' days')",
    "month": "strftime('%Y-%m-01',s.created,'unixepoch')",
    "year": "strftime('%Y-01-01',s.created,'unixepoch')",
}
METRICS = ("impressions", "views", "view_rate", "finishes", "watched_seconds", "average_watched_seconds")


def row_metrics(row):
    values = metrics(row)
    return dict(values, average_watched_seconds=values["watched_seconds"] / values["views"] if values["views"] else 0)


def filter_settings(options):
    first, last, start, end = bounds(options)
    primary, secondary = options.get("group", "video"), options.get("breakdown", "")
    period = options.get("period", "day")
    surface, device, source = (options.get(name, "") for name in ("surface", "device", "source"))
    if (not all(isinstance(value, str) for value in (primary, secondary, period, surface, device, source))
            or primary not in DIMENSIONS or secondary not in ("", *DIMENSIONS) or primary == secondary
            or period not in PERIODS or surface not in ("", *SURFACES) or device not in ("", *DEVICES)
            or len(source) > 270):
        abort(422, "Choose valid report filters and distinct breakdowns.")
    selected = options.get("videos", [])
    if (not isinstance(selected, list) or len(selected) > 10000 or any(
            not isinstance(item, str) or not re.fullmatch(r"[a-f0-9-]{36}", item) for item in selected)):
        abort(422, "Choose up to 10,000 library videos.")
    selected = list(dict.fromkeys(selected))
    clause = " WHERE s.created>=? AND s.created<? AND d.item IS NULL"
    params = [start, end]
    for dimension, value in (("surface", surface), ("device", device), ("source", source)):
        if value:
            clause += f" AND {DIMENSIONS[dimension]}=?"
            params.append(value)
    if selected:
        clause += " AND s.work IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(selected))
    filters = dict(date_from=first.isoformat(), date_to=last.isoformat(), group=primary, breakdown=secondary,
                   period=period, surface=surface, device=device, source=source, videos=selected)
    return filters, clause, tuple(params)


def summarize(con, options, catalog):
    filters, clause, params = filter_settings(options)
    tables = """ FROM video_analytics_session s
        LEFT JOIN video_analytics_context c ON c.session=s.id
        LEFT JOIN library_deleted_item d ON d.kind='work' AND d.item=s.work"""
    scope = tables + clause
    period = PERIODS[filters["period"]]
    primary, secondary = filters["group"], filters["breakdown"]
    group = DIMENSIONS[primary] or period
    detail = (DIMENSIONS[secondary] or period) if secondary else "''"
    totals = con.execute(f"SELECT {COUNTS} {scope}", params).fetchone()
    trend = con.execute(f"""SELECT {period} AS period,{COUNTS} {scope}
                         GROUP BY period ORDER BY period""", params)
    overview = [dict(period=row["period"], **row_metrics(row)) for row in trend]
    rows = con.execute(f"""SELECT {group} AS category,{detail} AS breakdown,{COUNTS} {scope}
                        GROUP BY category,breakdown ORDER BY views DESC,impressions DESC,category,breakdown""", params)
    items = []
    for row in rows:
        category, value = row["category"], row["breakdown"]
        items.append(dict(
            category=catalog.get(category, category) if primary == "video" else category,
            video=category if primary == "video" else value if secondary == "video" else None,
            breakdown=catalog.get(value, value) if secondary == "video" else value,
            **row_metrics(row),
        ))
    return dict(filters=filters, totals=row_metrics(totals), overview=overview, rows=items)


def register(app, tx, staff, read_works):
    def catalog(con):
        renamed = dict(con.execute("SELECT work,title FROM library_title"))
        hidden = {row[0] for row in con.execute("SELECT item FROM library_deleted_item WHERE kind='work'")}
        return {work["id"]: renamed.get(work["id"], work["title"]) for work in read_works()
                if work.get("asset_type", "video") == "video" and work["id"] not in hidden}

    @app.get("/api/analytics/videos")
    def analytics_videos():
        staff()
        with tx() as con:
            works = catalog(con)
        return [dict(id=uid, title=title) for uid, title in
                sorted(works.items(), key=lambda item: (item[1].casefold(), item[0]))]

    @app.post("/api/analytics/video-report")
    def video_report():
        staff()
        request.max_content_length = 512 * 1024
        options = request.get_json()
        if not isinstance(options, dict):
            abort(422, "Enter valid report filters.")
        with tx() as con:
            result = summarize(con, options, catalog(con))
        if request.args.get("format") != "csv":
            return result
        output = StringIO()
        writer = csv.writer(output)
        filters = result["filters"]
        writer.writerow((filters["group"], filters["breakdown"] or "breakdown", "video_id", *METRICS))
        writer.writerows([
            csv_cell(row["category"]), csv_cell(row["breakdown"]), row["video"] or "",
            *(row[field] for field in METRICS),
        ] for row in result["rows"])
        return Response(output.getvalue(), mimetype="text/csv", headers={
            "Content-Disposition": "attachment; filename=cueporch-video-report.csv",
            "Cache-Control": "no-store",
        })
