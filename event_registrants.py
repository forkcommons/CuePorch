"""Private registration records for individual entries on a live event calendar."""

import csv
from datetime import datetime, timezone
from io import StringIO
import json
from time import time
import uuid

from flask import Response, abort, request

from browser_account_security import private
import event_webhooks
import library_analytics
from library_registration import UTM_FIELDS, csv_cell


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_event_registrant(
 id TEXT PRIMARY KEY, event TEXT NOT NULL, opens INTEGER NOT NULL, email_key TEXT NOT NULL,
 email TEXT NOT NULL, registered REAL, source TEXT NOT NULL, answers TEXT NOT NULL,
 UNIQUE(event,opens,email_key));
CREATE INDEX IF NOT EXISTS live_event_registrant_calendar ON live_event_registrant(event,opens,registered DESC,id);
CREATE TABLE IF NOT EXISTS live_attendee_sharing(
 id TEXT PRIMARY KEY, registrant TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS live_attendee_sharing_registrant ON live_attendee_sharing(registrant,revoked);
CREATE TABLE IF NOT EXISTS live_attendee_session(session TEXT PRIMARY KEY, sharing TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS live_attendee_session_consent ON live_attendee_session(sharing);
CREATE TABLE IF NOT EXISTS live_attendee_replay_coverage(
 session TEXT PRIMARY KEY, sharing TEXT NOT NULL, source TEXT NOT NULL,
 sequence INTEGER NOT NULL, duration_ms INTEGER NOT NULL, intervals TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS live_attendee_replay_coverage_sharing ON live_attendee_replay_coverage(sharing);
"""
PAGE = 50


def initialize(con):
    con.execute("""INSERT OR IGNORE INTO live_event_registrant(id,event,opens,email_key,email,registered,source,answers)
                   SELECT min(l.id),l.event,l.opens,l.email_key,r.email,NULL,'retained-link',r.answers
                   FROM live_attendance_link l JOIN viewer_registrant r
                   ON r.kind='event' AND r.subject=l.event AND r.email_key=l.email_key
                   GROUP BY l.event,l.opens,l.email_key""")


def record(con, uid, opens, address, answers, source):
    found = con.execute("""SELECT id,email,answers FROM live_event_registrant WHERE event=? AND opens=? AND email_key=?""",
                        (uid, opens, address.casefold())).fetchone()
    encoded = json.dumps(answers, sort_keys=True)
    if found is None:
        registrant = uuid.uuid4().hex
        con.execute("INSERT INTO live_event_registrant VALUES(?,?,?,?,?,?,?,?)",
                    (registrant, uid, opens, address.casefold(), address, time(), source, encoded))
        event_webhooks.enqueue(con, "registration-created", uid, opens, registrant, dict(registrant=registrant))
        return registrant, True
    elif source != "form" and (found["email"] != address or json.loads(found["answers"]) != answers):
        con.execute("UPDATE live_event_registrant SET email=?,answers=? WHERE id=?", (address, encoded, found["id"]))
        event_webhooks.enqueue(con, "registration-updated", uid, opens, uuid.uuid4().hex, dict(registrant=found["id"]))
    if source != "hubspot-scheduled":
        cur = con.execute("DELETE FROM live_event_hubspot_member WHERE registrant=?", (found["id"],))
        if cur.rowcount:
            con.execute("UPDATE live_event_registrant SET source=? WHERE id=?", (source, found["id"]))
    return found["id"], False


def share(con, registrant):
    uid = uuid.uuid4().hex
    con.execute("INSERT INTO live_attendee_sharing(id,registrant) VALUES(?,?)", (uid, registrant))
    return uid


def sharing_active(con, uid, event, opens):
    return uid and con.execute("""SELECT 1 FROM live_attendee_sharing s JOIN live_event_registrant r ON r.id=s.registrant
                                  WHERE s.id=? AND s.revoked=0 AND r.event=? AND r.opens=?""",
                               (uid, event, opens)).fetchone() is not None


def stop_sharing(con, uid):
    con.execute("UPDATE live_attendee_sharing SET revoked=1 WHERE id=?", (uid,))
    con.execute("DELETE FROM live_attendee_session WHERE sharing=?", (uid,))
    con.execute("DELETE FROM live_attendee_replay_coverage WHERE sharing=?", (uid,))


def link_sample(con, session, uid):
    con.execute("INSERT OR IGNORE INTO live_attendee_session VALUES(?,?)", (session, uid))


def record_replay_coverage(con, session, sharing, source, sequence, duration_ms, ranges):
    if type(duration_ms) is not int or duration_ms <= 0:
        return
    if not isinstance(ranges, list) or len(ranges) > 512:
        abort(400)
    kept, prev_last = [], 0
    for pair in ranges:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            abort(400)
        first, last = pair
        if type(first) is not int or type(last) is not int:
            abort(400)
        if first < 0 or first >= last or last > duration_ms + 2000:
            abort(400)
        if first < prev_last:
            abort(400)
        clip = min(last, duration_ms)
        if first >= clip:
            continue
        kept.append([first, clip])
        prev_last = clip
    payload = json.dumps(kept, separators=(",", ":"))
    con.execute(
        "INSERT INTO live_attendee_replay_coverage(session,sharing,source,sequence,duration_ms,intervals) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(session) DO UPDATE SET "
        "sharing=excluded.sharing,source=excluded.source,sequence=excluded.sequence,"
        "duration_ms=excluded.duration_ms,intervals=excluded.intervals "
        "WHERE excluded.sequence > live_attendee_replay_coverage.sequence",
        (session, sharing, source, sequence, duration_ms, payload),
    )


def replay_coverage(con, registrant):
    rows = con.execute(
        """
        SELECT c.duration_ms, c.intervals
        FROM live_attendee_replay_coverage c
        JOIN live_attendee_sharing s ON s.id=c.sharing
        JOIN live_event_registrant r ON r.id=s.registrant
        JOIN live_occurrence o ON o.event=r.event AND o.opens=r.opens
        WHERE s.registrant = ?
          AND s.revoked = 0
          AND o.delivery = 'replay'
          AND c.source = o.remote
        ORDER BY c.duration_ms DESC
        """,
        (registrant,),
    ).fetchall()

    if not rows:
        return None

    duration = rows[0][0]
    coverage = []

    for measured, intervals in rows:
        if measured != duration:
            continue
        coverage = library_analytics.merge_coverage(
            coverage, json.loads(intervals)
        )

    return sum(last - first for first, last in coverage) / duration


def watch_time(con, registrant):
    live, replay, plays = con.execute("""SELECT sum(CASE p.phase WHEN 'live' THEN p.watched ELSE 0 END),
                                         sum(CASE p.phase WHEN 'replay' THEN p.watched ELSE 0 END),sum(p.played)
                                        FROM live_attendee_sharing s JOIN live_attendee_session v ON v.sharing=s.id
                                        JOIN live_playback p ON p.id=v.session WHERE s.registrant=? AND s.revoked=0""",
                                     (registrant,)).fetchone()
    return (dict(live_seconds=live / 1000, replay_seconds=replay / 1000, plays=plays,
                 replay_watched_fraction=replay_coverage(con, registrant)) if live is not None else None)


def describe(row, con=None):
    return dict(id=row["id"], occurrence=row["opens"], email=row["email"], registered=row["registered"],
                source=row["source"], answers=json.loads(row["answers"]),
                watch_time=watch_time(con, row["id"]) if con else None)


def rows(con, uid, opens, filter_text="", offset=0, limit=-1):
    return con.execute("""SELECT id,opens,email,registered,source,answers FROM live_event_active_registrant
                       WHERE event=? AND opens=? AND instr(email_key,?)>0 ORDER BY registered DESC,id LIMIT ? OFFSET ?""",
                       (uid, opens, filter_text, limit, offset))


def export(con, uid, opens):
    entries = [describe(row, con) for row in rows(con, uid, opens)]
    keys = ["first_name", "last_name", *UTM_FIELDS]
    keys += sorted({key for entry in entries for key in entry["answers"] if key.startswith("field_")})
    output = StringIO()
    writer = csv.writer(output)
    tracking = ["live_seconds", "replay_seconds", "plays", "replay_watched_fraction"]
    writer.writerow(["email", "occurrence", "registration_date_utc", "registration_source", *keys, *tracking])
    for entry in entries:
        registered = entry["registered"]
        date = datetime.fromtimestamp(registered, timezone.utc).isoformat() if registered is not None else ""
        values = [entry["email"], opens, date, entry["source"], *(entry["answers"].get(key, "") for key in keys)]
        watch = entry["watch_time"] or {}
        values += ["" if watch.get(key) is None else watch[key] for key in tracking]
        writer.writerow(csv_cell(value) for value in values)
    return Response(output.getvalue(), mimetype="text/csv", headers={
        "Cache-Control": "no-store", "Content-Disposition": f"attachment; filename=cueporch-attendees-{opens}.csv",
    })


def register(app, tx, staff, read, selected_open):
    @app.get("/api/live-events/<uid>/<int:opens>/registrants")
    def list_event_registrants(uid, opens):
        staff()
        search = request.args.get("email", "").strip().casefold()
        offset = request.args.get("offset", "0")
        if len(search) > 254 or not offset.isascii() or not offset.isdecimal() or len(offset) > 9:
            abort(422, "Use an email filter and a valid page offset.")
        offset = int(offset)
        with tx() as con:
            _, event = read(con, uid)
            selected_open(event, opens)
            matching = con.execute("""SELECT count(*) FROM live_event_active_registrant WHERE event=? AND opens=?
                                      AND instr(email_key,?)>0""", (uid, opens, search)).fetchone()[0]
            attendees = [describe(row, con) for row in rows(con, uid, opens, search, offset, PAGE)]
        return private(dict(occurrence=opens, attendees=attendees, total=matching, offset=offset, page_size=PAGE))

    @app.get("/api/live-events/<uid>/<int:opens>/registration/export")
    def export_event_occurrence_registrants(uid, opens):
        staff()
        with tx() as con:
            _, event = read(con, uid)
            selected_open(event, opens)
            return export(con, uid, opens)
