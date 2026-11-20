"""Recently deleted batches with a recoverable, fail-closed access state."""

import json
import time
import uuid

from flask import abort, request

import folder_slack_queue

RECOVERY_SECONDS = 30 * 24 * 3600
SCHEMA = """
CREATE TABLE IF NOT EXISTS library_deletion(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, item TEXT NOT NULL,
 title TEXT NOT NULL, actor TEXT NOT NULL, deleted_at REAL NOT NULL,
 state TEXT NOT NULL, error TEXT, attempted_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS library_deleted_item(
 kind TEXT NOT NULL, item TEXT NOT NULL, deletion TEXT NOT NULL
 REFERENCES library_deletion(id), PRIMARY KEY(kind,item));
"""


def hidden(con, kind, item):
    return (
        con.execute(
            "SELECT 1 FROM library_deleted_item WHERE kind=? AND item=?", (kind, item)
        ).fetchone()
        is not None
    )


def register(app, tx, staff, read_works):
    @app.get("/api/library/recently-deleted")
    def recently_deleted():
        staff()
        with tx() as con:
            return [
                dict(row)
                for row in con.execute(
                    """SELECT id,kind,title,actor,deleted_at,state,error
                       FROM library_deletion WHERE deleted_at>? OR state!='deleted'
                       ORDER BY deleted_at DESC,id""",
                    (time.time() - RECOVERY_SECONDS,),
                )
            ]

    @app.post("/api/library/recently-deleted")
    def remove_library_items():
        actor = staff()
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        kind, items = body.get("kind"), body.get("items")
        if (
            kind not in ("work", "folder")
            or not isinstance(items, list)
            or not items
            or any(not isinstance(item, str) for item in items)
            or (kind == "folder" and len(items) != 1)
        ):
            abort(400)
        catalog = read_works()
        works = {work["id"]: work["title"] for work in catalog}
        videos = {work["id"] for work in catalog if work.get("asset_type", "video") == "video"}
        with tx() as con:
            works.update(
                (work, title)
                for work, title in con.execute("SELECT work,title FROM library_title")
                if work in works
            )
            if kind == "folder":
                folder = con.execute(
                    "SELECT name FROM library_folder WHERE id=?", (items[0],)
                ).fetchone()
                if folder is None or hidden(con, kind, items[0]):
                    abort(404)
                branch = con.execute(
                    """WITH RECURSIVE branch(id) AS (
                       SELECT ? UNION
                       SELECT f.id FROM library_folder f JOIN branch b ON f.parent=b.id)
                       SELECT b.id,trash.state,d.item IS NOT NULL AS hidden FROM branch b
                       LEFT JOIN library_deleted_item d ON d.kind='folder' AND d.item=b.id
                       LEFT JOIN library_deletion trash ON trash.id=d.deletion""",
                    (items[0],),
                ).fetchall()
                descendants = {row["id"] for row in branch}
                folders = {row["id"] for row in branch if not row["hidden"]}
                placements = con.execute(
                    """SELECT p.*,d.item IS NOT NULL AS hidden,trash.state FROM library_placement p
                       LEFT JOIN library_deleted_item d ON d.kind='work' AND d.item=p.work
                       LEFT JOIN library_deletion trash ON trash.id=d.deletion"""
                ).fetchall()
                if any(row["state"] == "restoring" for row in branch) or any(
                    row["state"] == "restoring" and row["folder"] in descendants
                    for row in placements
                ):
                    abort(409, "Wait for items in this folder to finish restoring.")
                selected = [
                    row["work"]
                    for row in placements
                    if row["folder"] in folders
                    and row["work"] in works
                    and not row["hidden"]
                ]
                batches = [
                    (
                        "folder",
                        items[0],
                        folder["name"],
                        [("folder", item) for item in folders]
                        + [("work", item) for item in selected],
                    )
                ]
            else:
                selected = list(dict.fromkeys(items))
                if any(
                    item not in works or hidden(con, "work", item) for item in selected
                ):
                    abort(
                        404, "A selected work is already deleted or no longer exists."
                    )
                batches = [
                    ("work", item, works[item], [("work", item)]) for item in selected
                ]
            for item in selected:
                if con.execute(
                    """WITH related(destination) AS (SELECT destination FROM route WHERE slug=?)
                       SELECT 1 FROM route WHERE public_record=1 AND
                         (slug=? OR destination IN related)
                       UNION ALL SELECT 1 FROM pending_route p JOIN action a ON p.action_id=a.id
                       WHERE json_extract(a.payload,'$.public_record')=1 AND
                         (p.slug=? OR json_extract(a.payload,'$.destination') IN related)""",
                    (item, item, item),
                ).fetchone():
                    abort(409, "A designated public record must remain available.")
                if con.execute(
                    """SELECT 1 FROM route r WHERE slug=? AND NOT EXISTS
                       (SELECT 1 FROM publication_job j WHERE j.id=r.current_action AND j.work=r.slug)""",
                    (item,),
                ).fetchone():
                    abort(
                        409,
                        "Withdraw this separately managed video route before deleting the work.",
                    )
            con.executemany(
                "DELETE FROM library_download WHERE work=?",
                [(item,) for item in selected],
            )
            for item in selected:
                if item in videos:
                    folder_slack_queue.enqueue(con, item, "delete")
            deleted_at = time.time()
            for batch_kind, item, title, members in batches:
                deletion = uuid.uuid4().hex
                con.execute(
                    """INSERT INTO library_deletion(id,kind,item,title,actor,deleted_at,state)
                       VALUES(?,?,?,?,?,?,'withdrawing')""",
                    (deletion, batch_kind, item, title, actor, deleted_at),
                )
                con.executemany(
                    "INSERT INTO library_deleted_item VALUES(?,?,?)",
                    [
                        (member_kind, member, deletion)
                        for member_kind, member in members
                    ],
                )
        return {"queued": len(batches)}, 202

    @app.post("/api/library/recently-deleted/<deletion>/restore")
    def restore_library_item(deletion):
        staff()
        with tx() as con:
            row = con.execute(
                "SELECT * FROM library_deletion WHERE id=?", (deletion,)
            ).fetchone()
            if not row:
                abort(404)
            if row["state"] == "restoring":
                return "", 202
            if row["deleted_at"] <= time.time() - RECOVERY_SECONDS:
                abort(410, "The 30-day recovery window has expired.")
            if row["state"] != "deleted":
                abort(409, "Wait for withdrawal to finish before restoring.")
            if row["kind"] == "folder":
                folder = con.execute(
                    "SELECT parent,name FROM library_folder WHERE id=?", (row["item"],)
                ).fetchone()
                parent = folder["parent"]
                if con.execute(
                    """SELECT 1 FROM library_folder f
                       LEFT JOIN library_deleted_item d ON d.kind='folder' AND d.item=f.id
                       LEFT JOIN library_deletion trash ON trash.id=d.deletion
                       WHERE f.id<>? AND f.parent IS ? AND f.name=? COLLATE NOCASE
                       AND (d.item IS NULL OR trash.state='restoring')""",
                    (row["item"], parent, folder["name"]),
                ).fetchone():
                    abort(409, "Rename the existing folder before restoring.")
            else:
                placement = con.execute(
                    "SELECT folder FROM library_placement WHERE work=?", (row["item"],)
                ).fetchone()
                parent = placement["folder"] if placement else None
            if parent is not None and hidden(con, "folder", parent):
                abort(409, "Restore the parent folder first.")
            con.execute(
                "UPDATE library_deletion SET state='restoring', attempted_at=0,error=NULL WHERE id=?",
                (deletion,),
            )
        return "", 202


