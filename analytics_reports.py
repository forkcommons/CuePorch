"""Aggregate the institution's observed playback across videos."""

import csv
from io import StringIO
from itertools import accumulate
import json
import re

from flask import Response, abort, request

from catalog import video_version_key
from library_analytics import COUNTS, DEVICES, REGION_CODE, SURFACES, bounds, metrics
from library_registration import csv_cell
import media_bandwidth


DIMENSIONS = {
    "video": "s.work",
    "date": None,
    "device": "coalesce(c.device, 'Unknown')",
    "source": "coalesce(c.source, 'Unknown')",
    "region": "coalesce(g.region, 'Unknown')",
    "surface": "s.surface",
}
GROUP_REPORTS = ("date", "device", "source", "region")
PERIODS = {
    "day": "date(s.created, 'unixepoch')",
    "week": "date(s.created,'unixepoch','-'||((CAST(strftime('%w',s.created,'unixepoch') AS INTEGER)+6)%7)||' days')",
    "month": "strftime('%Y-%m-01',s.created,'unixepoch')",
    "year": "strftime('%Y-01-01',s.created,'unixepoch')",
}
METRICS = ("impressions", "views", "view_rate", "finishes", "watched_seconds", "average_watched_seconds",
           "unique_impressions", "unique_viewers", "average_watched_fraction")
VIDEO_COUNTS = COUNTS + """,
    count(DISTINCT identity.viewer) AS unique_impressions,
    count(DISTINCT identity.viewer) FILTER (WHERE s.played=1) AS unique_viewers,
    count(identity.viewer) AS identified_impressions,
    count(identity.viewer) FILTER (WHERE s.played=1) AS identified_views,
    avg(cueporch_watched_fraction(s.work,s.version,s.watched)) FILTER (WHERE s.played=1) AS average_watched_fraction"""
MILESTONES = (1, 25, 50, 75, 100)
PLAYBACK_TABLES = """ FROM video_analytics_session s
    LEFT JOIN video_analytics_context c ON c.session=s.id
    LEFT JOIN video_analytics_region g ON g.session=s.id
    LEFT JOIN library_deleted_item d ON d.kind='work' AND d.item=s.work"""


def row_metrics(row):
    values = metrics(row)
    return dict(values, average_watched_seconds=values["watched_seconds"] / values["views"] if values["views"] else 0,
                **{name: row[name] for name in ("unique_impressions", "unique_viewers", "identified_impressions",
                                                "identified_views", "average_watched_fraction")})


def content_selection(options, include_events=False):
    selected = options.get("videos", [])
    content_id = r"(?:[a-f0-9-]{36}|[a-f0-9]{32})" if include_events else r"[a-f0-9-]{36}"
    if (not isinstance(selected, list) or len(selected) > 10000 or any(
            not isinstance(item, str) or not re.fullmatch(content_id, item) for item in selected)):
        abort(422, "Choose up to 10,000 library items.")
    return list(dict.fromkeys(selected))


def filter_settings(options):
    first, last, start, end = bounds(options)
    primary, secondary = options.get("group", "video"), options.get("breakdown", "")
    period = options.get("period", "day")
    surface, device, source, region = (options.get(name, "") for name in ("surface", "device", "source", "region"))
    if (not all(isinstance(value, str) for value in (primary, secondary, period, surface, device, source, region))
            or primary not in DIMENSIONS or secondary not in ("", *DIMENSIONS) or primary == secondary
            or period not in PERIODS or surface not in ("", *SURFACES) or device not in ("", *DEVICES)
            or len(source) > 270 or (region not in ("", "Unknown") and not REGION_CODE.fullmatch(region))):
        abort(422, "Choose valid report filters and distinct breakdowns.")
    selected = content_selection(options)
    clause = " WHERE s.created>=? AND s.created<? AND d.item IS NULL"
    params = [start, end]
    for dimension, value in (("surface", surface), ("device", device), ("source", source), ("region", region)):
        if value:
            clause += f" AND {DIMENSIONS[dimension]}=?"
            params.append(value)
    if selected:
        clause += " AND s.work IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(selected))
    filters = dict(date_from=first.isoformat(), date_to=last.isoformat(), group=primary, breakdown=secondary,
                   period=period, surface=surface, device=device, source=source, region=region, videos=selected)
    return filters, clause, tuple(params)


