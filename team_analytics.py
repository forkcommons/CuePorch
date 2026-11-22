"""Named viewing activity for opted-in team workspaces."""

from collections import Counter, defaultdict
import csv
from io import StringIO
from itertools import chain, groupby
import json
import secrets

from flask import Response, abort, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from browser_account_security import private, same_origin, secure_site
from catalog import video_version_key
import library_analytics


SCHEMA = """
CREATE TABLE IF NOT EXISTS team_analytics_setting(
 id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT);
CREATE TABLE IF NOT EXISTS team_analytics_view(
 session TEXT PRIMARY KEY, member TEXT NOT NULL, duration_ms INTEGER);
"""


def _progress_metrics(rows):
    net_ranges = []
    duration_ms = coverage_ms = measured_views = completions = views = watched_ms = 0
    average_fraction_sum = 0.0
    for row in rows:
        views += 1
        watched_ms += row["watched"]
        duration = row["duration_ms"] or 0
        intervals = json.loads(row['intervals'])
        if duration > 0:
            clipped = [[start, min(duration, end)] for start, end in intervals if start < duration]
            coverage = sum(end - start for start, end in clipped)
            net_ranges = library_analytics.merge_coverage(net_ranges, clipped)
            coverage_ms += coverage
            measured_views += 1
            average_fraction_sum += coverage / duration
            if coverage * 100 > duration * 95:
                completions += 1
        else:
            coverage_ms += sum(e - s for s, e in intervals)
        duration_ms = max(duration_ms, duration)
    return dict(net_ms=sum(end - start for start, end in net_ranges), duration_ms=duration_ms,
                coverage_ms=coverage_ms, average_fraction_sum=average_fraction_sum,
                measured_views=measured_views, completions=completions, views=views, watched_ms=watched_ms)


def report_progress(row):
    duration = row["duration_ms"]
    measured = row["measured_views"]
    row["net_seconds"] = row["net_ms"] / 1000
    row["net_fraction"] = row["net_ms"] / duration if duration else None
    row["avg_watched_fraction"] = row["average_fraction_sum"] / measured if measured else None
    row["covered_seconds"] = row["coverage_ms"] / 1000


def summarize_selection(results, group):
    acc = defaultdict(lambda: dict(
        views=0, completions=0, watched_seconds=0.0,
        net_ms=0, duration_ms=0, coverage_ms=0,
        average_fraction_sum=0.0, measured_views=0,
        members=set(), works=set(), label=None, removed=None))
    by_member = group == 'member'
    for r in results:
        key = r['member'] if by_member else r['work']
        a = acc[key]
        a['label'] = (r.get('name') or r.get('email')) if by_member else r.get('title')
        a['removed'] = r.get('removed') if by_member else None
        for k in ('views', 'completions', 'watched_seconds', 'net_ms',
                  'duration_ms', 'coverage_ms', 'average_fraction_sum', 'measured_views'):
            a[k] += r.get(k) or 0
        a['members'].add(r['member'])
        a['works'].add(r['work'])
    summary = []
    for key, a in acc.items():
        report_progress(a)
        summary.append(dict(
            id=key, label=a['label'] or str(key), removed=a['removed'],
            viewers=len(a['members']), videos=len(a['works']),
            views=a['views'], completions=a['completions'],
            watched_seconds=a['watched_seconds'],
            net_seconds=a['net_seconds'], net_fraction=a['net_fraction'],
            covered_seconds=a['covered_seconds'],
            avg_watched_fraction=a['avg_watched_fraction']))
    summary.sort(key=lambda x: (-x['views'], (x['label'] or '').casefold(), str(x['id'])))
    return summary


def generation(con):
    row = con.execute("SELECT generation FROM team_analytics_setting WHERE id=1").fetchone()
    return row["generation"] if row else None


