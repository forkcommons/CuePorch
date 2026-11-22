"""Link a finished live occurrence to an exact migrated Library recording."""

import math
import re
import uuid

import event_webhooks
from library_privacy import playback


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_imported_replay(
 event TEXT NOT NULL, opens INTEGER NOT NULL, remote TEXT NOT NULL,
 work TEXT NOT NULL, digest TEXT NOT NULL, approval TEXT NOT NULL,
 PRIMARY KEY(event,opens));
"""
DIGEST = re.compile(r"[0-9a-f]{64}")


def read(con, uid, opens):
    return con.execute("SELECT * FROM live_imported_replay WHERE event=? AND opens=?", (uid, opens)).fetchone()


def source(root, work, digest=None):
    if work["provider"] != "vimeo" or work.get("asset_type", "video") != "video":
        return None
    cuts = (work, *work.get("versions_history", [])) if digest is not None else (work,)
    for cut in cuts:
        file = playback(cut)
        if file is None:
            continue
        sha = file["sha256"]
        if not DIGEST.fullmatch(sha) or (digest is not None and sha != digest):
            continue
        path = root / "objects" / sha
        if not path.is_file():
            continue
        duration = cut.get("duration_seconds")
        if type(duration) not in (int, float) or not math.isfinite(duration) or not .001 <= duration <= 7 * 86400:
            duration = None
        return dict(path=path, digest=sha, duration=duration)
    return None


def attach(con, action, uid, opens, event_title, work_id, entry):
    remote = uuid.uuid4().hex
    approval = "live-imported-replay-" + remote
    action(con, approval, "replay", dict(title=event_title, event=uid, occurrence=opens))
    con.execute("INSERT INTO live_imported_replay VALUES(?,?,?,?,?,?)",
                (uid, opens, remote, work_id, entry["digest"], approval))
    con.execute("INSERT INTO live_occurrence VALUES(?,?,?,?,'archive_pending')", (uid, opens, "", remote))
    return remote


def advance(con, uid, opens, remote):
    updated = con.execute("""UPDATE live_occurrence SET delivery='replay'
        WHERE event=? AND opens=? AND remote=? AND origin='' AND delivery='archive_pending'
          AND EXISTS (SELECT 1 FROM live_imported_replay r JOIN action a ON a.id=r.approval
                      WHERE r.event=live_occurrence.event AND r.opens=live_occurrence.opens
                        AND r.remote=live_occurrence.remote AND a.status='eligible')""", (uid, opens, remote))
    if updated.rowcount != 1:
        return False
    event_webhooks.enqueue(con, "live-event-archive-available", uid, opens, data=dict(clip=remote))
    return True
