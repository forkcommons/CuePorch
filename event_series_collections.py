"""Organize event series in the current workspace Library folders."""

from flask import abort

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS live_series_collection ("
    "series TEXT PRIMARY KEY REFERENCES live_series_page(id) ON DELETE CASCADE, "
    "folder TEXT NOT NULL REFERENCES library_folder(id) ON DELETE CASCADE);"
)

HIDE = ("NOT EXISTS (SELECT 1 FROM library_deleted_item d "
        "WHERE d.kind='folder' AND d.item=f.id)")


def folders(con):
    rows = con.execute(
        f"SELECT f.id, f.parent, f.name FROM library_folder f "
        f"WHERE {HIDE} ORDER BY f.name COLLATE NOCASE, f.id"
    ).fetchall()
    return [dict(row) for row in rows]


def active_folder(con, uid):
    row = con.execute(
        f"""SELECT c.folder
            FROM live_series_collection c
            JOIN library_folder f ON f.id = c.folder
            WHERE c.series = ? AND {HIDE}""",
        (uid,),
    ).fetchone()
    return (row["folder"] or None) if row else None


def set_folder(con, uid, folder):
    if folder is None:
        con.execute("DELETE FROM live_series_collection WHERE series = ?", (uid,))
        return
    if not isinstance(folder, str) or not folder.strip():
        abort(400)
    found = con.execute(
        f"SELECT 1 FROM library_folder f WHERE f.id = ? AND {HIDE}",
        (folder,),
    ).fetchone()
    if found is None:
        abort(404)
    con.execute(
        "INSERT INTO live_series_collection (series, folder) VALUES (?, ?) "
        "ON CONFLICT(series) DO UPDATE SET folder = excluded.folder",
        (uid, folder),
    )
