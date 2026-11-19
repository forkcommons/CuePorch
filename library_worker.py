"""Durable PeerTube delivery of catalog entries with operator-controlled gates."""

import hashlib
import json
import os
import threading
import time

from filelock import FileLock, Timeout
from peertube_destination import PeerTube
import library_trash
from library_actions import sync_edits, title_for
from library_thumbnail import selected_thumbnail

SCHEMA = """
CREATE TABLE IF NOT EXISTS publication_job(
 id TEXT PRIMARY KEY,work TEXT NOT NULL,revision TEXT NOT NULL,want_public INTEGER NOT NULL,
 scheduled REAL NOT NULL,status TEXT NOT NULL,remote TEXT,error TEXT,kind TEXT NOT NULL);
"""


def enqueue(con, work, public, scheduled=0, kind="work"):
    if public and (
        work.get("source_privacy") not in ("public", "anybody")
        or work.get("source_embed_policy") not in ("anywhere", "public")
    ):
        raise ValueError("This work's viewing terms require private delivery")
    if (
        not public
        and con.execute(
            "SELECT 1 FROM route WHERE slug=? AND public_record=1", (work["id"],)
        ).fetchone()
    ):
        raise ValueError("Designated public records require a public route")
    rev = hashlib.sha256(json.dumps(work, sort_keys=True).encode()).hexdigest()
    last = con.execute(
        "SELECT id,revision,want_public,scheduled,kind FROM publication_job WHERE work=? ORDER BY rowid DESC LIMIT 1",
        (work["id"],),
    ).fetchone()
    intent = (rev, int(public), scheduled, kind)
    if last and tuple(last)[1:] == intent:
        return last[0]
    job = hashlib.sha256(
        json.dumps([work["id"], *intent, last[0] if last else None]).encode()
    ).hexdigest()
    con.execute(
        "UPDATE publication_job SET status='superseded' WHERE work=? AND status IN ('queued','waiting')",
        (work["id"],),
    )
    con.execute(
        "INSERT OR IGNORE INTO publication_job VALUES(?,?,?,?,?,'queued',NULL,NULL,?)",
        (job, work["id"], rev, public, scheduled, kind),
    )
    return job


def start(app, root, db, tx, action, apply_routes):
    receipts = db.parent / (db.stem + "-receipts")
    receipts.mkdir(exist_ok=True)

    def run():
        # One active delivery loop per database across server processes.
        with app.app_context():
            while receipts.is_dir():
                try:
                    with FileLock(str(receipts / "worker.lock"), timeout=0):
                        origin = os.environ.get("CUEPORCH_PEERTUBE_ORIGIN")
                        library_trash.process(tx, receipts, origin, root)
                        if origin:
                            process(origin)
                            sync_edits(tx, receipts, origin, root)
                except Timeout:
                    pass
                except Exception:
                    app.logger.exception(
                        "Delivery worker could not read the local queue"
                    )
                time.sleep(1)

    def process(origin):
        with tx() as con:
            jobs = con.execute(
                """SELECT * FROM publication_job WHERE status IN ('queued','waiting','processing') AND scheduled<=?
                   AND NOT EXISTS(SELECT 1 FROM library_deleted_item d
                                  WHERE d.kind='work' AND d.item=publication_job.work) ORDER BY rowid""",
                (time.time(),),
            ).fetchall()
        if not jobs:
            return
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf8"))
        works = {w["id"]: w for w in manifest["works"]}
        for job in jobs:
            uid = job["id"]
            try:
                work = works[job["work"]]
                rev = hashlib.sha256(
                    json.dumps(work, sort_keys=True).encode()
                ).hexdigest()
                if rev != job["revision"]:
                    with tx() as con:
                        con.execute(
                            "UPDATE publication_job SET status='superseded' WHERE id=?",
                            (uid,),
                        )
                    continue
                steps = ["intake"]
                if any(f["role"] == "caption" for f in work["files"]):
                    steps.append("caption")
                steps += ["publication"]
                if job["kind"] == "meeting":
                    steps.append("replay")
                if job["want_public"]:
                    steps.append("catalog")
                with tx() as con:
                    if library_trash.hidden(con, "work", work["id"]):
                        continue
                    if (
                        con.execute(
                            "SELECT status FROM publication_job WHERE id=?", (uid,)
                        ).fetchone()[0]
                        == "superseded"
                    ):
                        continue
                    label = title_for(con, work)
                    thumbnail = selected_thumbnail(con, work)
                    status = "processing"
                    for step in steps:
                        result = action(
                            con,
                            uid + "-" + step,
                            step,
                            {
                                "work": work["id"],
                                "title": work["title"],
                                "revision": rev,
                            },
                        )
                        if result != "eligible":
                            status = result
                            break
                    con.execute(
                        "UPDATE publication_job SET status=? WHERE id=?", (status, uid)
                    )
                if status != "processing":
                    continue
                peer = PeerTube(
                    origin,
                    os.environ["PEERTUBE_USERNAME"],
                    os.environ["PEERTUBE_PASSWORD"],
                )
                video = peer.publish(
                    root,
                    dict(work, title=label),
                    receipts / (work["id"] + ".json"),
                    bool(job["want_public"]),
                    thumbnail=thumbnail,
                )
                with tx() as con:
                    if job["want_public"]:
                        dest = peer.origin + "/videos/embed/" + video
                        con.execute(
                            """INSERT INTO route VALUES(?,?,?,?,0,?)
                            ON CONFLICT(kind,slug) DO UPDATE SET destination=excluded.destination,
                            description=excluded.description,current_action=excluded.current_action""",
                            (job["kind"], work["id"], dest, title_for(con, work), uid),
                        )
                    else:
                        con.execute(
                            "DELETE FROM route WHERE slug=? AND public_record=0",
                            (work["id"],),
                        )
                    con.execute(
                        "UPDATE publication_job SET status='playable',remote=?,error=? WHERE id=?",
                        (
                            video,
                            " ".join(
                                json.loads(
                                    (receipts / (work["id"] + ".json")).read_text()
                                ).get("warnings", [])
                            )
                            or None,
                            uid,
                        ),
                    )
            except Exception as exc:
                with tx() as con:
                    con.execute(
                        "UPDATE publication_job SET status='failed',error=? WHERE id=?",
                        (type(exc).__name__, uid),
                    )

    threading.Thread(target=run, name="CuePorch delivery", daemon=True).start()
