"""Aggregate the institution's observed playback across videos."""

import csv
from io import StringIO
from itertools import accumulate
import json
import re

from flask import Response, abort, request

from catalog import video_version_key
from library_analytics import AUDIO_KINDS, CAPTION_KINDS, DEVICES, REGION_CODE, SURFACES, bounds, metrics
from library_registration import csv_cell
import media_bandwidth
from subtitles import LANGUAGE


DIMENSIONS = {
    "video": "s.work",
    "date": None,
    "device": "coalesce(s.device, 'Unknown')",
    "source": "coalesce(s.source, 'Unknown')",
    "region": "coalesce(s.region, 'Unknown')",
    "surface": "s.surface",
    "streaming": "CASE s.streaming WHEN 'live' THEN 'Live' WHEN 'replay' THEN 'Replay' ELSE 'On demand' END",
    "caption": "CASE tracks.caption_kind WHEN 'none' THEN 'Off' WHEN 'captions' THEN tracks.caption_language || ' · Captions'"
               " WHEN 'subtitles' THEN tracks.caption_language || ' · Subtitles'"
               " WHEN 'captions_ai' THEN tracks.caption_language || ' · Automated captions'"
               " WHEN 'subtitles_ai' THEN tracks.caption_language || ' · Automated subtitles' ELSE 'Unknown' END",
    "audio": "CASE tracks.audio_kind WHEN 'original' THEN tracks.audio_language || ' · Original audio'"
             " WHEN 'dubbed' THEN tracks.audio_language || ' · Dubbed audio'"
             " WHEN 'description' THEN tracks.audio_language || ' · Audio description'"
             " WHEN 'commentary' THEN tracks.audio_language || ' · Commentary'"
             " WHEN 'alternate' THEN tracks.audio_language || ' · Alternate audio' ELSE 'Unknown' END",
}
GROUP_REPORTS = ("date", "device", "source", "region")
TOP_DIMENSIONS = ("video", "region", "device", "source")
TRACK_DIMENSIONS = {"caption", "audio"}
COMMUNITY_UNAVAILABLE = {"device", "region", "source", *TRACK_DIMENSIONS}
TRACK_WATCHED = "min(s.watched,coalesce(tracks.watched,s.watched))"
PAIR_ROWS = """SELECT session,audio_language,audio_kind,caption_language,caption_kind,watched
    FROM video_analytics_track_pair
    UNION ALL
    SELECT session,'unknown','unknown',language,kind,watched FROM video_analytics_caption c
    WHERE NOT EXISTS (SELECT 1 FROM video_analytics_track_pair WHERE session=c.session)"""
PERIODS = {
    "day": "date(s.created, 'unixepoch')",
    "week": "date(s.created,'unixepoch','-'||((CAST(strftime('%w',s.created,'unixepoch') AS INTEGER)+6)%7)||' days')",
    "month": "strftime('%Y-%m-01',s.created,'unixepoch')",
    "year": "strftime('%Y-01-01',s.created,'unixepoch')",
}
METRICS = ("impressions", "views", "view_rate", "finishes", "watched_seconds", "average_watched_seconds",
           "unique_impressions", "unique_viewers", "average_watched_fraction", "downloads", "likes", "comments")
VIDEO_COUNTS = """coalesce(sum(s.impression),0) AS impressions, coalesce(sum(s.played),0) AS views,
    coalesce(sum(s.completed),0) AS finishes, coalesce(sum({watched}),0) AS watched,
    count(DISTINCT identity.viewer) AS unique_impressions,
    count(DISTINCT identity.viewer) FILTER (WHERE s.played=1) AS unique_viewers,
    count(identity.viewer) AS identified_impressions,
    count(identity.viewer) FILTER (WHERE s.played=1) AS identified_views,
    avg(cueporch_watched_fraction(s.work,s.version,s.duration_ms,{watched})) FILTER (WHERE s.played=1) AS average_watched_fraction,
    coalesce(sum(s.download),0) AS downloads,
    coalesce(sum(s.likes),0) AS likes, coalesce(sum(s.comments),0) AS comments"""