def register(app, tx, origin, key, manager, member, access, mutation, note, read_works,
             title_for, spreadsheet_cell):
    signer = URLSafeTimedSerializer(key, salt="cueporch-team-playback")

    def playback_endpoint(con, work, actor):
        cycle = generation(con)
        if cycle and library_analytics.permitted():
            return url_for("observe_team_playback", token=signer.dumps(
                [work["id"], video_version_key(work), actor["id"], cycle, secrets.token_urlsafe(20)],
            ))
        return None

    @app.post("/api/team/playback/<token>")
    def observe_team_playback(token):
        secure = secure_site(origin)
        same_origin(secure)
        if not library_analytics.permitted():
            return "", 204
        try:
            uid, version, member_id, cycle, nonce = signer.loads(token, max_age=4 * 3600)
        except (BadSignature, ValueError):
            abort(404)
        request.max_content_length = 128 * 1024
        with tx() as con:
            actor = member(con, secure)
            if actor["id"] != member_id or cycle != generation(con):
                abort(404)
            work = access(con, actor, uid).get(uid)
            if work is None or video_version_key(work) != version:
                abort(404)
            library_analytics.record(con, work, nonce, "watch", request.get_json())
            duration = work.get("duration_seconds")
            con.execute("INSERT OR IGNORE INTO team_analytics_view VALUES(?,?,?)",
                        (nonce, member_id, round(duration * 1000) if duration else None))
        return "", 204

    @app.get("/team/analytics")
    def team_analytics_page():
        secure_site(origin)
        return private(render_template("team_analytics.html"))

    @app.route("/api/team/analytics/settings", methods=["GET", "PUT"])
    def team_analytics_settings():
        secure = secure_site(origin)
        same_origin(secure)
        with tx() as con:
            actor = manager(con)
            current = generation(con)
            if request.method == "PUT":
                enabled = mutation(secure).get("enabled")
                if type(enabled) is not bool:
                    abort(400, "Choose whether to record named viewing activity.")
                if enabled != bool(current):
                    current = secrets.token_urlsafe(20) if enabled else None
                    con.execute("""INSERT INTO team_analytics_setting VALUES(1,?)
                        ON CONFLICT(id) DO UPDATE SET generation=excluded.generation""", (current,))
                    note(con, actor, "team_analytics_enabled" if enabled else "team_analytics_disabled", "analytics")
            return private(dict(enabled=bool(current)))

    @app.get("/api/team/analytics")
    def team_analytics_report():
        secure = secure_site(origin)
        same_origin(secure)
        with tx() as con:
            actor = manager(con)
            if not generation(con):
                abort(403, "Named viewing activity is turned off.")
            first, last, start, end = library_analytics.bounds()
            member_filter = request.args.get("member")
            work_filter = request.args.get("video")
            works = {work["id"]: work for work in read_works()}
            group = request.args.get("group")
            if group not in (None, "member", "video"):
                abort(422, "Group Team analytics by member or video.")
            rows = con.execute("""
                SELECT m.id AS member, m.email, m.name, m.removed, s.work, s.version, t.duration_ms, s.watched, s.intervals
                FROM team_analytics_view t
                JOIN video_analytics_session s ON s.id = t.session
                JOIN team_member m ON m.id = t.member
                LEFT JOIN library_deleted_item d ON d.kind = 'work' AND d.item = s.work
                WHERE s.played = 1
                  AND d.item IS NULL
                  AND s.created >= ? AND s.created < ? AND s.created >= unixepoch('now','-5 years')
                  AND (? IS NULL OR m.id = ?) AND (? IS NULL OR s.work = ?)
                ORDER BY m.id, s.work, s.version
            """, (start, end, member_filter, member_filter, work_filter, work_filter))
            results = []
            for (_, work_id), member_work in groupby(rows, key=lambda row: (row["member"], row["work"])):
                if work_id not in works:
                    continue
                sample = next(member_work)
                restored = chain((sample,), member_work)
                counter = Counter()
                for _, cut in groupby(restored, key=lambda row: row["version"]):
                    counter.update(_progress_metrics(cut))
                row = {**counter, "member": sample["member"], "work": work_id,
                       "email": sample["email"], "name": sample["name"], "removed": sample["removed"],
                       "title": title_for(con, works[work_id]),
                       "watched_seconds": counter["watched_ms"] / 1000}
                report_progress(row)
                results.append(row)
            results.sort(key=lambda row: (-row["views"], row["name"], row["work"]))
            summary = summarize_selection(results, group) if group else []
            if request.args.get("format") == "csv":
                note(con, actor, "team_analytics_exported", "analytics")
        if request.args.get("format") == "csv":
            output = StringIO()
            writer = csv.writer(output)
            progress_columns = ("Net time completed (seconds)", "Net percent completed",
                                "Average percent watched", "Time covered by each view (seconds)")

            def percent(value):
                return f"{value:.2%}" if value is not None else ""

            def progress(row):
                return (row["net_seconds"], percent(row["net_fraction"]),
                        percent(row["avg_watched_fraction"]), row["covered_seconds"])

            if group and member_filter is None and work_filter is None:
                writer.writerow([group.capitalize(), "Viewers", "Videos", "Views", "Completions", *progress_columns])
                for row in summary:
                    label = row["label"] + (" (removed)" if row["removed"] else "")
                    writer.writerow([spreadsheet_cell(label), row["viewers"], row["videos"], row["views"],
                                     row["completions"], *progress(row)])
            else:
                writer.writerow(["Member", "Email", "Membership", "Video", "Views", "Completions",
                                 "Time watched (seconds)", *progress_columns])
                for row in results:
                    writer.writerow([spreadsheet_cell(row["name"]), spreadsheet_cell(row["email"]),
                                     "Removed" if row["removed"] else "Active", spreadsheet_cell(row["title"]),
                                     row["views"], row["completions"], row["watched_seconds"], *progress(row)])
            return private(Response(output.getvalue(), mimetype="text/csv", headers={
                "Content-Disposition": "attachment; filename=cueporch-team-analytics.csv",
            }))
        for row in results:
            for field in ("watched_ms", "net_ms", "duration_ms", "coverage_ms", "average_fraction_sum", "measured_views"):
                del row[field]
        return private(dict(date_from=first.isoformat(), date_to=last.isoformat(), rows=results,
                            group=group, summary=summary,
                            viewers=len({row["member"] for row in results}),
                            videos=len({row["work"] for row in results}), views=sum(row["views"] for row in results)))

    return playback_endpoint
