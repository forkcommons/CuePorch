"""Consented HubSpot Media Bridge sessions and playback delivery."""

import os
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken
from flask import abort

from library_analytics import source_origin

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_hubspot_media_view (
    session  TEXT PRIMARY KEY,
    work     TEXT NOT NULL,
    version  TEXT NOT NULL,
    revision TEXT NOT NULL,
    external TEXT NOT NULL,
    page_id  INTEGER NOT NULL,
    page_origin TEXT NOT NULL,
    consent  BLOB,
    expires  REAL NOT NULL,
    sequence INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS library_hubspot_media_event (
    session   TEXT NOT NULL,
    milestone INTEGER NOT NULL
              CHECK (milestone IN (-1, 0, 25, 50, 75, 100)),
    action    TEXT UNIQUE NOT NULL,
    state     TEXT NOT NULL,
    seen      REAL NOT NULL,
    due       REAL,
    PRIMARY KEY (session, milestone)
);

CREATE INDEX IF NOT EXISTS library_hubspot_media_event_state_due_idx
    ON library_hubspot_media_event (state, due);
"""


def available_milestones(intervals, duration_ms, played):
    """Return the tuple of milestone values reachable for a session.

    ``intervals`` is an already-validated, normalized list of
    non-overlapping integer ``(start, end)`` pairs (milliseconds).
    Each interval's endpoints are clamped to ``[0, duration_ms]`` and
    the unique summed play time is compared against the media
    duration to decide which percentage thresholds apply.

    Rules:
      * Not played -> ``()``.
      * Played but duration unknown -> ``(-1, 0)``.
      * Played with duration -> ``(-1, 0)`` followed by every reached
        threshold among ``25, 50, 75, 100``.
    """
    if not played:
        return ()

    if not duration_ms or duration_ms <= 0:
        return (-1, 0)

    total = 0
    for start, end in intervals:
        lo = start if start > 0 else 0
        hi = end if end < duration_ms else duration_ms
        if hi > lo:
            total += hi - lo

    milestones = [-1, 0]
    for threshold in (25, 50, 75, 100):
        # Integer arithmetic avoids float rounding at boundaries.
        if total * 100 >= threshold * duration_ms:
            milestones.append(threshold)

    return tuple(milestones)


STEP = "event_engagement_sync"


def grant(
    con,
    session,
    work,
    version,
    revision,
    external,
    page_id,
    page_origin,
    encrypted_consent,
    now,
):
    """Register a HubSpot media view grant for a session.

    Uses INSERT OR IGNORE so a withdrawn row or a prior session is never
    replaced. Returns True iff a new row was actually created.
    """
    cur = con.execute(
        "INSERT OR IGNORE INTO library_hubspot_media_view "
        "(session, work, version, revision, external, page_id, page_origin, consent, expires) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session,
            work,
            version,
            revision,
            external,
            page_id,
            page_origin,
            encrypted_consent,
            now + 4 * 3600,
        ),
    )
    return cur.rowcount > 0


def withdraw(con, session):
    """Clear view consent and cancel any pending work for this session."""
    con.execute(
        "UPDATE library_hubspot_media_view SET consent = NULL WHERE session = ?",
        (session,),
    )
    con.execute(
        "UPDATE action SET status = 'rejected', decision = 'canceled' "
        "WHERE status = 'waiting' AND id IN ("
        "SELECT action FROM library_hubspot_media_event "
        "WHERE session = ? AND state = 'queued')",
        (session,),
    )
    con.execute(
        "UPDATE library_hubspot_media_event "
        "SET state = 'canceled', due = NULL "
        "WHERE session = ? AND state = 'queued'",
        (session,),
    )


def observe(con, session, sequence, intervals, duration_ms, played, now, action):
    """Advance playback sequence once higher, then enqueue newly-earned milestones."""
    cur = con.execute(
        "UPDATE library_hubspot_media_view SET sequence = ? "
        "WHERE session = ? AND sequence < ? AND expires > ? "
        "AND consent IS NOT NULL AND consent <> ''",
        (sequence, session, sequence, now),
    )
    if cur.rowcount == 0:
        return
    for milestone in available_milestones(intervals, duration_ms, played):
        uid = uuid.uuid4().hex
        ins = con.execute(
            "INSERT OR IGNORE INTO library_hubspot_media_event "
            "(session, milestone, action, state, seen, due) "
            "VALUES (?, ?, ?, 'queued', ?, ?)",
            (session, milestone, uid, now, now),
        )
        if ins.rowcount > 0:
            action(
                con,
                uid,
                STEP,
                {
                    "session": session,
                    "milestone": milestone,
                    "destination": "HubSpot Media Bridge",
                },
            )


def eligible(con, work, page_origin, origins, connection, permits):
    import hubspot_media_bridge as provider
    import library_hubspot_bridge as bridge
    import library_hubspot_folders as folders
    import library_privacy

    if not work or page_origin not in origins:
        return None
    if not connection.configured:
        return None
    saved = connection.connection(con)
    if not saved:
        return None
    if not library_privacy.public_oembed_eligible(con, work, permits):
        return None
    row = bridge.get(con, work["id"])
    if not row:
        return None
    if (
        row["state"] != "published"
        or not row["remote"]
        or row["revision"] != saved["revision"]
    ):
        return None
    if row["media_type"] != provider.object_type():
        return None
    if folders.is_managed(con, work["id"]):
        selected = folders.plan(con)
        if not selected or not folders.active_member(
            con, work["id"], selected["folder"]
        ):
            return None
    return row


def next_event(con, now):
    return con.execute(
        """
SELECT e.*, a.status AS approval
FROM library_hubspot_media_event e
JOIN action a ON a.id=e.action
WHERE e.state='queued'
  AND e.due<=?
  AND a.status IN ('eligible','rejected')
  AND (e.milestone=-1 OR EXISTS (
    SELECT 1 FROM library_hubspot_media_event prior
    WHERE prior.session=e.session
      AND prior.milestone=-1
      AND prior.state<>'queued'
  ))
ORDER BY e.seen,e.session,e.milestone
LIMIT 1;
""",
        (now,),
    ).fetchone()


def current_view(con, event, now):

    row = con.execute(
        "SELECT e.state, a.status FROM library_hubspot_media_event e JOIN action a ON a.id=e.action WHERE e.action=?",
        (event["action"],),
    ).fetchone()
    if row is None or row[0] != "queued":
        abort(409)
    if row[1] == "waiting":
        abort(423)
    if row[1] != "eligible":
        abort(409)

    cur = con.execute(
        "SELECT * FROM library_hubspot_media_view WHERE session=?",
        (event["session"],),
    )
    view = cur.fetchone()
    if view is None:
        abort(409)

    if hasattr(view, "keys"):
        expires = view["expires"]
        consent = view["consent"]
    else:
        columns = [d[0] for d in cur.description]
        record = dict(zip(columns, view))
        expires = record["expires"]
        consent = record["consent"]

    if expires <= now or consent is None:
        abort(409)

    return view


def validate_view(con, view, connection, origins, permits, read_works):
    import catalog

    work = None
    for w in read_works():
        if w["id"] == view["work"]:
            work = w
            break

    if work is None:
        abort(409)

    if catalog.video_version_key(work) != view["version"]:
        abort(409)

    result = eligible(con, work, view["page_origin"], origins, connection, permits)
    if not result:
        abort(409)

    if result["external"] != view["external"] or result["revision"] != view["revision"]:
        abort(409)


def provider_payload(event, view, connection):
    try:
        contact = connection.cipher.decrypt(view["consent"]).decode("ascii")
    except (InvalidToken, UnicodeDecodeError):
        abort(409)

    payload = {
        "mediaType": "VIDEO",
        "occurredTimestamp": int(event["seen"] * 1000),
        "sessionId": event["session"],
        "externalId": view["external"],
        "pageId": view["page_id"],
        "contactUtk": contact,
    }

    if event["milestone"] == -1:
        payload["state"] = "STARTED"
        return ("media-played", payload)

    payload["playedPercent"] = event["milestone"]
    return ("media-played-percent", payload)


def deliver(tx, event, connection, origins, permits, read_works, stop, clock):
    from hubspot_media_bridge import send_media_event

    def verify(con):
        if stop.is_set():
            abort(423)

        view = current_view(con, event, clock())
        validate_view(con, view, connection, origins, permits, read_works)

        if event["milestone"] != -1:
            row = con.execute(
                "SELECT state FROM library_hubspot_media_event "
                "WHERE session=? AND milestone=-1",
                (event["session"],),
            ).fetchone()
            if row is None or row[0] != "sent":
                abort(409)

        return view

    with tx() as con:
        view = verify(con)
        kind, payload = provider_payload(event, view, connection)

    def before_write():
        with tx() as con:
            verify(con)

    send_media_event(
        connection,
        view["revision"],
        kind,
        payload,
        before_write=before_write,
    )


def run_one(tx, connection, origins, permits, read_works, stop, clock):
    from werkzeug.exceptions import HTTPException

    if stop.is_set():
        return False
    with tx() as con:
        now = clock()
        con.execute(
            "UPDATE library_hubspot_media_view SET consent=NULL WHERE expires<=? AND consent IS NOT NULL",
            (now,),
        )
        event = next_event(con, now)
        if event is None:
            return False
    try:
        deliver(tx, event, connection, origins, permits, read_works, stop, clock)
        state = "sent"
        due = None
    except HTTPException as exc:
        code = exc.code
        if code == 423:
            return False
        if code == 429:
            state = "queued"
            due = clock() + max(15, getattr(exc, "retry_after", 0) or 0)
        elif code == 409:
            state = "canceled"
            due = None
        elif code in (502, 503, 504):
            state = "unconfirmed"
            due = None
        else:
            state = "problem"
            due = None
    with tx() as con:
        con.execute(
            "UPDATE library_hubspot_media_event SET state=?, due=? WHERE action=? AND state='queued'",
            (state, due, event["action"]),
        )
    return True


def configured_origins() -> frozenset[str]:
    """Allowed CMS origins from CUEPORCH_HUBSPOT_CMS_ORIGINS.

    Entries must be bare origins: source_origin() round-trips exactly, which
    rejects URLs carrying a path, query, fragment, or credentials. HTTPS is
    always permitted; plain HTTP only for loopback hosts.
    """
    origins = set()
    for entry in os.getenv("CUEPORCH_HUBSPOT_CMS_ORIGINS", "").split(","):
        value = entry.strip()
        if source_origin(value) != value:
            continue
        if value.startswith("https://") or (
            value.startswith("http://")
            and urlsplit(value).hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            origins.add(value)
    return frozenset(origins)


def sample_fields(body, duration_ms):

    if not isinstance(body, dict):
        abort(422)

    sequence = body.get("sequence")
    if type(sequence) is not int:
        abort(422)
    if not (1 <= sequence <= 10**9 - 1):
        abort(422)

    played = body.get("played")
    if type(played) is not bool:
        abort(422)

    intervals = body.get("intervals")
    if not isinstance(intervals, list):
        abort(422)
    if len(intervals) > 512:
        abort(422)

    if not played and len(intervals) > 0:
        abort(422)

    if duration_ms > 0:
        limit = duration_ms + 2000
    else:
        limit = 7 * 24 * 60 * 60 * 1000  # 7 days in ms

    prior = 0
    for item in intervals:
        if not isinstance(item, list) or len(item) != 2:
            abort(422)
        begin, end = item[0], item[1]
        if type(begin) is not int or type(end) is not int:
            abort(422)
        if not (prior <= begin < end <= limit):
            abort(422)
        prior = end

    return (sequence, intervals, played)


def enable(con, connection, nonce, work, version, catalog, source, body, now):
    import re

    consent = body.get("consent")
    contactUtk = body.get("contactUtk")
    pageId = body.get("pageId")

    if consent is not True:
        abort(422)
    if (
        not isinstance(contactUtk, str)
        or re.fullmatch(r"[a-fA-F0-9]{32}", contactUtk) is None
    ):
        abort(422)
    if not isinstance(pageId, str) or re.fullmatch(r"[1-9][0-9]{0,18}", pageId) is None:
        abort(422)

    page_int = int(pageId)
    if page_int > 2**63 - 1:
        abort(422)

    encrypted = connection.cipher.encrypt(contactUtk.encode("ascii"))

    if not grant(
        con,
        nonce,
        work["id"],
        version,
        catalog["revision"],
        catalog["external"],
        page_int,
        source,
        encrypted,
        now,
    ):
        abort(409)