MILESTONES = (1, 25, 50, 75, 100)
PLAYBACK_ROWS = """SELECT s.id,s.work,s.version,s.surface,s.created,s.played,s.completed,s.watched,s.intervals,
    c.source,c.device,g.region,1 AS impression,0 AS download,'vod' AS streaming,NULL AS duration_ms,
    0 AS likes,0 AS comments
    FROM video_analytics_session s
    LEFT JOIN video_analytics_context c ON c.session=s.id
    LEFT JOIN video_analytics_region g ON g.session=s.id"""
DOWNLOAD_ROWS = """SELECT NULL,work,NULL,surface,created,0,0,0,NULL,source,device,region,0,1,'vod',NULL,0,0
    FROM video_analytics_download"""
EVENT_ROWS = """SELECT playback.id,playback.event,NULL,details.surface,details.created,playback.played,
    details.completed,playback.watched,NULL,details.source,details.device,details.region,1,0,playback.phase,details.duration_ms,0,0
    FROM live_playback_reporting details JOIN live_playback playback ON playback.id=details.session
    JOIN live_event event ON event.id=playback.event"""
LIKE_ROWS = """SELECT NULL,l.slug,NULL,'watch',l.liked,0,0,0,NULL,NULL,NULL,NULL,0,0,'vod',NULL,1,0
    FROM audience_like_reporting r JOIN audience_like l ON l.account=r.account AND l.kind=r.kind AND l.slug=r.slug
    WHERE cueporch_library_content(l.slug)"""
COMMENT_ROWS = """SELECT NULL,c.slug,NULL,'watch',r.published,0,0,0,NULL,NULL,NULL,NULL,0,0,'vod',NULL,0,1
    FROM audience_comment_reporting r JOIN audience_comment c ON c.id=r.comment JOIN action a ON a.id=c.approval
    WHERE a.status='eligible' AND r.published IS NOT NULL AND cueporch_library_content(c.slug)"""
ACTIVE_WORKS = " LEFT JOIN library_deleted_item d ON d.kind='work' AND d.item=s.work"
PLAYBACK_TABLES = f" FROM ({PLAYBACK_ROWS}) s{ACTIVE_WORKS}"
OBSERVED_ROWS = " UNION ALL ".join((PLAYBACK_ROWS, DOWNLOAD_ROWS, EVENT_ROWS))
VIDEO_TABLES = f" FROM ({OBSERVED_ROWS}) s{ACTIVE_WORKS}"
COMMUNITY_TABLES = f" FROM ({OBSERVED_ROWS} UNION ALL {LIKE_ROWS} UNION ALL {COMMENT_ROWS}) s{ACTIVE_WORKS}"


def row_metrics(row, community):
    values = metrics(row)
    return dict(values, average_watched_seconds=values["watched_seconds"] / values["views"] if values["views"] else 0,
                likes=row["likes"] if community else None, comments=row["comments"] if community else None,
                **{name: row[name] for name in ("unique_impressions", "unique_viewers", "identified_impressions",
                                                "identified_views", "average_watched_fraction", "downloads")})


def content_selection(options, include_events=False):
    selected = options.get("videos", [])
    content_id = r"(?:[a-f0-9-]{36}|[a-f0-9]{32})" if include_events else r"[a-f0-9-]{36}"
    if (not isinstance(selected, list) or len(selected) > 10000 or any(
            not isinstance(item, str) or not re.fullmatch(content_id, item) for item in selected)):
        abort(422, "Choose up to 10,000 library items.")
    return list(dict.fromkeys(selected))


