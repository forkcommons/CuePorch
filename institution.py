"""Institution-scoped operator policy, screening grants and complaint records."""

import argparse
import csv
import io
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import zipfile
import sqlite3
import subprocess
import time
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import urlsplit

from flask import (
    Flask,
    Response,
    abort,
    redirect,
    render_template,
    request,
    stream_with_context,
)
from waitress import serve

from cueporch import verify
import library_folders
import library_trash
import library_worker

STEPS = (
    "intake",
    "caption",
    "publication",
    "replay",
    "migration",
    "grant",
    "catalog",
    "moderation",
    "restoration",
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS policy(step TEXT PRIMARY KEY, required INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS action(id TEXT PRIMARY KEY,step TEXT NOT NULL,payload TEXT NOT NULL,
 status TEXT NOT NULL,decision TEXT,actor TEXT,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS grant_access(id TEXT PRIMARY KEY,work TEXT NOT NULL,origin TEXT NOT NULL,
 starts REAL NOT NULL,ends REAL NOT NULL,agreement TEXT NOT NULL,audience TEXT NOT NULL,version TEXT NOT NULL,
 secret_hash TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS screening_session(token_hash TEXT PRIMARY KEY,grant_id TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS route(kind TEXT,slug TEXT,destination TEXT,description TEXT,public_record INTEGER,
 current_action TEXT,PRIMARY KEY(kind,slug));
CREATE TABLE IF NOT EXISTS pending_route(kind TEXT,slug TEXT,action_id TEXT,PRIMARY KEY(kind,slug));
CREATE TABLE IF NOT EXISTS case_hold(case_id TEXT PRIMARY KEY,work TEXT NOT NULL,active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS case_change(action_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,work TEXT NOT NULL,active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS complaint(id TEXT PRIMARY KEY,work TEXT NOT NULL,secret_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS case_event(id INTEGER PRIMARY KEY,case_id TEXT,kind TEXT,details TEXT,actor TEXT,at REAL);
"""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def create_app(root, database, operator, institution, simulation=None):
    if len(operator) < 24:
        raise ValueError(
            "Supply a private operator credential of at least 24 characters"
        )
    verify(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf8"))
    if manifest["institution"] != institution:
        raise ValueError("Collection does not belong to this institution")
    works = {work["id"]: work for work in manifest["works"]}
    db = sqlite3.connect(database)
    db.executescript(
        SCHEMA + library_worker.SCHEMA + library_folders.SCHEMA + library_trash.SCHEMA
    )
    db.execute("CREATE TABLE IF NOT EXISTS owner(institution TEXT PRIMARY KEY)")
    db.execute("BEGIN IMMEDIATE")
    existing = db.execute("SELECT institution FROM owner").fetchone()
    if existing and existing[0] != institution:
        db.close()
        raise ValueError("Policy database belongs to another institution")
    db.execute("INSERT OR IGNORE INTO owner VALUES(?)", (institution,))
    db.commit()
    db.close()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 65536

    @contextmanager
    def tx():
        con = sqlite3.connect(database, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def bearer():
        return request.headers.get("Authorization", "").removeprefix("Bearer ")

    operator_keys = {
        "operator": operator,
        **json.loads(os.environ.get("CUEPORCH_OPERATOR_KEYS", "{}")),
    }
    if any(
        not isinstance(value, str) or len(value) < 24
        for value in operator_keys.values()
    ):
        raise ValueError("Each operator credential must contain at least 24 characters")

    def operator_name():
        return next(
            (
                name
                for name, key in operator_keys.items()
                if hmac.compare_digest(bearer().encode(), key.encode())
            ),
            None,
        )

    def staff():
        actor = operator_name()
        if not actor:
            abort(403)
        return actor

    def action(con, uid, step, payload):
        if step not in STEPS:
            abort(400)
        encoded = json.dumps(payload, sort_keys=True)
        row = con.execute("SELECT * FROM action WHERE id=?", (uid,)).fetchone()
        if row:
            if row["step"] != step or row["payload"] != encoded:
                abort(409)
            return row["status"]
        gate = con.execute(
            "SELECT required FROM policy WHERE step=?", (step,)
        ).fetchone()
        status = "waiting" if gate and gate[0] else "eligible"
        decision = None if status == "waiting" else "automatic"
        con.execute(
            "INSERT INTO action VALUES(?,?,?,?,?,?,?)",
            (uid, step, encoded, status, decision, None, time.time()),
        )
        return status

    def apply_routes(con):
        pending = con.execute("""SELECT p.*,a.payload FROM pending_route p JOIN action a
            ON a.id=p.action_id WHERE a.status='eligible'""").fetchall()
        for row in pending:
            if library_trash.hidden(con, "work", row["slug"]):
                continue
            data = json.loads(row["payload"])
            con.execute(
                """INSERT INTO route VALUES(?,?,?,?,?,?)
                ON CONFLICT(kind,slug) DO UPDATE SET destination=excluded.destination,
                description=excluded.description,public_record=MAX(route.public_record,excluded.public_record),
                current_action=excluded.current_action""",
                (
                    row["kind"],
                    row["slug"],
                    data["destination"],
                    data.get("description", ""),
                    bool(data.get("public_record")),
                    row["action_id"],
                ),
            )
            con.execute(
                "DELETE FROM pending_route WHERE kind=? AND slug=?",
                (row["kind"], row["slug"]),
            )

        changes = con.execute("""SELECT c.* FROM case_change c JOIN action a ON a.id=c.action_id
            WHERE a.status='eligible' ORDER BY a.created""").fetchall()
        for change in changes:
            con.execute(
                "INSERT OR REPLACE INTO case_hold VALUES(?,?,?)",
                (change["case_id"], change["work"], change["active"]),
            )
            con.execute(
                "DELETE FROM case_change WHERE action_id=?", (change["action_id"],)
            )

    @app.after_request
    def cache(response):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/operator")
    def console():
        return render_template("console.html", institution=institution)

    # Keep credentials in process memory. After a process restart a new request can
    # resume from disk using the same source identity and work checkpoints.
    transfer_lock = Lock()
    transfer_directory = database.parent / (database.stem + "-vimeo")
    transfer_status = {"state": "idle"}

    @app.route("/operator/migration")
    def migration_screen():
        return render_template("migration_screen.html")

    @app.get("/api/migration")
    def migration_status():
        staff()
        return dict(transfer_status)

    @app.post("/api/migration")
    def begin_transfer():
        from vimeo_transfer import Source, migrate

        staff()
        dry = request.json.get("dry_run")
        if type(dry) is not bool:
            abort(400)
        token = request.json.get("token") or os.environ.get("VIMEO_ACCESS_TOKEN")
        if not simulation and not token:
            abort(400, "Supply the account owner's Vimeo token")
        if not dry and not (transfer_directory / "inventory.json").exists():
            abort(409, "Preview the source inventory before transfer")
        if not transfer_lock.acquire(blocking=False):
            abort(409, "A transfer is already running")
        try:
            source = Source(simulation, token)
            uid = secrets.token_urlsafe(18)
            with tx() as con:
                if not dry:
                    action(con, uid, "migration", {"institution": institution})
            transfer_status.clear()
            transfer_status.update(state="running", action=uid)

            def worker():
                try:
                    if not dry:
                        while True:
                            with tx() as con:
                                state = con.execute(
                                    "SELECT status FROM action WHERE id=?", (uid,)
                                ).fetchone()[0]
                            if state == "rejected":
                                transfer_status.update(state="rejected")
                                return
                            if state == "eligible":
                                break
                            transfer_status.update(state="waiting")
                            time.sleep(0.5)
                    transfer_status.update(state="running")
                    migrate(institution, transfer_directory, source, dry)
                    filename = "inventory.json" if dry else "manifest.json"
                    data = json.loads(
                        (transfer_directory / filename).read_text(encoding="utf8")
                    )
                    if not dry:
                        from catalog import merge_collection

                        works.update(merge_collection(transfer_directory, root))
                        if os.environ.get("CUEPORCH_PEERTUBE_ORIGIN"):
                            with tx() as con:
                                for item in data["works"]:
                                    if any(
                                        f["role"] in ("original", "playback")
                                        for f in item["files"]
                                    ):
                                        merged = works[item["id"]]
                                        prior = con.execute(
                                            "SELECT want_public,scheduled,kind FROM publication_job WHERE work=? ORDER BY rowid DESC LIMIT 1",
                                            (item["id"],),
                                        ).fetchone()
                                        permitted = merged.get("source_privacy") in (
                                            "public",
                                            "anybody",
                                        ) and merged.get("source_embed_policy") in (
                                            "public",
                                            "anywhere",
                                        )
                                        library_worker.enqueue(
                                            con,
                                            merged,
                                            bool(prior and prior[0] and permitted),
                                            prior[1] if prior else 0,
                                            prior[2] if prior else "work",
                                        )
                    transfer_status.update(
                        state="inventoried" if dry else "transferred",
                        works=data["works"],
                    )
                except (ValueError, OSError, KeyError) as exc:
                    transfer_status.update(
                        state="failed",
                        detail=type(exc).__name__
                        + ". Retry from the source inventory.",
                    )
                finally:
                    transfer_lock.release()

            Thread(target=worker, daemon=True).start()
        except BaseException:
            transfer_lock.release()
            raise
        return {"action": uid}, 202

    @app.post("/api/station-packages")
    def ingest_station():
        from civiccast_destination import unpack
        from catalog import merge_collection

        station_key = os.environ.get("CUEPORCH_STATION_KEY")
        if not station_key or not hmac.compare_digest(
            bearer().encode(), station_key.encode()
        ):
            abort(403)
        if not os.environ.get("CUEPORCH_PEERTUBE_ORIGIN"):
            abort(503, "Configure PeerTube before sending station packages")
        if request.mimetype != "application/zip":
            abort(415)
        kind = request.args.get("kind", "work")
        if kind not in ("meeting", "work"):
            abort(400)
        public = request.args.get("public", "false")
        if public not in ("true", "false"):
            abort(400)
        request.max_content_length = int(
            os.environ.get("CUEPORCH_MAX_UPLOAD_BYTES", str(8 * 1024**3))
        )
        with tempfile.TemporaryDirectory(dir=database.parent) as tmp:
            folder = Path(tmp)
            package = folder / "handoff.zip"
            with package.open("wb") as f:
                shutil.copyfileobj(request.stream, f, 1024 * 1024)
            try:
                staged, work = unpack(package, folder, institution, public == "true")
            except (KeyError, ValueError, zipfile.BadZipFile):
                abort(422, "Invalid package or checksum")
            works.update(merge_collection(staged, root))
            with tx() as con:
                job = library_worker.enqueue(
                    con, works[work["id"]], public == "true", kind=kind
                )
        return {"work": work["id"], "job": job, "status_url": "/api/delivery"}, 202

    @app.get("/submit")
    def submission_form():
        return render_template("submission_form.html")

    @app.post("/api/submissions")
    def submit():
        from catalog import build_intake, merge_collection

        creator = os.environ.get("CUEPORCH_CREATOR_KEY", "")
        if not creator or not hmac.compare_digest(bearer().encode(), creator.encode()):
            abort(403)
        request.max_content_length = int(
            os.environ.get("CUEPORCH_MAX_UPLOAD_BYTES", str(8 * 1024**3))
        )
        title = request.form.get("title", "").strip()
        master = request.files.get("master")
        if not title or not master:
            abort(400)
        terms = request.form.get("terms", "private")
        if terms not in ("private", "public"):
            abort(400)
        try:
            scheduled = float(request.form.get("scheduled", "0"))
            if not math.isfinite(scheduled) or scheduled < 0:
                raise ValueError()
        except ValueError:
            abort(400, "Invalid publication time")
        uid = str(uuid.uuid4())
        with tempfile.TemporaryDirectory(dir=database.parent) as tmp:
            folder = Path(tmp)
            entries = []
            for field, role, allowed in [
                ("master", "original", {"mp4", "mov", "webm", "mkv"}),
                ("caption", "caption", {"vtt"}),
                ("poster", "poster", {"png", "jpg", "jpeg"}),
            ]:
                file = request.files.get(field)
                if not file or not file.filename:
                    continue
                fmt = Path(file.filename).suffix.lower().lstrip(".")
                if fmt not in allowed:
                    abort(400)
                name = field + "." + fmt
                file.save(folder / name)
                entry = dict(role=role, path=name, format=fmt)
                if role == "caption":
                    entry.update(language="en", provenance="creator supplied")
                entries.append(entry)
            master_entry = next(item for item in entries if item["role"] == "original")
            if terms == "private":
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-i",
                        str(folder / master_entry["path"]),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-map_metadata",
                        "-1",
                        "-vf",
                        "scale=w='min(1280,iw)':h=-2",
                        "-c:v",
                        "libx264",
                        "-threads",
                        "2",
                        "-crf",
                        "23",
                        "-c:a",
                        "aac",
                        "-movflags",
                        "+faststart",
                        str(folder / "screening.mp4"),
                    ],
                    capture_output=True,
                )
                if result.returncode:
                    abort(422, "Unable to prepare the screening copy")
                entries.append(
                    dict(
                        role="playback",
                        path="screening.mp4",
                        format="mp4",
                        provenance="screening derivative",
                    )
                )
            staged = build_intake(
                folder,
                institution,
                uid,
                title,
                entries,
                dict(
                    credits=request.form.get("credits"),
                    description=request.form.get("description"),
                    source_privacy=terms,
                    source_embed_policy="anywhere" if terms == "public" else "private",
                ),
            )
            incoming = json.loads(
                (staged / "manifest.json").read_text(encoding="utf8")
            )["works"][0]
            from caption_adapter import caption_work

            caption_work(incoming, staged)
            from cueporch import dump

            submitted = [incoming]
            trailer = request.files.get("trailer")
            if trailer and trailer.filename:
                trailer_format = Path(trailer.filename).suffix.lower().lstrip(".")
                if trailer_format not in ("mp4", "mov", "webm", "mkv"):
                    abort(400, "Unsupported trailer format")
                trailer_folder = folder / "trailer"
                trailer_folder.mkdir()
                trailer_path = trailer_folder / ("trailer." + trailer_format)
                trailer.save(trailer_path)
                public_trailer = request.form.get("public_trailer") == "on"
                trailer_collection = build_intake(
                    trailer_folder,
                    institution,
                    uid + "-trailer",
                    title + " (trailer)",
                    [
                        dict(
                            role="playback",
                            path=trailer_path.name,
                            format=trailer_format,
                        )
                    ],
                    dict(
                        related_work=incoming["id"],
                        source_privacy="public" if public_trailer else "private",
                        source_embed_policy="anywhere" if public_trailer else "private",
                    ),
                )
                merge_collection(trailer_collection, staged)
                submitted.extend(
                    json.loads(
                        (trailer_collection / "manifest.json").read_text(
                            encoding="utf8"
                        )
                    )["works"]
                )
            dump(
                staged / "manifest.json",
                dict(schema=1, institution=institution, works=submitted),
            )
            updated = merge_collection(staged, root)
            works.update(updated)
        with tx() as con:
            status = "received"
            job = (
                library_worker.enqueue(con, incoming, terms == "public", scheduled)
                if os.environ.get("CUEPORCH_PEERTUBE_ORIGIN")
                else None
            )
            trailer_job = None
            if len(submitted) > 1 and os.environ.get("CUEPORCH_PEERTUBE_ORIGIN"):
                trailer_job = library_worker.enqueue(
                    con, submitted[1], public_trailer, scheduled
                )
        return {
            "work": incoming["id"],
            "action": uid,
            "status": status,
            "delivery": job,
            "trailer_delivery": trailer_job,
        }, 201

    @app.get("/api/policy")
    def policies():
        staff()
        with tx() as con:
            values = dict(con.execute("SELECT step,required FROM policy"))
        return {step: bool(values.get(step)) for step in STEPS}

    @app.put("/api/policy/<step>")
    def policy(step):
        staff()
        required = request.json.get("required")
        if step not in STEPS or type(required) is not bool:
            abort(400)
        with tx() as con:
            con.execute("INSERT OR REPLACE INTO policy VALUES(?,?)", (step, required))
            if required:
                con.execute(
                    """UPDATE action SET status='waiting',decision=NULL WHERE step=? AND decision='automatic'
                    AND EXISTS(SELECT 1 FROM publication_job j WHERE action.id=j.id||'-'||action.step
                    AND j.status IN ('queued','waiting'))""",
                    (step,),
                )
            if not required:
                con.execute(
                    "UPDATE action SET status='eligible',decision='automatic' WHERE step=? AND status='waiting'",
                    (step,),
                )
            apply_routes(con)
        return {"required": required}

    @app.put("/api/actions/<uid>")
    def enqueue(uid):
        staff()
        with tx() as con:
            status = action(con, uid, request.json["step"], request.json["payload"])
        return {"status": status}

    @app.get("/api/actions")
    def waiting():
        staff()
        with tx() as con:
            return [
                dict(row)
                for row in con.execute(
                    "SELECT id,step,payload FROM action WHERE status='waiting' ORDER BY created"
                )
            ]

    @app.get("/api/actions/<uid>")
    def get_action(uid):
        staff()
        with tx() as con:
            row = con.execute("SELECT * FROM action WHERE id=?", (uid,)).fetchone()
            if not row:
                abort(404)
            return dict(row)

    @app.post("/api/actions/<uid>/decision")
    def decide(uid):
        staff()
        decision = request.json.get("decision")
        if decision not in ("approve", "reject"):
            abort(400)
        with tx() as con:
            changed = con.execute(
                "UPDATE action SET status=?,decision=?,actor=? WHERE id=? AND status='waiting'",
                (
                    "eligible" if decision == "approve" else "rejected",
                    decision,
                    operator_name(),
                    uid,
                ),
            ).rowcount
            if not changed:
                abort(409)
            apply_routes(con)
        return "", 204

    @app.get("/operator/exhibitions")
    def exhibitions():
        return render_template("exhibitions.html")

    @app.get("/api/screening-copies")
    def screening_copies():
        staff()
        catalog = json.loads((root / "manifest.json").read_text(encoding="utf8"))
        works.update({w["id"]: w for w in catalog["works"]})
        with tx() as con:
            deleted = {
                row[0]
                for row in con.execute(
                    "SELECT item FROM library_deleted_item WHERE kind='work'"
                )
            }
        return [
            dict(work=w["id"], version=f["sha256"], title=w["title"])
            for w in catalog["works"]
            if w["id"] not in deleted
            for f in w["files"]
            if f["role"] == "playback" and f["format"] == "mp4"
        ]

    @app.get("/api/grants")
    def list_grants():
        staff()
        with tx() as con:
            return [
                dict(row)
                for row in con.execute("""SELECT g.id,g.work,g.origin,g.starts,g.ends,g.audience,
                g.agreement,g.version,g.revoked,a.status,EXISTS(SELECT 1 FROM case_hold h WHERE h.work=g.work AND h.active=1) AS suspended
                FROM grant_access g JOIN action a USING(id) ORDER BY g.starts DESC""")
            ]

    @app.post("/api/grants")
    def grant():
        staff()
        data = request.json
        work = works.get(data.get("work"))
        if not work:
            abort(404)
        version = data.get("version")
        if not any(
            f["role"] == "playback" and f["format"] == "mp4" and f["sha256"] == version
            for f in work["files"]
        ):
            abort(400, "Grant requires a designated playback derivative")
        origin = data.get("origin", "")
        parts = urlsplit(origin)
        if (
            not parts.hostname
            or parts.scheme not in ("http", "https")
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
            or (
                parts.scheme == "http"
                and parts.hostname not in ("127.0.0.1", "localhost")
            )
        ):
            abort(400)
        if not all(
            isinstance(data.get(k), str) and data[k].strip()
            for k in ("agreement", "audience")
        ):
            abort(400)
        if (
            not all(
                type(data.get(k)) in (int, float) and math.isfinite(data[k])
                for k in ("starts", "ends")
            )
            or not data["starts"] < data["ends"]
        ):
            abort(400)
        uid = secrets.token_urlsafe(20)
        secret = secrets.token_urlsafe(32)
        with tx() as con:
            if library_trash.hidden(con, "work", work["id"]):
                abort(404)
            state = action(con, uid, "grant", data)
            con.execute(
                "INSERT INTO grant_access VALUES(?,?,?,?,?,?,?,?,?,0)",
                (
                    uid,
                    work["id"],
                    origin,
                    data["starts"],
                    data["ends"],
                    data["agreement"],
                    data["audience"],
                    version,
                    digest(secret),
                ),
            )
        return {"id": uid, "exhibitor_secret": secret, "status": state}, 201

    @app.delete("/api/grants/<uid>")
    def revoke(uid):
        staff()
        with tx() as con:
            if not con.execute(
                "UPDATE grant_access SET revoked=1 WHERE id=?", (uid,)
            ).rowcount:
                abort(404)
        return "", 204

    def active(con, uid):
        now = time.time()
        row = con.execute(
            """SELECT grant_access.* FROM grant_access JOIN action USING(id)
        WHERE id=? AND revoked=0 AND starts<=? AND ends>? AND status='eligible'
        AND NOT EXISTS(SELECT 1 FROM case_hold h WHERE h.work=grant_access.work AND h.active=1)
        AND NOT EXISTS(SELECT 1 FROM library_deleted_item d WHERE d.kind='work' AND d.item=grant_access.work)""",
            (uid, now, now),
        ).fetchone()
        if not row:
            abort(403)
        return row

    @app.post("/api/grants/<uid>/session")
    def session(uid):
        with tx() as con:
            row = active(con, uid)
            if not hmac.compare_digest(row["secret_hash"], digest(bearer())):
                abort(403)
            token = secrets.token_urlsafe(32)
            duration = works[row["work"]].get("duration_seconds") or 4 * 3600
            expires = min(time.time() + max(300, duration + 300), row["ends"])
            con.execute(
                "DELETE FROM screening_session WHERE expires<=?", (time.time(),)
            )
            con.execute(
                "INSERT INTO screening_session VALUES(?,?,?)",
                (digest(token), uid, expires),
            )
        return {"embed": "/screen/" + token, "expires": expires}

    @app.get("/screen/<token>")
    @app.get("/screen/<token>/media")
    @app.get("/screen/<token>/status")
    @app.get("/screen/<token>/caption/<int:track_index>")
    def screening(token, track_index=None):
        with tx() as con:
            session = con.execute(
                "SELECT grant_id,expires FROM screening_session WHERE token_hash=? AND expires>?",
                (digest(token), time.time()),
            ).fetchone()
            if not session:
                abort(403)
            grant = active(con, session[0])
        versions = [
            works[grant["work"]],
            *works[grant["work"]].get("versions_history", []),
        ]
        selected = next(
            (
                v
                for v in versions
                if any(f["sha256"] == grant["version"] for f in v["files"])
            ),
            None,
        )
        if selected is None:
            abort(403)
        tracks = [
            f
            for f in selected["files"]
            if f["role"] == "caption" and f["format"] == "vtt"
        ]
        if request.path.endswith("/status") or track_index is not None:
            ref = urlsplit(request.referrer or "")
            if ref.path != "/screen/" + token or ref.netloc != request.host:
                abort(403)
            if track_index is None:
                return {
                    "remaining": max(
                        0, min(session["expires"], grant["ends"]) - time.time()
                    )
                }
            if track_index >= len(tracks):
                abort(404)
            return Response(
                (root / "objects" / tracks[track_index]["sha256"]).read_bytes(),
                mimetype="text/vtt",
            )
        if request.path.endswith("/media"):
            if (
                urlsplit(request.referrer or "").path != "/screen/" + token
                or urlsplit(request.referrer or "").netloc != request.host
            ):
                abort(403)
            file = root / "objects" / grant["version"]
            size = file.stat().st_size
            ranges = request.range
            if ranges:
                span = ranges.range_for_length(size)
                if span is None:
                    abort(416)
                first, stop = span
            else:
                first, stop = 0, size

            def chunks():
                with file.open("rb") as stream:
                    stream.seek(first)
                    remaining = stop - first
                    while remaining:
                        with tx() as con:
                            allowed = con.execute(
                                """SELECT 1 FROM screening_session s JOIN grant_access g
                            ON g.id=s.grant_id WHERE s.token_hash=? AND g.revoked=0 AND s.expires>? AND g.ends>?
                            AND NOT EXISTS(SELECT 1 FROM case_hold h WHERE h.work=g.work AND h.active=1)
                            AND NOT EXISTS(SELECT 1 FROM library_deleted_item d WHERE d.kind='work' AND d.item=g.work)""",
                                (digest(token), time.time(), time.time()),
                            ).fetchone()
                        if not allowed:
                            return
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            return
                        remaining -= len(chunk)
                        yield chunk

            response = Response(
                stream_with_context(chunks()),
                status=206 if ranges else 200,
                mimetype="video/mp4",
            )
            response.headers["Accept-Ranges"] = "bytes"
            response.headers["Content-Length"] = str(stop - first)
            if ranges:
                response.headers["Content-Range"] = f"bytes {first}-{stop - 1}/{size}"
        else:
            referring = urlsplit(request.referrer or "")
            if f"{referring.scheme}://{referring.netloc}" != grant["origin"]:
                abort(403)
            page = render_template(
                "screening.html",
                title=works[grant["work"]]["title"],
                src=request.path + "/media",
                base=request.path,
                tracks=tracks,
                remaining=max(0, min(session["expires"], grant["ends"]) - time.time()),
            )
            response = app.make_response(page)
        response.headers["Content-Security-Policy"] = (
            "frame-ancestors " + grant["origin"]
        )
        return response

    @app.put("/api/routes/<kind>/<slug>")
    def configure_route(kind, slug):
        staff()
        if kind not in ("work", "channel", "meeting") or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", slug
        ):
            abort(400)
        data = request.json
        dest = data.get("destination", "")
        parts = urlsplit(dest)
        if (
            parts.username
            or parts.password
            or not parts.hostname
            or (
                parts.scheme != "https"
                and not (
                    parts.scheme == "http"
                    and parts.hostname in ("localhost", "127.0.0.1")
                )
            )
            or not re.fullmatch(
                r"/(?:videos/embed/[A-Za-z0-9-]+|api/public/embed-player/(?:asset|channel)/[A-Za-z0-9_-]+)",
                parts.path,
            )
            or parts.query
            or parts.fragment
        ):
            abort(400, "Expected a PeerTube or CivicCast player URL")
        if not data.get("public_authorized") is True:
            abort(400, "Public routes require explicit public viewing rights")
        revision = data.get("revision")
        if not isinstance(revision, str) or not revision:
            abort(400)
        step = "replay" if kind == "meeting" else "publication"
        with tx() as con:
            if (
                library_trash.hidden(con, "work", slug)
                or con.execute(
                    """SELECT 1 FROM route r JOIN library_deleted_item d ON d.kind='work' AND d.item=r.slug
                   WHERE r.destination=?""",
                    (dest,),
                ).fetchone()
            ):
                abort(404)
            key = digest(json.dumps([kind, slug, revision]))
            previous = con.execute(
                "SELECT status FROM action WHERE id=?", (key,)
            ).fetchone()
            state = action(con, key, step, data)
            if state == "rejected":
                abort(409)
            if not previous:
                con.execute(
                    "INSERT OR REPLACE INTO pending_route VALUES(?,?,?)",
                    (kind, slug, key),
                )
            apply_routes(con)
        return {"action": key, "status": state}

    @app.get("/")
    def catalog():
        with tx() as con:
            routes = con.execute(
                """SELECT kind,slug,description FROM route
                   WHERE NOT EXISTS (SELECT 1 FROM library_deleted_item d WHERE d.kind='work' AND d.item=route.slug)
                   ORDER BY description,slug"""
            ).fetchall()
        return render_template("catalog.html", name=institution, rows=routes)

    @app.get("/watch/<kind>/<slug>")
    @app.get("/embed/<kind>/<slug>")
    def watch(kind, slug):
        with tx() as con:
            row = con.execute(
                """SELECT * FROM route WHERE kind=? AND slug=?
                   AND NOT EXISTS (SELECT 1 FROM library_deleted_item d WHERE d.kind='work' AND d.item=route.slug)""",
                (kind, slug),
            ).fetchone()
        if not row:
            abort(404)
        if request.path.startswith("/embed/"):
            return redirect(row["destination"], code=302)
        return render_template(
            "watch.html",
            title=row["description"] or row["slug"],
            url=row["destination"],
            slug=slug,
        )

    @app.get("/support")
    def support():
        return render_template(
            "support.html",
            work=request.args.get("work", ""),
            contact=os.environ.get(
                "CUEPORCH_SUPPORT_CONTACT", "Institutional support inbox"
            ),
            hours=os.environ.get("CUEPORCH_SUPPORT_HOURS", ""),
            escalation=os.environ.get("CUEPORCH_ESCALATION_CONTACT", ""),
            recovery=os.environ.get("CUEPORCH_RECOVERY_CONTACT", ""),
        )

    @app.post("/api/works/<work>/complaints")
    def complaint(work):
        if work not in works:
            abort(404)
        details = request.json.get("details", "").strip()
        if not details:
            abort(400)
        uid = secrets.token_urlsafe(20)
        token = secrets.token_urlsafe(32)
        with tx() as con:
            con.execute(
                "INSERT INTO complaint VALUES(?,?,?)", (uid, work, digest(token))
            )
            con.execute(
                "INSERT INTO case_event(case_id,kind,details,actor,at) VALUES(?,?,?,?,?)",
                (uid, "notice", details, "reporter", time.time()),
            )
        return {"case": uid, "case_key": token}, 201

    @app.get("/api/cases")
    def inbox():
        staff()
        with tx() as con:
            return [
                dict(row)
                for row in con.execute(
                    "SELECT id,work FROM complaint ORDER BY rowid DESC"
                )
            ]

    @app.route("/api/cases/<uid>", methods=["GET", "POST"])
    def case(uid):
        with tx() as con:
            row = con.execute("SELECT * FROM complaint WHERE id=?", (uid,)).fetchone()
            if not row:
                abort(404)
            operator_access = operator_name()
            if not operator_access and not hmac.compare_digest(
                row["secret_hash"], digest(bearer())
            ):
                abort(403)
            if request.method == "GET":
                events = [
                    dict(e)
                    for e in con.execute(
                        "SELECT kind,details,actor,at FROM case_event WHERE case_id=? ORDER BY id",
                        (uid,),
                    )
                ]
                return {"work": row["work"], "events": events}
            kind = request.json.get("kind")
            details = request.json.get("details", "").strip()
            if (
                kind
                not in (
                    ("decision", "restoration", "context")
                    if operator_access
                    else ("appeal", "context")
                )
                or not details
            ):
                abort(400)
            if operator_access and (
                kind == "restoration"
                or (
                    kind == "decision"
                    and request.json.get("suspend_screenings") is True
                )
            ):
                change = secrets.token_urlsafe(20)
                active_hold = kind == "decision"
                action(
                    con,
                    change,
                    "moderation" if active_hold else "restoration",
                    {"case": uid, "work": row["work"], "reason": details},
                )
                con.execute(
                    "INSERT INTO case_change VALUES(?,?,?,?)",
                    (change, uid, row["work"], active_hold),
                )
                apply_routes(con)
            con.execute(
                "INSERT INTO case_event(case_id,kind,details,actor,at) VALUES(?,?,?,?,?)",
                (uid, kind, details, operator_access or "reporter", time.time()),
            )
        return "", 204

    def read_library_works():
        return json.loads((root / "manifest.json").read_text(encoding="utf8"))["works"]

    library_folders.register(app, tx, staff, read_library_works)
    library_trash.register(app, tx, staff, read_library_works)

    @app.get("/api/library")
    def library():
        actor = staff()
        data = read_library_works()
        with tx() as con:
            jobs = con.execute(
                "SELECT j.work,j.status,j.remote,j.error,r.kind AS route_kind FROM publication_job j LEFT JOIN route r ON r.slug=j.work AND r.kind=j.kind ORDER BY j.rowid"
            ).fetchall()
            placement = dict(con.execute("SELECT work,folder FROM library_placement"))
            deleted = {
                row[0]
                for row in con.execute(
                    "SELECT item FROM library_deleted_item WHERE kind='work'"
                )
            }
            starred = {
                row[0]
                for row in con.execute(
                    "SELECT item FROM library_star WHERE kind='work' AND actor=?",
                    (actor,),
                )
            }
        latest = {r["work"]: dict(r) for r in jobs}
        return [
            dict(
                id=w["id"],
                title=w["title"],
                folder=placement.get(w["id"]),
                source_privacy=w.get("source_privacy"),
                duration_seconds=w.get("duration_seconds"),
                creator=w.get("source_creator"),
                added_at=w.get("library_added_at"),
                modified_at=w.get("source_modified_time"),
                version_count=1 + len(w.get("versions_history", [])),
                starred=w["id"] in starred,
                source=w["source_url"],
                quality=w["disposition"],
                exceptions=w.get("transfer_issues", []),
                public_eligible=w.get("source_privacy") in ("public", "anybody")
                and w.get("source_embed_policy") in ("anywhere", "public"),
                delivery=latest.get(w["id"]),
            )
            for w in data
            if w["id"] not in deleted
        ]

    @app.get("/api/embed-map.csv")
    def embed_map():
        staff()
        data = json.loads((root / "manifest.json").read_text(encoding="utf8"))
        with tx() as con:
            routes = {
                r["slug"]: r["kind"]
                for r in con.execute(
                    """SELECT kind,slug FROM route WHERE NOT EXISTS
                       (SELECT 1 FROM library_deleted_item d WHERE d.kind='work' AND d.item=route.slug)"""
                )
            }
        buf = io.StringIO(newline="")
        csvout = csv.writer(buf)
        csvout.writerow(
            [
                "source_id",
                "old_url",
                "work_id",
                "watch_url",
                "embed_url",
                "disposition",
                "external_page",
            ]
        )
        base = os.environ.get("CUEPORCH_PUBLIC_URL", request.url_root).rstrip("/")
        for w in data["works"]:
            kind = routes.get(w["id"])
            prefix = kind + "/" + w["id"] if kind else ""
            for site in w.get("known_embed_pages", []) or [""]:
                csvout.writerow(
                    [
                        w["source_id"],
                        w["source_url"],
                        w["id"],
                        base + "/watch/" + prefix if kind else "",
                        base + "/embed/" + prefix if kind else "",
                        w["disposition"],
                        site,
                    ]
                )
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=cueporch-embed-map.csv"
            },
        )

    @app.get("/operator/library")
    def library_console():
        return render_template("library_console.html")

    @app.get("/api/delivery")
    def delivery_jobs():
        staff()
        with tx() as con:
            return [
                dict(row)
                for row in con.execute(
                    "SELECT * FROM publication_job ORDER BY rowid DESC"
                )
            ]

    @app.post("/api/delivery/<work_id>")
    def deliver_work(work_id):
        staff()
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf8"))
        works.update({w["id"]: w for w in manifest["works"]})
        work = works.get(work_id)
        if not work:
            abort(404)
        if not os.environ.get("CUEPORCH_PEERTUBE_ORIGIN"):
            abort(503, "Configure the PeerTube destination")
        try:
            with tx() as con:
                if library_trash.hidden(con, "work", work_id):
                    abort(404)
                public = request.json.get("public", False)
                scheduled = request.json.get("scheduled", 0)
                kind = request.json.get("kind", "work")
                if (
                    type(public) is not bool
                    or kind not in ("work", "meeting")
                    or type(scheduled) not in (float, int)
                    or not math.isfinite(scheduled)
                ):
                    abort(400)
                uid = library_worker.enqueue(con, work, public, scheduled, kind)
                con.execute(
                    "UPDATE publication_job SET status='queued',error=NULL WHERE id=? AND status='failed'",
                    (uid,),
                )
        except ValueError as exc:
            abort(400, str(exc))
        return {"job": uid}, 202

    @app.post("/api/migration/publish-eligible")
    def publish_eligible_imports():
        staff()
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf8"))
        queued = 0
        with tx() as con:
            for item in manifest["works"]:
                if (
                    item["provider"] != "vimeo"
                    or library_trash.hidden(con, "work", item["id"])
                    or item.get("source_privacy") not in ("public", "anybody")
                    or item.get("source_embed_policy") not in ("public", "anywhere")
                    or item.get("transfer_issues")
                ):
                    continue
                last = con.execute(
                    "SELECT status,want_public FROM publication_job WHERE work=? ORDER BY rowid DESC LIMIT 1",
                    (item["id"],),
                ).fetchone()
                if last and last[0] == "playable" and not last[1]:
                    library_worker.enqueue(con, item, True)
                    queued += 1
        return {"queued": queued}, 202

    library_worker.start(app, root, database, tx, action, apply_routes)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path)
    parser.add_argument("database", type=Path)
    parser.add_argument("institution")
    parser.add_argument("--port", type=int, default=19001)
    parser.add_argument("--simulate", help="Local rehearsal source origin")
    args = parser.parse_args()
    serve(
        create_app(
            args.collection,
            args.database,
            os.environ["CUEPORCH_OPERATOR_KEY"],
            args.institution,
            args.simulate,
        ),
        host="127.0.0.1",
        port=args.port,
        max_request_body_size=int(
            os.environ.get("CUEPORCH_MAX_UPLOAD_BYTES", str(8 * 1024**3))
        ),
    )
