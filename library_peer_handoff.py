"""Retire managed public PeerTube delivery before activating local access rules."""

import json
import re
import time

from cueporch import dump

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_peer_handoff(
 work TEXT PRIMARY KEY, video TEXT NOT NULL, state TEXT NOT NULL,
 attempted REAL NOT NULL DEFAULT 0, error TEXT);
"""


def managed_route(con, work):
    if con.execute("SELECT 1 FROM pending_route WHERE slug=?", (work,)).fetchone():
        return False
    rows = con.execute(
        """SELECT r.kind,r.public_record,j.remote,j.want_public,j.status
           FROM route r LEFT JOIN publication_job j ON j.id=r.current_action AND j.work=r.slug
           WHERE r.slug=?""",
        (work,),
    ).fetchall()
    if not rows:
        return None
    ids = {row["remote"] for row in rows}
    if len(ids) != 1 or any(
        row["kind"] != "work" or row["public_record"] or not row["want_public"] or row["status"] != "playable"
        for row in rows
    ):
        return False
    video = next(iter(ids))
    return video if isinstance(video, str) and re.fullmatch(r"[a-zA-Z0-9-]{1,64}", video) else False


def get(con, uid):
    return con.execute("SELECT * FROM library_peer_handoff WHERE work=?", (uid,)).fetchone()


def request_handoff(con, uid, video):
    con.execute("INSERT OR IGNORE INTO library_peer_handoff(work,video,state) VALUES(?,?,'pending')", (uid, video))


def process(tx, receipts, origin, connect, apply_routes):
    with tx() as con:
        candidates = con.execute(
            """SELECT h.* FROM library_peer_handoff h
               WHERE h.state='pending' AND h.attempted<=? AND NOT EXISTS (
                 SELECT 1 FROM route r WHERE r.slug=h.work AND r.public_record=1
               ) ORDER BY h.attempted""",
            (time.time() - 10,),
        ).fetchall()
    for candidate in candidates:
        work, video = candidate["work"], candidate["video"]
        receipt = receipts / (work + ".json")
        try:
            if not origin:
                raise ValueError("Connect the publishing host to complete this handoff.")
            record = json.loads(receipt.read_text(encoding="utf8"))
            if record["work"] != work or record["video"] != video or record["provider"] != origin.rstrip("/"):
                raise ValueError("The publishing receipt does not match the original host and video.")
            peer = connect(origin)
            if record["account"] != peer.user["id"]:
                raise ValueError("Connect the original publishing account.")
            peer.set_public(video, False)
            remote = peer.call("GET", "/api/v1/videos/" + video).json()
            if remote["privacy"]["id"] != 3:
                raise ValueError("PeerTube has not confirmed that this copy is private.")
            record["visibility"] = "private"
            dump(receipt, record)
        except (OSError, ValueError, KeyError) as exc:
            error = str(exc) if type(exc) is ValueError else type(exc).__name__
            with tx() as con:
                con.execute("UPDATE library_peer_handoff SET attempted=?,error=? WHERE work=?",
                            (time.time(), error, work))
            continue
        with tx() as con:
            con.execute("UPDATE library_peer_handoff SET state='private',error=NULL WHERE work=?", (work,))
            con.execute("DELETE FROM route WHERE slug=? AND public_record=0", (work,))
            apply_routes(con)