def filter_settings(options, *, include_events=False):
    first, last, start, end = bounds(options)
    primary, secondary = options.get("group", "video"), options.get("breakdown", "")
    period = options.get("period", "day")
    surface, device, source, region = (options.get(name, "") for name in ("surface", "device", "source", "region"))
    if (not all(isinstance(value, str) for value in (primary, secondary, period, surface, device, source, region))
            or primary not in DIMENSIONS or secondary not in ("", *DIMENSIONS) or primary == secondary
            or period not in PERIODS or surface not in ("", *SURFACES) or device not in ("", *DEVICES)
            or len(source) > 270 or (region not in ("", "Unknown") and not REGION_CODE.fullmatch(region))):
        abort(422, "Choose valid report filters and distinct breakdowns.")
    selected = content_selection(options, include_events=include_events)
    clause = " WHERE s.created>=? AND s.created<? AND d.item IS NULL"
    params = [start, end]
    for dimension, value in (("surface", surface), ("device", device), ("source", source), ("region", region)):
        if value:
            clause += f" AND {DIMENSIONS[dimension]}=?"
            params.append(value)
    streaming = options.get("streaming", "") if include_events else ""
    if streaming not in ("", "vod", "live", "replay"):
        abort(422, "Choose a valid streaming type.")
    if streaming:
        clause += " AND s.streaming=?"
        params.append(streaming)
    if selected:
        clause += " AND s.work IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(selected))
    filters = dict(date_from=first.isoformat(), date_to=last.isoformat(), group=primary, breakdown=secondary,
                   period=period, surface=surface, device=device, source=source, region=region, videos=selected,
                   streaming=streaming)
    return filters, clause, tuple(params)


def caption_scope(language, kind, split):
    selected = {column: value for column, value in (("language", language), ("kind", kind)) if value}
    if not split and not selected:
        return "", "", (), (), "s.watched"
    if split:
        join = (" LEFT JOIN (SELECT session,language AS caption_language,kind AS caption_kind,watched"
                " FROM video_analytics_caption) tracks ON tracks.session=s.id")
        where = " AND s.impression=1" + "".join(f" AND coalesce(tracks.caption_{column},'unknown')=?" for column in selected)
        return join, where, (), tuple(selected.values()), TRACK_WATCHED
    if "unknown" in selected.values():
        where = (" AND s.impression=1 AND NOT EXISTS (SELECT 1 FROM video_analytics_caption WHERE session=s.id)"
                 if all(value == "unknown" for value in selected.values()) else " AND 0")
        return "", where, (), (), "s.watched"
    match = " AND ".join(f"{column}=?" for column in selected)
    join = (f" JOIN (SELECT session,sum(watched) AS watched FROM video_analytics_caption WHERE {match}"
            " GROUP BY session) tracks ON tracks.session=s.id")
    return join, "", tuple(selected.values()), (), TRACK_WATCHED


def pair_scope(selected, splits):
    if not selected and not splits:
        return "", "", (), (), "s.watched"
    groups = "session" + "".join(f",{split}_language,{split}_kind" for split in sorted(splits))
    match = " WHERE " + " AND ".join(f"{column}=?" for column in selected) if selected else ""
    allow_unmeasured = all(value == "unknown" for value in selected.values())
    join_type = "LEFT JOIN" if allow_unmeasured else "JOIN"
    join = (f" {join_type} (SELECT {groups},sum(watched) AS watched FROM ({PAIR_ROWS}){match}"
            f" GROUP BY {groups}) tracks ON tracks.session=s.id")
    where = " AND s.impression=1"
    if selected and allow_unmeasured:
        where += (" AND (tracks.session IS NOT NULL OR ("
                  " NOT EXISTS (SELECT 1 FROM video_analytics_track_pair WHERE session=s.id)"
                  " AND NOT EXISTS (SELECT 1 FROM video_analytics_caption WHERE session=s.id)))")
    return join, where, tuple(selected.values()), (), TRACK_WATCHED


def track_filters(options, dimension, kinds):
    language, kind = (options.get(f"{dimension}_{key}", "") for key in ("language", "kind"))
    if (not isinstance(language, str) or len(language) > 53 or (language and not LANGUAGE.fullmatch(language))
            or not isinstance(kind, str) or kind not in ("", *kinds)):
        abort(422, f"Choose a valid {dimension} track language and type.")
    return language.lower(), kind


