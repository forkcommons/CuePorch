import uuid
from flask import abort

APPROVAL = "publication"

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_hubspot_media (
    work             TEXT PRIMARY KEY,
    job              TEXT NOT NULL UNIQUE,
    revision         TEXT NOT NULL,
    media_type       TEXT NOT NULL,
    external         TEXT NOT NULL,
    remote           TEXT,
    state            TEXT NOT NULL,
    operation        TEXT NOT NULL CHECK (operation IN ('publish', 'archive')),
    create_attempted INTEGER NOT NULL DEFAULT 0,
    due              REAL,
    problem          TEXT
);
CREATE INDEX IF NOT EXISTS library_hubspot_media_state_due
    ON library_hubspot_media (state, due);
"""


def _cancel_waiting_action(con, action_id):
    con.execute(
        "UPDATE action SET status = 'rejected', decision = 'canceled' "
        "WHERE id = ? AND status = 'waiting'",
        (action_id,),
    )


def get(con, work):
    return con.execute(
        "SELECT * FROM library_hubspot_media WHERE work = ?", (work,)
    ).fetchone()


def queue_publish(con, action, work, revision, media_type, external, now):
    row = get(con, work)

    if row is not None:
        same_scope = (
            row["revision"] == revision
            and row["media_type"] == media_type
            and row["external"] == external
        )
        risky = bool(row["remote"]) or bool(row["create_attempted"])

        if not same_scope and risky:
            abort(409)

        if row["operation"] == "archive" and row["state"] == "queued":
            abort(409)

        if same_scope and row["operation"] == "publish" and row["state"] == "queued":
            return row

        preserve = same_scope
        remote = row["remote"] if preserve else None
        attempted = row["create_attempted"] if preserve else 0
        _cancel_waiting_action(con, row["job"])
    else:
        remote = None
        attempted = 0

    job = uuid.uuid4().hex
    con.execute(
        """
        INSERT INTO library_hubspot_media
            (work, job, revision, media_type, external, remote, state,
             operation, create_attempted, due, problem)
        VALUES (?, ?, ?, ?, ?, ?, 'queued', 'publish', ?, ?, NULL)
        ON CONFLICT(work) DO UPDATE SET
            job              = excluded.job,
            revision         = excluded.revision,
            media_type       = excluded.media_type,
            external         = excluded.external,
            remote           = excluded.remote,
            state            = excluded.state,
            operation        = excluded.operation,
            create_attempted = excluded.create_attempted,
            due              = excluded.due,
            problem          = excluded.problem
        """,
        (work, job, revision, media_type, external, remote, attempted, now),
    )
    action(
        con,
        job,
        APPROVAL,
        {"work": work, "destination": "HubSpot Media Bridge"},
    )
    return get(con, work)


def queue_archive(con, work, now):
    row = get(con, work)
    if row is None:
        return None

    if row["operation"] == "archive" and row["state"] in ("archived", "queued"):
        return row

    _cancel_waiting_action(con, row["job"])

    if not row["remote"] and not row["create_attempted"]:
        con.execute(
            """
            UPDATE library_hubspot_media
               SET operation = 'archive',
                   state = 'archived',
                   due = NULL,
                   problem = NULL
             WHERE work = ?
            """,
            (work,),
        )
    else:
        con.execute(
            """
            UPDATE library_hubspot_media
               SET operation = 'archive',
                   state = 'queued',
                   due = ?,
                   problem = NULL
             WHERE work = ?
            """,
            (now, work),
        )

    return get(con, work)


def status(con, row):
    if row is None:
        return {
            "state": "off",
            "operation": None,
            "problem": None,
            "remote": False,
        }

    state = row["state"] or "off"

    if state == "queued" and row["operation"] == "publish":
        action_row = con.execute(
            "SELECT status FROM action WHERE id = ?",
            (row["job"],),
        ).fetchone()
        if action_row is not None and action_row["status"] == "waiting":
            state = "waiting"

    return {
        "state": state,
        "operation": row["operation"],
        "problem": row["problem"],
        "remote": bool(row["remote"]),
    }


def guarded(con, job, connection, consult, stop):
    from flask import abort
    import hubspot_media_bridge

    if stop.is_set():
        abort(423)

    current = get(con, job["work"])
    if (
        not current
        or current["job"] != job["job"]
        or current["operation"] != job["operation"]
    ):
        abort(409)
    if current["state"] != "queued":
        abort(409)

    connection.authorize(con, job["revision"])

    if hubspot_media_bridge.object_type() != job["media_type"]:
        abort(409)

    if job["operation"] == "archive":
        return None

    row = con.execute(
        "SELECT status FROM action WHERE id = ?", (job["job"],)
    ).fetchone()
    status = row["status"] if row else None
    if status == "waiting":
        abort(423)
    if status != "eligible":
        abort(409)

    metadata = consult(con, job["work"], job["external"])
    if metadata is None:
        abort(409)

    return metadata


def send(tx, job, connection, consult, stop):
    import hubspot_media_bridge as bridge
    from flask import abort
    from werkzeug.exceptions import HTTPException

    with tx() as con:
        props = guarded(con, job, connection, consult, stop)

    revision = job["revision"]
    media_type = job["media_type"]
    external = job["external"]
    remote = job["remote"]
    create_attempted = job["create_attempted"]

    def before_write():
        with tx() as con:
            current = guarded(con, job, connection, consult, stop)
        if current != props:
            abort(409)

    found = bridge.existing_object(connection, revision, media_type, external)
    found_id = found["id"] if found is not None else None

    if found_id is not None and remote is not None and found_id != remote:
        abort(409)

    known_id = remote if remote is not None else found_id

    if props is None:
        if known_id is None:
            return (
                "unconfirmed",
                None,
                1,
                "HubSpot has not confirmed the media record. Recheck the delivery.",
            )
        bridge.archive_object(
            connection, revision, media_type, known_id, before_write=before_write
        )
        return ("archived", None, 0, None)

    if known_id is not None:
        bridge.update_object(
            connection,
            revision,
            media_type,
            known_id,
            props,
            before_write=before_write,
        )
        return ("published", known_id, 1, None)

    if create_attempted:
        return (
            "unconfirmed",
            None,
            1,
            "HubSpot has not confirmed the media record. Recheck the delivery.",
        )

    def mark_attempt():
        with tx() as con:
            current = guarded(con, job, connection, consult, stop)
            if current != props:
                abort(409)
            con.execute(
                "UPDATE library_hubspot_media SET create_attempted = 1 "
                "WHERE job=? AND work=? AND operation='publish' AND state='queued'",
                (job["job"], job["work"]),
            )

    try:
        data = bridge.create_object(
            connection, revision, media_type, props, before_write=mark_attempt
        )
    except HTTPException as exc:
        if exc.code == 429:
            with tx() as con:
                con.execute(
                    "UPDATE library_hubspot_media SET create_attempted = 0 "
                    "WHERE job=? AND work=? AND operation='publish' AND state='queued'",
                    (job["job"], job["work"]),
                )
        raise

    return ("published", data["id"], 1, None)


def run_one(tx, connection, consult, stop, clock):
    from werkzeug.exceptions import HTTPException

    with tx() as con:
        job = con.execute(
            """
            SELECT j.*
              FROM library_hubspot_media AS j
             WHERE j.state = 'queued'
               AND j.due <= ?
               AND (
                    j.operation = 'archive'
                    OR EXISTS (
                        SELECT 1 FROM action AS a
                         WHERE a.id = j.job
                           AND a.status IN ('eligible', 'rejected')
                    )
               )
             ORDER BY j.due, j.job
             LIMIT 1
            """,
            (clock(),),
        ).fetchone()

    if not job or stop.is_set():
        return False

    state = remote = attempted = problem = error = None
    try:
        state, remote, attempted, problem = send(tx, job, connection, consult, stop)
    except HTTPException as exc:
        if exc.code == 423 or stop.is_set():
            return False
        error = exc

    delay = 15
    with tx() as con:
        current = con.execute(
            "SELECT * FROM library_hubspot_media WHERE work = ?",
            (job["work"],),
        ).fetchone()
        if (
            current is None
            or current["job"] != job["job"]
            or current["state"] != "queued"
        ):
            return True

        if current["operation"] != job["operation"]:
            if current["operation"] == "archive" and remote:
                con.execute(
                    """
                    UPDATE library_hubspot_media
                       SET remote = ?, create_attempted = 1
                     WHERE work = ? AND job = ?
                    """,
                    (remote, job["work"], job["job"]),
                )
            return True

        if error is not None:
            code = error.code
            remote = current["remote"]
            attempted = current["create_attempted"]
            problem = None
            if code == 429:
                state = "queued"
                delay = max(15, getattr(error, "retry_after", None) or 0)
            elif code in (404, 409):
                state = "canceled"
                problem = f"HubSpot delivery failed ({code})."
            elif code in (502, 503, 504) and attempted and not remote:
                state = "unconfirmed"
                problem = f"HubSpot delivery failed ({code})."
            else:
                state = "problem"
                problem = f"HubSpot delivery failed ({code})."

        if state == "queued":
            due = clock() + delay
        elif job["operation"] == "publish" and (remote or attempted):
            due = clock() + 30
        else:
            due = None

        con.execute(
            """
            UPDATE library_hubspot_media
               SET state = ?, remote = ?, create_attempted = ?,
                   problem = ?, due = ?
             WHERE work = ? AND job = ?
            """,
            (state, remote, attempted, problem, due, job["work"], job["job"]),
        )
        return True


def scan_one(tx, consult, clock):
    now = clock()
    with tx() as con:
        row = con.execute(
            "SELECT * FROM library_hubspot_media "
            "WHERE operation='publish' AND state IN "
            "('published','canceled','problem','unconfirmed') "
            "AND (remote IS NOT NULL OR create_attempted=1) "
            "AND due<=? ORDER BY due, job LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            return False
        if consult(con, row["work"], row["external"]) is None:
            queue_archive(con, row["work"], now)
        else:
            con.execute(
                "UPDATE library_hubspot_media SET due=? WHERE work=? AND job=?",
                (now + 30, row["work"], row["job"]),
            )
    return True


def start(
    app,
    tx,
    database,
    connection,
    consult,
    *,
    clock=None,
    reconcile=None,
    analytics=None,
):
    from threading import Event, Thread
    from time import time
    from filelock import FileLock, Timeout

    stop = Event()
    if clock is None:
        clock = time

    def _run():
        while not stop.is_set():
            try:
                with FileLock(str(database) + ".hubspot-media-bridge.lock", timeout=0):
                    if reconcile is not None and reconcile(clock):
                        continue
                    if run_one(tx, connection, consult, stop, clock) or scan_one(
                        tx, consult, clock
                    ):
                        continue
                    if analytics is not None and analytics(stop, clock):
                        continue
                stop.wait(1)
            except Timeout:
                stop.wait(1)
            except Exception:
                app.logger.error(
                    "The HubSpot Media Bridge queue could not be processed."
                )
                stop.wait(1)

    worker = Thread(
        target=_run,
        name="HubSpot Media Bridge queue worker",
        daemon=True,
    )
    worker.start()

    def close():
        stop.set()
        worker.join(8)

    return close


def register(
    app,
    tx,
    database,
    origin,
    staff,
    action,
    connection,
    read_works,
    title_for,
    permits,
    enforce_sharing,
    *,
    credential_owner,
    hubspot_origins,
):
    from flask import request, abort
    import hubspot_media_bridge as bridge
    import library_hubspot_folders as folders
    import library_hubspot_media_events as media_events
    from browser_account_security import (
        private,
        secure_site,
        same_origin,
        json_mutation,
    )
    from library_analytics import source_origin
    from time import time

    kind = bridge.object_type()
    scopes = connection.config.authorization_parameters.get(
        "optional_scope", ""
    ).split()
    configured = (
        bool(kind)
        and origin != "Unknown"
        and connection.configured
        and all(s in scopes for s in bridge.MEDIA_SCOPES)
    )

    def consult(con, uid, external):
        work = next((work for work in read_works() if work["id"] == uid), None)
        return approved_props(con, uid, external, origin, work, title_for, permits)

    def worker_consult(con, uid, external):
        if folders.is_managed(con, uid):
            selected = folders.plan(con)
            if not (selected and folders.active_member(con, uid, selected["folder"])):
                return None
        return consult(con, uid, external)

    def check_request():
        secure = secure_site(origin)
        same_origin(secure)
        if (
            origin != "Unknown"
            and source_origin(("https://" if secure else "http://") + request.host)
            != origin
        ):
            abort(403)
        if request.method != "GET":
            return json_mutation(secure, "X-CuePorch-Integration")
        return None

    folders.register_folder(
        app,
        tx,
        credential_owner,
        check_request,
        configured,
        connection,
        kind,
        enforce_sharing,
    )

    @app.route(
        "/api/library/work/<uuid:uid>/hubspot-media-bridge",
        methods=["GET", "PUT", "DELETE"],
    )
    def hubspot_media_bridge(uid):
        uid = str(uid)
        staff()
        check_request()
        external = external_id(origin, uid)

        with tx() as con:
            row = get(con, uid)
            saved = connection.connection(con) if configured else None
            is_managed = folders.is_managed(con, uid)
            selected = folders.plan(con) if is_managed else None
            active = bool(
                is_managed
                and selected
                and folders.active_member(con, uid, selected["folder"])
            )
            eligible = bool(
                configured and saved and consult(con, uid, external) is not None
            )

            def snapshot(current):
                return private(
                    dict(
                        status(con, current),
                        configured=configured,
                        connected=bool(saved),
                        eligible=eligible,
                        managed=active,
                    )
                )

            if request.method == "GET":
                return snapshot(row)

            if request.method == "PUT":
                if not (saved and eligible):
                    abort(409)
                enforce_sharing(con, feature="outbound")
                if is_managed and not active:
                    if row is None or row["state"] != "archived":
                        abort(409)
                    folders.remove_member(con, uid)
                row = queue_publish(
                    con, action, uid, saved["revision"], kind, external, time()
                )
            else:
                if not (
                    row
                    and saved
                    and row["revision"] == saved["revision"]
                    and row["media_type"] == kind
                ):
                    abort(409)
                if is_managed and active:
                    abort(409)
                row = queue_archive(con, uid, time())
            return snapshot(row)

    def reconcile_folder(clock):
        def batch():
            manifest = {work["id"]: work for work in read_works()}
            return lambda con, uid, external: approved_props(
                con, uid, external, origin, manifest.get(uid), title_for, permits
            )

        return folders.reconcile(tx, clock, connection, kind, origin, batch, action)

    return (
        start(
            app,
            tx,
            database,
            connection,
            worker_consult,
            reconcile=reconcile_folder,
            analytics=(
                lambda stop, clock: media_events.run_one(
                    tx, connection, hubspot_origins, permits, read_works, stop, clock
                )
            )
            if hubspot_origins
            else None,
        )
        if configured
        else (lambda: None)
    )


def external_id(origin, uid):
    from hashlib import sha256

    return "cueporch-" + sha256(f"{origin}/{uid}".encode()).hexdigest()


def approved_props(con, uid, external, origin, work, title_for, permits):
    import library_privacy
    import library_embed

    if work is None:
        return None
    if work.get("asset_type", "video") != "video":
        return None
    if not library_privacy.public_oembed_eligible(con, work, permits):
        return None
    return {
        "hs_title": title_for(con, work),
        "hs_external_id": external,
        "hs_oembed_url": library_embed.oembed_url(origin, uid),
        "hs_details_page_link": f"{origin}/watch/work/{uid}",
    }
