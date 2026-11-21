from flask import request, abort
from time import time
from browser_account_security import private


SCHEMA = """
CREATE TABLE IF NOT EXISTS library_hubspot_folder (
    singleton    INTEGER PRIMARY KEY CHECK (singleton = 1),
    folder       TEXT NOT NULL,
    revision     TEXT NOT NULL,
    media_type   TEXT NOT NULL,
    due          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS library_hubspot_folder_work (
    work TEXT PRIMARY KEY
);
"""

_SELECT = (
    "SELECT singleton, folder, revision, media_type, due "
    "FROM library_hubspot_folder WHERE singleton = 1"
)


def plan(con):
    return con.execute(_SELECT).fetchone()


def save(con, folder, revision, media_type, now):
    con.execute(
        "INSERT INTO library_hubspot_folder "
        "(singleton, folder, revision, media_type, due) "
        "VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET "
        "folder = excluded.folder, "
        "revision = excluded.revision, "
        "media_type = excluded.media_type, "
        "due = excluded.due",
        (folder, revision, media_type, now),
    )
    return plan(con)


def clear(con):
    con.execute("DELETE FROM library_hubspot_folder WHERE singleton = 1")


def is_managed(con, uid):
    cur = con.execute(
        "SELECT 1 FROM library_hubspot_folder_work WHERE work = ? LIMIT 1",
        (uid,),
    )
    return cur.fetchone() is not None


def active_member(con, uid, folder):
    import library_trash

    if not folder:
        return False

    cur = con.execute(
        "SELECT 1 FROM library_folder WHERE id = ? LIMIT 1",
        (folder,),
    )
    if cur.fetchone() is None:
        return False

    cur = con.execute(
        "SELECT 1 FROM library_placement WHERE work = ? AND folder = ? LIMIT 1",
        (uid, folder),
    )
    if cur.fetchone() is None:
        return False

    if library_trash.hidden(con, "folder", folder):
        return False

    if library_trash.hidden(con, "work", uid):
        return False

    return True


def retire(con, now, revision, media_type):
    from library_hubspot_bridge import queue_archive

    rows = con.execute(
        "SELECT j.work FROM library_hubspot_folder_work m "
        "JOIN library_hubspot_media j ON j.work=m.work "
        "WHERE j.revision=? AND j.media_type=? "
        "ORDER BY j.work",
        (revision, media_type),
    ).fetchall()
    for row in rows:
        queue_archive(con, row["work"], now)


def folder_eligible(con, folder, origin, consult):
    from library_hubspot_bridge import external_id

    eligible = set()
    for row in con.execute(
        "SELECT work FROM library_placement WHERE folder=?", (folder,)
    ):
        work = row["work"]
        if (
            active_member(con, work, folder)
            and consult(con, work, external_id(origin, work)) is not None
        ):
            eligible.add(work)
    return eligible


def withdraw(con, eligible, revision, media_type, now):
    from library_hubspot_bridge import queue_archive

    cur = con.cursor()
    cur.execute(
        "SELECT j.work FROM library_hubspot_media j "
        "JOIN library_hubspot_folder_work m ON j.work=m.work "
        "WHERE j.revision=? AND j.media_type=? AND j.operation='publish'",
        (revision, media_type),
    )
    rows = cur.fetchall()
    for row in rows:
        if row["work"] not in eligible:
            queue_archive(con, row["work"], now)


def enqueue(con, eligible, revision, media_type, origin, now, action):
    import library_hubspot_bridge as bridge

    for uid in sorted(eligible):
        row = bridge.get(con, uid)
        if row is not None and not (
            is_managed(con, uid)
            and row["state"] == "archived"
            and row["revision"] == revision
            and row["media_type"] == media_type
        ):
            continue

        bridge.queue_publish(
            con,
            action,
            uid,
            revision,
            media_type,
            bridge.external_id(origin, uid),
            now,
        )

        con.execute(
            "INSERT OR IGNORE INTO library_hubspot_folder_work(work) VALUES(?)",
            (uid,),
        )


def reconcile(tx, clock, connection, media_type, origin, consult_batch, action):
    now = clock()
    with tx() as con:
        p = plan(con)
        if not p or p["due"] > now:
            return False
        con.execute(
            "UPDATE library_hubspot_folder SET due=? WHERE singleton=1",
            (now + 15,),
        )
        saved = connection.connection(con)
        if (
            saved
            and saved["revision"] == p["revision"]
            and media_type == p["media_type"]
        ):
            eligible = folder_eligible(con, p["folder"], origin, consult_batch())
            withdraw(con, eligible, p["revision"], media_type, now)
            enqueue(con, eligible, p["revision"], media_type, origin, now, action)
    return True


def arm(con, folder, revision, media_type, now):
    from library_trash import hidden

    if not isinstance(folder, str):
        abort(400)

    if con.execute(
        "SELECT 1 FROM library_folder WHERE id=?", (folder,)
    ).fetchone() is None or hidden(con, "folder", folder):
        abort(404)

    selected = plan(con)
    if selected is not None and (
        selected["revision"] != revision or selected["media_type"] != media_type
    ):
        abort(409)

    return save(con, folder, revision, media_type, now)


def register_folder(
    app, tx, owner, check_request, configured, connection, media_type, enforce_sharing
):
    @app.route(
        "/api/library/hubspot-media-bridge/folder", methods=["GET", "PUT", "DELETE"]
    )
    def _folder():
        owner()
        body = check_request()
        with tx() as con:
            p = plan(con)
            saved = connection.connection(con) if configured else None
            active = bool(
                p
                and saved
                and p["revision"] == saved["revision"]
                and p["media_type"] == media_type
            )
            if request.method == "GET":
                return private(
                    dict(
                        configured=configured,
                        connected=bool(saved),
                        folder=p["folder"] if p else None,
                        active=active,
                    )
                )
            if request.method == "PUT":
                if not saved:
                    abort(409)
                enforce_sharing(con, feature="outbound")
                arm(con, body.get("folder"), saved["revision"], media_type, time())
            else:
                if p:
                    if not active:
                        abort(409)
                    retire(con, time(), p["revision"], media_type)
                clear(con)
        return "", 204


def remove_member(con, uid):
    """Delete a single work row by uid. Caller is responsible for commit."""
    con.execute(
        "DELETE FROM library_hubspot_folder_work WHERE work=?",
        (uid,),
    )
