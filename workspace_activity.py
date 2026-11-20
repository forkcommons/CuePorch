"""Recent workspace activity with current access checked before revealing an item."""

import math
import time

from flask import abort, request, url_for

from browser_account_security import json_mutation, private, same_origin, secure_site
from team_accounts import MANAGERS, SESSION_MARKER, actor_id

RETENTION = 30 * 86400
PAGE_SIZE = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_activity_read(
 actor TEXT NOT NULL, item TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(actor,item));
CREATE INDEX IF NOT EXISTS workspace_activity_read_at ON workspace_activity_read(at);
CREATE INDEX IF NOT EXISTS workspace_activity_shared ON team_membership_event(subject,action,at);
CREATE INDEX IF NOT EXISTS workspace_activity_share_latest ON team_membership_event(subject,action,detail,sequence);
CREATE INDEX IF NOT EXISTS workspace_activity_help ON help_request_event(at);
"""

ACTIVITY = """
WITH activity(id, kind, resource, title, action, at) AS (
    SELECT 'support:' || e.id, 'support', r.id, r.subject, e.kind, e.at
    FROM help_request_event e JOIN help_request r ON r.id=e.request
    WHERE e.at>=:cutoff AND (:manager OR r.requester=:actor)
    UNION ALL
    SELECT 'share:' || e.sequence, 'share', e.detail, '', '', e.at
    FROM team_membership_event e JOIN team_work_grant g ON g.member=e.subject AND g.work=e.detail
    WHERE e.subject=:member AND e.action='video_shared' AND e.at>=:cutoff
      AND e.sequence=(SELECT max(recent.sequence) FROM team_membership_event recent
          WHERE recent.subject=e.subject AND recent.action='video_shared' AND recent.detail=e.detail)
    UNION ALL
    SELECT 'event:' || id, 'event', id, json_extract(settings,'$.title'), '',
        (julianday(created)-2440587.5)*86400
    FROM live_event WHERE :manager AND julianday(created)>=2440587.5+:cutoff/86400
)
SELECT a.*, seen.item IS NOT NULL AS read
FROM activity a LEFT JOIN workspace_activity_read seen ON seen.actor=:actor AND seen.item=a.id
"""


def register(app, tx, origin, team_member, operator_name, team_works, title_for):
    def materialize(con, rows, member):
        works = None
        for row in rows:
            kind = row["kind"]
            resource = row["resource"]
            title = row["title"]
            if kind == "share":
                if works is None:
                    works = team_works(con, member)
                work = works.get(resource)
                if not work:
                    continue
                title = title_for(con, work)
                url = url_for("team_watch", work=resource)
                label = "Video shared with you"
            elif kind == "support":
                url = url_for("help_requests_page", request=resource, team=1 if member else None)
                label = {"message": "Support message", "resolved": "Request resolved",
                         "reopened": "Request reopened"}[row["action"]]
            else:
                url = url_for("live_event_dashboard", event=resource)
                label = "Live event created"
            yield dict(id=row["id"], title=title, label=label, url=url, at=row["at"], read=bool(row["read"]))

    @app.route("/api/team/activity", methods=["GET", "PUT"])
    def workspace_activity_api():
        secure = secure_site(origin)
        if request.method == "GET":
            same_origin(secure)
            cursor_at = request.args.get("at", type=float)
            cursor_id = request.args.get("before")
            if "at" in request.args or cursor_id is not None:
                if cursor_at is None or not math.isfinite(cursor_at) or not cursor_id or len(cursor_id) > 80:
                    abort(400, "The activity page address is invalid.")
            body = None
        else:
            body = json_mutation(secure, "X-CuePorch-Team")
        with tx() as con:
            member = None
            if request.headers.get("Authorization") == f"Bearer {SESSION_MARKER}":
                member = team_member(con)
                if not member:
                    abort(401, "Sign in to your team.")
                actor, manager = actor_id(member), member["role"] in MANAGERS
            else:
                actor = operator_name(con)
                if not actor:
                    abort(403, "Enter an operator credential.")
                actor, manager = "operator:" + actor, True
            cutoff = time.time() - RETENTION
            params = dict(actor=actor, manager=manager, member=member["id"] if member else None, cutoff=cutoff)
            if body is not None:
                uid = body.get("id")
                if not isinstance(uid, str) or not 1 <= len(uid) <= 80:
                    abort(400, "Choose an activity item.")
                rows = con.execute(ACTIVITY + " WHERE a.id=:id", params | dict(id=uid))
                visible = next(materialize(con, rows, member), None)
                if not visible:
                    abort(404, "This activity is unavailable.")
                con.execute("DELETE FROM workspace_activity_read WHERE at<?", (cutoff,))
                con.execute("INSERT OR IGNORE INTO workspace_activity_read VALUES(?,?,?)", (actor, uid, visible["at"]))
                return private("", 204)
            rows = con.execute(ACTIVITY + """
                WHERE (:at IS NULL OR (a.at,a.id)<(:at,:before))
                ORDER BY a.at DESC,a.id DESC""", params | dict(at=cursor_at, before=cursor_id))
            items = []
            more = False
            for item in materialize(con, rows, member):
                if len(items) == PAGE_SIZE:
                    more = True
                    break
                items.append(item)
            return private(dict(items=items, more=more))