def process(tx, receipts, origin, root, connect):
    """Run while holding the same lock used by the hosted publication worker."""
    with tx() as con:
        pending = con.execute(
            """SELECT * FROM library_deletion WHERE state IN ('withdrawing','restoring')
               AND attempted_at<=? ORDER BY deleted_at,id""",
            (time.time() - 10,),
        ).fetchall()
    for deletion in pending:
        uid = deletion["id"]
        with tx() as con:
            members = con.execute(
                "SELECT item FROM library_deleted_item WHERE deletion=? AND kind='work'",
                (uid,),
            ).fetchall()
        try:
            restoring = deletion["state"] == "restoring"
            unpublished = []
            if restoring:
                works = {
                    item["id"]: item
                    for item in json.loads(
                        (root / "manifest.json").read_text(encoding="utf8")
                    )["works"]
                }
            for (work,) in members:
                receipt = receipts / (work + ".json")
                if not receipt.exists():
                    continue
                state = json.loads(receipt.read_text())
                video = state.get("video")
                if video is None:
                    continue
                if state.get("work") != work:
                    raise ValueError("Receipt work mismatch")
                if not origin or state["provider"] != origin.rstrip("/"):
                    raise ValueError("Connect the original video host")
                peer = connect(origin)
                if state["account"] != peer.user["id"]:
                    raise ValueError("Connect the original publishing account")
                is_public = restoring and state.get("visibility") == "public"
                if is_public and (
                    works[work].get("source_privacy") not in ("public", "anybody")
                    or works[work].get("source_embed_policy")
                    not in ("public", "anywhere")
                ):
                    is_public = False
                    unpublished.append(work)
                peer.set_public(video, is_public)
            with tx() as con:
                if restoring:
                    con.executemany(
                        "DELETE FROM route WHERE slug=?",
                        [(work,) for work in unpublished],
                    )
                    con.execute(
                        "DELETE FROM library_deleted_item WHERE deletion=?", (uid,)
                    )
                    con.execute("DELETE FROM library_deletion WHERE id=?", (uid,))
                else:
                    con.execute(
                        "UPDATE library_deletion SET state='deleted',error=NULL WHERE id=?",
                        (uid,),
                    )
        except (OSError, ValueError, KeyError) as exc:
            with tx() as con:
                con.execute(
                    "UPDATE library_deletion SET attempted_at=?,error=? WHERE id=?",
                    (time.time(), type(exc).__name__, uid),
                )