def summarize(con, options, catalog, lengths):
    filters, clause, params = filter_settings(options)
    scope = PLAYBACK_TABLES + " LEFT JOIN video_analytics_viewer identity ON identity.session=s.id" + clause

    def watched_fraction(uid, version, watched):
        duration = lengths.get((uid, version))
        return watched / duration if duration else None

    con.create_function("cueporch_watched_fraction", 3, watched_fraction, deterministic=True)
    period = PERIODS[filters["period"]]
    primary, secondary = filters["group"], filters["breakdown"]
    group = DIMENSIONS[primary] or period
    detail = (DIMENSIONS[secondary] or period) if secondary else "''"
    totals = con.execute(f"SELECT {VIDEO_COUNTS} {scope}", params).fetchone()
    trend = con.execute(f"""SELECT {period} AS period,{VIDEO_COUNTS} {scope}
                         GROUP BY period ORDER BY period""", params)
    overview = [dict(period=row["period"], **row_metrics(row)) for row in trend]
    rows = con.execute(f"""SELECT {group} AS category,{detail} AS breakdown,{VIDEO_COUNTS} {scope}
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


def durations(works):
    result = {}
    for work in works:
        for version in (work, *work.get("versions_history", ())):
            seconds = version.get("duration_seconds")
            if isinstance(seconds, (int, float)) and 0 < seconds <= 1_000_000_000:
                result[work["id"], video_version_key(version)] = max(1, round(1000 * seconds))
    return result


def engagement(con, options, catalog, lengths):
    _, clause, params = filter_settings(options)
    rows = con.execute("SELECT s.work,s.version,s.watched,s.completed,s.intervals" + PLAYBACK_TABLES +
                       clause + " AND s.played=1", params)
    curve = [0] * 101
    by_video = {}
    for row in rows:
        uid = row["work"]
        stats = by_video.setdefault(uid, dict(views=0, measured=0, finishes=0, fraction_watched=0.0,
                                             points=dict.fromkeys(MILESTONES, 0)))
        stats["views"] += 1
        duration = lengths.get((uid, row["version"]))
        intervals = json.loads(row["intervals"])
        if row["completed"] or (duration and intervals and intervals[-1][1] >= duration - 2000):
            stats["finishes"] += 1
        stats["points"][100] += row["completed"]
        curve[100] += row["completed"]
        if not duration:
            continue
        stats["measured"] += 1
        stats["fraction_watched"] += row["watched"] / duration
        for first, last in intervals:
            start = min(100, (first * 100 + duration - 1) // duration)
            stop = min(100, (last * 100 + duration - 1) // duration)
            curve[start] += 1
            curve[stop] -= 1
            for point in MILESTONES[:-1]:
                if start <= point < stop:
                    stats["points"][point] += 1

    def summary(stats):
        views, measured, finishes = (stats[name] for name in ("views", "measured", "finishes"))
        return dict(views=views, measured=measured, finishes=finishes, points=stats["points"],
                    average_watched=stats["fraction_watched"] / measured if measured else None,
                    completion=finishes / views if views else 0)

    totals = {name: sum(row[name] for row in by_video.values())
              for name in ("views", "measured", "finishes", "fraction_watched")}
    totals["points"] = {point: sum(row["points"][point] for row in by_video.values()) for point in MILESTONES}
    return dict(totals=summary(totals), retention=list(accumulate(curve)), rows=[
        dict(video=uid, title=catalog.get(uid, uid), **summary(stats))
        for uid, stats in sorted(by_video.items(), key=lambda item: (-item[1]["views"], catalog.get(item[0], item[0])))
    ])


def rating_summary(con, options, catalog):
    if any(options.get(name) for name in ("surface", "source", "device", "region")):
        abort(422, "Rating reports filter by video and date.")
    _, clause, params = filter_settings(options)
    rows = con.execute("""SELECT s.work,s.mode,s.score,count(*) AS votes FROM video_rating s
                       LEFT JOIN library_deleted_item d ON d.kind='work' AND d.item=s.work""" + clause +
                       " GROUP BY s.work,s.mode,s.score ORDER BY s.work,s.mode,s.score", params)
    by_video = {}
    totals = {"thumbs": [0, 0], "stars": [0] * 5}
    for row in rows:
        uid, mode, score, votes = (row[name] for name in ("work", "mode", "score", "votes"))
        counts = by_video.setdefault((uid, mode), [0] * len(totals[mode]))
        index = score if mode == "thumbs" else score - 1
        counts[index] = votes
        totals[mode][index] += votes

    def summary(mode, counts):
        responses = sum(counts)
        average = sum((index if mode == "thumbs" else index + 1) * value for index, value in enumerate(counts))
        return dict(mode=mode, votes=responses, scores=counts, average=average / responses if responses else None)

    return dict(totals=[summary(mode, counts) for mode, counts in totals.items()], rows=[
        dict(video=uid, title=catalog.get(uid, uid), **summary(mode, counts))
        for (uid, mode), counts in sorted(by_video.items(), key=lambda item: (catalog.get(item[0][0], item[0][0]), item[0][1]))
    ])


def csv_download(name, headings, rows):
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(headings)
    writer.writerows(rows)
    return Response(output.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=cueporch-{name}-report.csv", "Cache-Control": "no-store",
    })


def register(app, tx, staff, read_works):
    def catalog(con, works):
        renamed = dict(con.execute("SELECT work,title FROM library_title"))
        hidden = {row[0] for row in con.execute("SELECT item FROM library_deleted_item WHERE kind='work'")}
        return {work["id"]: renamed.get(work["id"], work["title"]) for work in works
                if work.get("asset_type", "video") == "video" and work["id"] not in hidden}

    def event_titles(con):
        return dict(con.execute("SELECT id,coalesce(json_extract(settings,'$.title'),id) FROM live_event"))

    @app.get("/api/analytics/videos")
    def analytics_videos():
        staff()
        with tx() as con:
            works = catalog(con, read_works())
            events = event_titles(con) if request.args.get("events") == "1" else {}
        entries = [dict(id=uid, title=title) for uid, title in works.items()]
        entries.extend(dict(id=uid, title=title, event=True) for uid, title in events.items())
        return sorted(entries, key=lambda item: (item["title"].casefold(), item["id"]))

    @app.post("/api/analytics/<any(video,date,device,source,region,engagement,rating,bandwidth):kind>-report")
    def report_view(kind):
        staff()
        request.max_content_length = 512 * 1024
        options = request.get_json()
        if not isinstance(options, dict):
            abort(422, "Enter valid report filters.")
        with tx() as con:
            works = read_works()
            titles = catalog(con, works)
            if kind in GROUP_REPORTS:
                result = summarize(con, options | {"group": kind}, titles, durations(works))
            elif kind == "video":
                result = summarize(con, options, titles, durations(works))
            elif kind == "engagement":
                result = engagement(con, options, titles, durations(works))
            elif kind == "bandwidth":
                first, last, _, _ = bounds(options)
                group = options.get("bandwidth_group", "month")
                if group not in ("month", "video"):
                    abort(422, "Group bandwidth by month or content.")
                result = media_bandwidth.report(con, first, last, content_selection(options, include_events=True),
                                                group, titles, event_titles(con))
            else:
                result = rating_summary(con, options, titles)
        if request.args.get("format") != "csv":
            return result
        if kind == "engagement":
            return csv_download(kind, ("video", "video_id", "views", "known_duration_views", "finishes", "completion_rate",
                                       "average_watched_fraction", *(f"views_at_{point}_percent" for point in MILESTONES)), (
                (csv_cell(row["title"]), row["video"], row["views"], row["measured"], row["finishes"], row["completion"],
                 row["average_watched"], *(row["points"][point] for point in MILESTONES))
                for row in [dict(result["totals"], title="Total", video=""), *result["rows"]]
            ))
        if kind == "rating":
            return csv_download(kind, ("video", "video_id", "rating_type", "votes", "thumbs_up_rate_or_average_stars",
                                       "thumbs_down", "thumbs_up", "1_star", "2_stars", "3_stars", "4_stars", "5_stars"), (
                (csv_cell(row["title"]), row["video"], row["mode"], row["votes"], row["average"],
                 *(row["scores"] + [None] * 5 if row["mode"] == "thumbs" else [None, None] + row["scores"]))
                for row in [*(dict(total, title="Total", video="") for total in result["totals"]), *result["rows"]]
            ))
        if kind == "bandwidth":
            return csv_download(kind, (result["group"], "content_id", "live_bytes", "on_demand_bytes", "total_bytes"), (
                (csv_cell(row["label"]), row["item"] or "", row["live_bytes"], row["vod_bytes"], row["total_bytes"])
                for row in [dict(result["totals"], label="Total", item=None), *result["rows"]]
            ))
        filters = result["filters"]
        return csv_download(kind, (filters["group"], filters["breakdown"] or "breakdown", "video_id", *METRICS), (
            (csv_cell(row["category"]), csv_cell(row["breakdown"]), row["video"] or "", *(row[field] for field in METRICS))
            for row in result["rows"]
        ))
