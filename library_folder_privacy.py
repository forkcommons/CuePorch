"""Folder-wide sharing rules for local library videos."""

import json

from flask import abort, request

import library_privacy as sharing
from library_trash import hidden

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_folder_privacy(
 folder TEXT PRIMARY KEY, settings TEXT NOT NULL, password_hash TEXT);
"""


def closest(con, folder):
    if not folder:
        return None
    return con.execute(
        """WITH RECURSIVE ancestors(id,parent,distance) AS (
               SELECT id,parent,0 FROM library_folder WHERE id=?
               UNION ALL
               SELECT f.id,f.parent,a.distance+1 FROM library_folder f JOIN ancestors a ON f.id=a.parent
           ) SELECT p.* FROM ancestors a JOIN library_folder_privacy p ON p.folder=a.id
           ORDER BY a.distance LIMIT 1""",
        (folder,),
    ).fetchone()


def apply_to_works(con, action, works, rule):
    if not rule:
        return 0
    settings = json.loads(rule["settings"])
    affected = 0
    for work in works:
        if sharing.editable(work) and not sharing.protected_route(con, work["id"]):
            sharing.apply_settings(con, action, work, settings, rule["password_hash"])
            affected += 1
    return affected


def place_under_rule(con, action, works, destination):
    apply_to_works(con, action, works, closest(con, destination))
    sharing.apply_pending(con)


def sync_branch(con, action, folder, works, inherited=None):
    policy = closest(con, folder)
    if policy is None or (inherited is not None and policy["folder"] != inherited):
        return 0
    members = {
        row[0] for row in con.execute(
            "SELECT work FROM library_placement WHERE folder=?", (folder,)
        ) if not hidden(con, "work", row[0])
    }
    count = apply_to_works(con, action, (works[uid] for uid in members if uid in works), policy)
    for (child,) in con.execute("SELECT id FROM library_folder WHERE parent=?", (folder,)).fetchall():
        if not hidden(con, "folder", child):
            count += sync_branch(con, action, child, works, inherited)
    return count


def register(app, tx, staff, action, read_works):
    @app.route("/api/library/folders/<folder>/privacy", methods=["GET", "PUT"])
    def folder_privacy(folder):
        staff()
        with tx() as con:
            if hidden(con, "folder", folder) or not con.execute(
                "SELECT 1 FROM library_folder WHERE id=?", (folder,)
            ).fetchone():
                abort(404)
            direct = con.execute("SELECT * FROM library_folder_privacy WHERE folder=?", (folder,)).fetchone()
            affected = 0
            if request.method == "PUT":
                body = request.get_json()
                enabled = body.get("enabled") if isinstance(body, dict) else None
                if type(enabled) is not bool:
                    abort(400, "Choose whether this folder overrides upload privacy.")
                if enabled:
                    settings, password = sharing.requested_settings(body, direct["password_hash"] if direct else None)
                    con.execute(
                        """INSERT INTO library_folder_privacy VALUES(?,?,?) ON CONFLICT(folder) DO UPDATE SET
                           settings=excluded.settings,password_hash=excluded.password_hash""",
                        (folder, json.dumps(settings), password),
                    )
                    affected = sync_branch(con, action, folder, {work["id"]: work for work in read_works()}, folder)
                    sharing.apply_pending(con)
                else:
                    con.execute("DELETE FROM library_folder_privacy WHERE folder=?", (folder,))
            effective = closest(con, folder)
        return dict(
            enabled=bool(effective and effective["folder"] == folder),
            inherited=bool(effective and effective["folder"] != folder),
            settings=json.loads(effective["settings"]) if effective else None,
            has_password=bool(effective and effective["password_hash"]),
            affected=affected,
        )
