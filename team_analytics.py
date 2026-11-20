"""Named viewing activity for opted-in team workspaces."""

import csv
from io import StringIO
import secrets

from flask import Response, abort, render_template, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from browser_account_security import private, same_origin, secure_site
from catalog import video_version_key
import library_analytics
from library_trash import hidden


SCHEMA = """
CREATE TABLE IF NOT EXISTS team_analytics_setting(
 id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT);
CREATE TABLE IF NOT EXISTS team_analytics_view(
 session TEXT PRIMARY KEY, member TEXT NOT NULL, duration_ms INTEGER);
"""


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
            rows = con.execute("""
                SELECT m.id AS member,m.email,m.name,m.removed,s.work,count(*) AS views,
                  sum(watched) AS watched_ms,
                  sum(CASE WHEN t.duration_ms>0 AND coalesce((
                    SELECT sum(json_extract(value,'$[1]')-json_extract(value,'$[0]'))
                    FROM json_each(s.intervals)),0)>0.95*t.duration_ms THEN 1 ELSE 0 END) AS completions
                FROM team_analytics_view t JOIN video_analytics_session s ON s.id=t.session
                JOIN team_member m ON m.id=t.member
                WHERE s.played=1 AND s.created>=? AND s.created<? AND s.created>=unixepoch('now','-5 years')
                  AND (? IS NULL OR m.id=?) AND (? IS NULL OR s.work=?)
                GROUP BY m.id,s.work ORDER BY views DESC,m.name,s.work
            """, (start, end, member_filter, member_filter, work_filter, work_filter))
            results = [dict(row, title=title_for(con, works[row["work"]]), watched_seconds=row["watched_ms"] / 1000)
                       for row in rows if row["work"] in works and not hidden(con, "work", row["work"])]
            if request.args.get("format") == "csv":
                note(con, actor, "team_analytics_exported", "analytics")
        if request.args.get("format") == "csv":
            output = StringIO()
            writer = csv.writer(output)
            writer.writerow(["Member", "Email", "Membership", "Video", "Views", "Completions", "Time watched (seconds)"])
            for row in results:
                writer.writerow([spreadsheet_cell(row["name"]), spreadsheet_cell(row["email"]),
                                 "Removed" if row["removed"] else "Active", spreadsheet_cell(row["title"]),
                                 row["views"], row["completions"], row["watched_seconds"]])
            return private(Response(output.getvalue(), mimetype="text/csv", headers={
                "Content-Disposition": "attachment; filename=cueporch-team-analytics.csv",
            }))
        for row in results:
            del row["watched_ms"]
        return private(dict(date_from=first.isoformat(), date_to=last.isoformat(), rows=results,
                            viewers=len({row["member"] for row in results}),
                            videos=len({row["work"] for row in results}), views=sum(row["views"] for row in results)))

    return playback_endpoint