def summarize(con, options, catalog, lengths, *, include_top=False, library_ids=None):
    filters, clause, params = filter_settings(options, include_events=True)
    language, kind = track_filters(options, "caption", CAPTION_KINDS)
    audio_language, audio_kind = track_filters(options, "audio", AUDIO_KINDS)
    track_options = dict(caption_language=language, caption_kind=kind, audio_language=audio_language, audio_kind=audio_kind)
    filters.update(track_options)
    selected = {key: value for key, value in track_options.items() if value}
    primary, secondary = filters["group"], filters["breakdown"]
    splits = TRACK_DIMENSIONS.intersection((primary, secondary))
    paired = "audio" in splits or audio_language or audio_kind

    def track_scope(grouped):
        return pair_scope(selected, grouped) if paired else caption_scope(language, kind, "caption" in grouped)

    community = not (selected or any(filters[name] for name in ("device", "region", "source")))
    identity_join = " LEFT JOIN video_analytics_viewer identity ON identity.session=s.id"
    base = (COMMUNITY_TABLES if community else VIDEO_TABLES) + identity_join
    join, where, before, after, watched = track_scope(set())
    scope, scope_params = base + join + clause + where, before + params + after
    counts = VIDEO_COUNTS.format(watched=watched)

    def watched_fraction(uid, version, measured_duration, watched):
        duration = measured_duration or lengths.get((uid, version))
        return watched / duration if duration else None

    con.create_function("cueporch_watched_fraction", 4, watched_fraction, deterministic=True)
    library_ids = catalog if library_ids is None else library_ids
    con.create_function("cueporch_library_content", 1, library_ids.__contains__, deterministic=True)
    period = PERIODS[filters["period"]]
    community_rows = community and not COMMUNITY_UNAVAILABLE.intersection((primary, secondary))
    group = DIMENSIONS[primary] or period
    detail = (DIMENSIONS[secondary] or period) if secondary else "''"
    totals = con.execute(f"SELECT {counts} {scope}", scope_params).fetchone()
    trend = con.execute(f"""SELECT {period} AS period,{counts} {scope}
                         GROUP BY period ORDER BY period""", scope_params)
    overview = [dict(period=row["period"], **row_metrics(row, community)) for row in trend]
    breakdown_scope, breakdown_params, breakdown_counts = scope, scope_params, counts
    if splits:
        split_join, split_where, split_before, split_after, split_watched = track_scope(splits)
        breakdown_scope = VIDEO_TABLES + identity_join + split_join + clause + split_where
        breakdown_params = split_before + params + split_after
        breakdown_counts = VIDEO_COUNTS.format(watched=split_watched)
    elif community and not community_rows:
        breakdown_scope = VIDEO_TABLES + identity_join + join + clause + where
    rows = con.execute(f"""SELECT {group} AS category,{detail} AS breakdown,{breakdown_counts} {breakdown_scope}
                        GROUP BY category,breakdown
                        ORDER BY views DESC,impressions DESC,downloads DESC,likes DESC,comments DESC,category,breakdown""", breakdown_params)
    items = []
    for row in rows:
        category, value = row["category"], row["breakdown"]
        items.append(dict(
            category=catalog.get(category, category) if primary == "video" else category,
            video=category if primary == "video" else value if secondary == "video" else None,
            breakdown=catalog.get(value, value) if secondary == "video" else value,
            **row_metrics(row, community_rows),
        ))
    result = dict(filters=filters, totals=row_metrics(totals, community), overview=overview, rows=items,
                  community_breakdown=community_rows)
    if include_top:
        result["top"] = {}
        for dimension in TOP_DIMENSIONS:
            leaders = con.execute(f"""SELECT {DIMENSIONS[dimension]} AS category,count(*) AS views
                                      {VIDEO_TABLES} {join} {clause} {where} AND s.played=1
                                      GROUP BY category ORDER BY views DESC,category LIMIT 5""", scope_params)
            result["top"][dimension] = [
                dict(label=catalog.get(category, category) if dimension == "video" else category,
                     video=category if dimension == "video" else None, views=views)
                for category, views in leaders
            ]
    return result


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
            if kind in GROUP_REPORTS or kind == "video":
                selected = options | {"group": kind} if kind in GROUP_REPORTS else options
                result = summarize(con, selected, titles | event_titles(con), durations(works), library_ids=titles,
                                   include_top=kind == "video" and request.args.get("format") != "csv")
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
