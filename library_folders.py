"""Operator library folders and organization, indexed by existing work IDs."""

import re
import uuid

from flask import abort, request
from library_trash import hidden

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_folder(
 id TEXT PRIMARY KEY, parent TEXT REFERENCES library_folder(id),
 name TEXT NOT NULL, color TEXT NOT NULL);
DROP INDEX IF EXISTS library_folder_sibling;
CREATE TABLE IF NOT EXISTS library_placement(
 work TEXT PRIMARY KEY, folder TEXT REFERENCES library_folder(id));
CREATE TABLE IF NOT EXISTS library_star(
 actor TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('work','folder')),
 item TEXT NOT NULL, PRIMARY KEY(actor,kind,item));
"""


def register(app, tx, staff, read_works):
    @app.put("/api/library/star/<kind>/<item>")
    def set_star(kind, item):
        actor = staff()
        body = request.get_json()
        if not isinstance(body, dict) or type(body.get("starred")) is not bool:
            abort(400, "Specify whether this item is starred.")
        with tx() as con:
            if kind == "folder":
                exists = con.execute(
                    "SELECT 1 FROM library_folder WHERE id=?", (item,)
                ).fetchone()
            elif kind == "work":
                exists = any(work["id"] == item for work in read_works())
            else:
                abort(404)
            if not exists or hidden(con, kind, item):
                abort(404)
            if body["starred"]:
                con.execute(
                    "INSERT OR IGNORE INTO library_star VALUES(?,?,?)",
                    (actor, kind, item),
                )
            else:
                con.execute(
                    "DELETE FROM library_star WHERE actor=? AND kind=? AND item=?",
                    (actor, kind, item),
                )
        return "", 204

    @app.get("/api/library/folders")
    def library_folders():
        actor = staff()
        with tx() as con:
            return [
                dict(row)
                for row in con.execute(
                    """SELECT f.*, s.item IS NOT NULL AS starred
                       FROM library_folder f LEFT JOIN library_star s
                       ON s.kind='folder' AND s.item=f.id AND s.actor=?
                       WHERE NOT EXISTS(SELECT 1 FROM library_deleted_item d WHERE d.kind='folder' AND d.item=f.id)
                       ORDER BY f.name COLLATE NOCASE, f.id""",
                    (actor,),
                )
            ]

    @app.post("/api/library/folders")
    @app.put("/api/library/folders/<folder>")
    def save_folder(folder=None):
        staff()
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        name, color, parent = body.get("name"), body.get("color"), body.get("parent")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 32:
            abort(400, "Folder names must contain 1 to 32 characters.")
        name = name.strip()
        if not isinstance(color, str) or not re.fullmatch(r"#[a-fA-F0-9]{6}", color):
            abort(400, "Use a six-digit folder color.")
        if parent is not None and not isinstance(parent, str):
            abort(400)
        uid = folder or uuid.uuid4().hex
        with tx() as con:
            if folder and (
                hidden(con, "folder", folder)
                or not con.execute(
                    "SELECT 1 FROM library_folder WHERE id=?", (folder,)
                ).fetchone()
            ):
                abort(404)
            ancestor = parent
            while ancestor is not None:
                if ancestor == uid:
                    abort(409, "Choose a folder outside this branch.")
                row = con.execute(
                    "SELECT parent FROM library_folder WHERE id=?", (ancestor,)
                ).fetchone()
                if row is None or hidden(con, "folder", ancestor):
                    abort(404, "The selected folder no longer exists.")
                ancestor = row["parent"]
            if con.execute(
                """SELECT 1 FROM library_folder f
                   LEFT JOIN library_deleted_item d ON d.kind='folder' AND d.item=f.id
                   LEFT JOIN library_deletion trash ON trash.id=d.deletion
                   WHERE f.parent IS ? AND f.name = ? COLLATE NOCASE AND f.id <> ?
                   AND (d.item IS NULL OR trash.state='restoring')""",
                (parent, name, uid),
            ).fetchone():
                abort(409, "A folder with this name already exists here.")
            con.execute(
                """INSERT INTO library_folder(id,parent,name,color) VALUES(?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET parent=excluded.parent,
                   name=excluded.name,color=excluded.color""",
                (uid, parent, name, color),
            )
        return dict(
            id=uid, parent=parent, name=name, color=color
        ), 200 if folder else 201

    @app.put("/api/library/placement")
    def move_library_works():
        staff()
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400)
        works, folder = body.get("works"), body.get("folder")
        if (
            not isinstance(works, list)
            or not works
            or any(not isinstance(work, str) for work in works)
            or (folder is not None and not isinstance(folder, str))
        ):
            abort(400)
        with tx() as con:
            if folder is not None and (
                hidden(con, "folder", folder)
                or not con.execute(
                    "SELECT 1 FROM library_folder WHERE id=?", (folder,)
                ).fetchone()
            ):
                abort(404, "The selected folder no longer exists.")
            if not set(works) <= {work["id"] for work in read_works()} or any(
                hidden(con, "work", item) for item in works
            ):
                abort(404, "A selected work no longer exists.")
            con.executemany(
                """INSERT INTO library_placement(work,folder) VALUES(?,?)
                   ON CONFLICT(work) DO UPDATE SET folder=excluded.folder""",
                [(work, folder) for work in works],
            )
        return "", 204
