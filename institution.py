"""Institution-scoped operator policy, screening grants and complaint records."""

import argparse
from collections import Counter
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
import time
import tempfile
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import urlsplit

from flask import (
    Flask,
    Response,
    abort,
    g,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)
from waitress import serve

from cueporch import dump, verify
import analytics_reports
import account_email
import account_recovery
import account_totp
import account_email_factor
import audience_accounts
import audience_comments
import content_rating
import event_api
import event_registrants
import event_webhooks
from browser_account_security import same_origin, secure_site
import community_analytics
import creator_profiles
import library_actions
import library_appearance
import library_caption_jobs
import library_ai_drafts
import library_video_questions
import studio_scripts
import team_ai_policy
import library_details
import library_discovery
import library_embed
import library_events
import library_folder_privacy
import library_folders
import library_file_links
import library_languages
import library_interactivity
import library_ratings
import library_registration
import library_showcases
import library_analytics
import library_peer_handoff
import library_privacy
import library_share
import library_thumbnail
import library_timeline
import library_trash
import library_upload
import library_worker
import media_bandwidth
import review_links
import review_push
import subtitle_translation_batches
import team_accounts
import team_saml
import team_scim
import team_group_roles
import team_sessions
import team_brand_kits
import team_branding
import team_sharing_policy
import team_workspaces
import timeline_jobs
import vimeo_privacy
import workspace_help
import workspace_updates
from review_settings import new_upload as create_upload_review

STEPS = (
    "intake",
    "caption",
    "translation",
    *library_ai_drafts.FEATURES,
    studio_scripts.STEP,
    "publication",
    "replay",
    "migration",
    "grant",
    "catalog",
    "moderation",
    audience_comments.STEP,
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
    with closing(sqlite3.connect(database, timeout=30)) as db, db:
        db.executescript(
            "BEGIN IMMEDIATE;\n"
            + SCHEMA
            + library_worker.SCHEMA
            + library_folders.SCHEMA
            + library_file_links.SCHEMA
            + library_folder_privacy.SCHEMA
            + library_peer_handoff.SCHEMA
            + library_privacy.SCHEMA
            + library_share.SCHEMA
            + library_trash.SCHEMA
            + library_actions.SCHEMA
            + library_appearance.SCHEMA
            + library_caption_jobs.SCHEMA
            + library_ai_drafts.SCHEMA
            + library_video_questions.SCHEMA
            + studio_scripts.SCHEMA
            + team_ai_policy.SCHEMA
            + subtitle_translation_batches.SCHEMA
            + library_details.SCHEMA
            + library_discovery.SCHEMA
            + audience_accounts.SCHEMA
            + account_totp.SCHEMA
            + account_email.SCHEMA
            + account_email_factor.SCHEMA
            + account_recovery.SCHEMA
            + team_accounts.SCHEMA
            + team_saml.SCHEMA
            + team_scim.SCHEMA
            + team_group_roles.SCHEMA
            + team_sessions.SCHEMA
            + team_branding.SCHEMA
            + team_brand_kits.SCHEMA
            + team_sharing_policy.SCHEMA
            + team_workspaces.SCHEMA
            + workspace_help.SCHEMA
            + workspace_updates.SCHEMA
            + library_interactivity.SCHEMA
            + library_ratings.SCHEMA
            + library_registration.SCHEMA
            + library_events.SCHEMA
            + event_api.SCHEMA
            + event_webhooks.SCHEMA
            + library_showcases.SCHEMA
            + library_analytics.SCHEMA
            + media_bandwidth.SCHEMA
            + library_timeline.SCHEMA
            + timeline_jobs.SCHEMA
            + library_thumbnail.SCHEMA
            + review_links.SCHEMA
            + "CREATE TABLE IF NOT EXISTS owner(institution TEXT PRIMARY KEY);"
        )
        existing = db.execute("SELECT institution FROM owner").fetchone()
        if existing and existing[0] != institution:
            raise ValueError("Policy database belongs to another institution")
        db.execute("INSERT OR IGNORE INTO owner VALUES(?)", (institution,))
        team_accounts.initialize_permissions(db)
        event_registrants.initialize(db)
        team_sessions.initialize(db)
        library_caption_jobs.initialize(db)
        library_ai_drafts.initialize(db)
        workspace_updates.initialize(db)
    app = Flask(__name__)
    app.extensions["account_reset_delivery"] = account_recovery.ResetDelivery()
    app.config["MAX_CONTENT_LENGTH"] = 65536
    app.config["WORKSPACE_COOKIE_SCOPE"] = hmac.digest(
        operator.encode(), b"CuePorch workspace cookie", "sha256",
    ).hex()[:32]

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

    team_origin = library_analytics.source_origin(os.environ.get("CUEPORCH_PUBLIC_URL", ""))

    def team_member(con=None):
        if request.headers.get("Authorization") != "Bearer " + team_accounts.SESSION_MARKER:
            return None
        secure = secure_site(team_origin)
        same_origin(secure)
        if con is not None:
            return team_accounts.current(con, secure)
        with closing(sqlite3.connect(database, timeout=30)) as lookup:
            lookup.row_factory = sqlite3.Row
            with lookup:
                return team_accounts.current(lookup, secure)

    def team_contributor(con=None):
        member = team_member(con)
        if not member or member["role"] not in (*team_accounts.CONTRIBUTORS, *team_accounts.MANAGERS):
            abort(403)
        return member

    def operator_name(con=None):
        if actor := g.get("event_api_actor"):
            return actor
        actor = next(
            (
                name
                for name, key in operator_keys.items()
                if hmac.compare_digest(bearer().encode(), key.encode())
            ),
            None,
        )
        if actor is not None:
            return actor
        member = team_member(con)
        return team_accounts.actor_id(member) if member and member["role"] in team_accounts.MANAGERS else None

    def staff():
        actor = operator_name()
        if not actor:
            abort(403)
        return actor

    def credential_owner():
        actor = staff()
        member = team_member()
        if member and member["role"] != "owner":
            abort(403, "Only the workspace Owner can manage event API credentials.")
        return actor

    workspace_brand = team_branding.register(app, tx, staff, institution, team_origin)
    ai_actor = team_ai_policy.register(app, tx, team_origin, operator_name, team_member)
    sharing_access, enforce_sharing = team_sharing_policy.register(app, tx, team_origin, team_member, operator_name)
    workspace_help.register(app, tx, team_origin, team_member, operator_name,
                            os.environ.get("CUEPORCH_SUPPORT_CONTACT", "Institutional support inbox"),
                            os.environ.get("CUEPORCH_SUPPORT_HOURS", ""))
    workspace_updates.register(app, tx, team_origin, team_member, operator_name)

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
        library_privacy.apply_pending(con)

    @app.after_request
    def cache(response):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.get("/operator")
    def console():
        with tx() as con:
            brand = team_branding.read(con, institution)
            _, picks = library_discovery.picks(con)
            entries = public_listing(con) if picks else {}
            featured = [library_discovery.entry_payload(entries[item]) for item in picks if item in entries]
        return render_template("console.html", brand=brand, featured=featured[:6])

    @app.get("/operator/search")
    def operator_search():
        query = request.args.get("q", "").strip()[:120]
        if request.args.get("scope") == "watch":
            return redirect(url_for("catalog", q=query))
        return redirect(url_for("library_console", q=query))

    # Keep credentials in process memory. After a process restart a new request can
    # resume from disk using the same source identity and work checkpoints.
    transfer_lock = Lock()
    transfer_directory = database.parent / (database.stem + "-vimeo")
    transfer_inventory = transfer_directory / "inventory.json"
    transfer_progress = database.with_name(database.stem + "-vimeo-progress.json")
    transfer_status = {"state": "idle"}
    try:
        saved = json.loads(transfer_progress.read_text(encoding="utf8"))
        if isinstance(saved, dict) and saved.get("state") in (
            "running", "waiting", "inventoried", "transferred", "rejected", "failed"
        ):
            transfer_status = saved
    except FileNotFoundError:
        if transfer_inventory.is_file():
            transfer_status = {"state": "inventoried"}
    except (ValueError, OSError):
        app.logger.warning("Could not read the saved Vimeo migration status")
    if transfer_status["state"] in ("running", "waiting"):
        transfer_status = {
            "state": "interrupted",
            "detail": "CuePorch restarted. Enter the Vimeo token again. Preview if no inventory is available.",
        }

    def update_transfer(state, uid, detail=None):
        nonlocal transfer_status
        status = dict(state=state, action=uid)
        if detail:
            status["detail"] = detail
        try:
            dump(transfer_progress, status)
        except OSError:
            app.logger.warning("Could not save the Vimeo migration status")
        transfer_status = status

    @app.route("/operator/migration")
    def migration_screen():
        return render_template("migration_screen.html")

    @app.get("/api/migration")
    def migration_status():
        staff()
        page = request.args.get("page", default=0, type=int)
        if "page" in request.args and page < 1:
            abort(400, "Choose a positive inventory page number.")
        status = dict(transfer_status)
        state = status["state"]
        if state in ("inventoried", "transferred") or (state == "interrupted" and transfer_inventory.is_file()):
            snapshot = transfer_directory / "manifest.json" if state == "transferred" else transfer_inventory
            try:
                data = json.loads(snapshot.read_text(encoding="utf8"))
                showcases = data.get("showcases", [])
                if state == "transferred" and showcases:
                    with tx() as con:
                        targets = library_showcases.imported_targets(con)
                    for showcase in showcases:
                        showcase["target"] = targets.get(showcase["uri"])
                entries = data["works"]
                counts = Counter(work["disposition"] for work in entries)
                status.update(total=len(entries), counts=dict(counts),
                              metadata_incomplete=sum(bool(work.get("unavailable_metadata")) for work in entries),
                              transfer_incomplete=sum(bool(work.get("transfer_issues")) for work in entries))
                if page:
                    size = 100
                    pages = max(1, (len(entries) + size - 1) // size)
                    page = min(page, pages)
                    entries = entries[(page - 1) * size:page * size]
                    status.update(page=page, pages=pages)
                status.update(works=entries, showcases=showcases,
                              showcase_issue=data.get("showcase_issue"))
            except (ValueError, OSError, KeyError):
                return dict(state="failed", detail="The saved inventory is unavailable. Preview the transfer again.")
        return status

    @app.post("/api/migration")
    def begin_transfer():
        from vimeo_transfer import Source, SourceFailure, migrate

        staff()
        body = request.get_json()
        if not isinstance(body, dict):
            abort(400, "Provide a migration request.")
        dry = body.get("dry_run")
        if type(dry) is not bool:
            abort(400)
        token = body.get("token") or os.environ.get("VIMEO_ACCESS_TOKEN")
        if token is not None and not isinstance(token, str):
            abort(400, "Enter a Vimeo token as text.")
        if not simulation and not token:
            abort(400, "Supply the account owner's Vimeo token")
        if not dry and not transfer_inventory.is_file():
            abort(409, "Preview the source inventory before transfer")
        if not transfer_lock.acquire(blocking=False):
            abort(409, "A transfer is already running")
        try:
            source = Source(simulation, token)
            uid = secrets.token_urlsafe(18)
            with tx() as con:
                if not dry:
                    action(con, uid, "migration", {"institution": institution})
            update_transfer("running", uid)

            def track(processed, total):
                nonlocal transfer_status
                transfer_status = dict(state="running", action=uid, processed=processed, total=total)

            def worker():
                try:
                    if not dry:
                        while True:
                            with tx() as con:
                                state = con.execute(
                                    "SELECT status FROM action WHERE id=?", (uid,)
                                ).fetchone()[0]
                            if state == "rejected":
                                update_transfer("rejected", uid)
                                return
                            if state == "eligible":
                                break
                            if transfer_status["state"] != "waiting":
                                update_transfer("waiting", uid)
                            time.sleep(0.5)
                    if transfer_status["state"] != "running":
                        update_transfer("running", uid)
                    migrate(institution, transfer_directory, source, dry, progress=track)
                    filename = "inventory.json" if dry else "manifest.json"
                    data = json.loads(
                        (transfer_directory / filename).read_text(encoding="utf8")
                    )
                    if not dry:
                        from catalog import merge_collection

                        works.update(merge_collection(transfer_directory, root))
                        with tx() as con:
                            library_showcases.import_showcases(con, data.get("showcases", []))
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
                                            bool(prior and prior[0] and permitted and not library_privacy.manages_sharing(con, item["id"])),
                                            prior[1] if prior else 0,
                                            prior[2] if prior else "work",
                                        )
                    update_transfer("inventoried" if dry else "transferred", uid)
                except SourceFailure as exc:
                    detail = {
                        401: "Vimeo rejected the token. Enter an active owner token and retry.",
                        403: "Vimeo denied access. Check the token's scopes and account permissions.",
                        404: "Vimeo could not find the requested source. Check the account and preview again.",
                        429: "Vimeo asked CuePorch to slow down. Wait before resuming the transfer.",
                    }.get(exc.status, "Check the source and retry from your saved progress.")
                    update_transfer("failed", uid, f"Source HTTP {exc.status}. {detail}")
                except (ValueError, OSError, KeyError) as exc:
                    update_transfer("failed", uid, type(exc).__name__ + ". Retry from the source inventory.")
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
            new_work = work["id"] not in works
            works.update(merge_collection(staged, root))
            with tx() as con:
                if new_work:
                    create_upload_review(con, works[work["id"]])
                job = library_worker.enqueue(
                    con, works[work["id"]], public == "true", kind=kind
                )
        return {"work": work["id"], "job": job, "status_url": "/api/delivery"}, 202

    @app.get("/submit")
    def submission_form():
        return render_template("submission_form.html", brand=workspace_brand())

    @app.post("/api/submissions")
    def submit():
        from catalog import build_intake, merge_collection
        from media_tools import prepare_screening

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
                try:
                    prepare_screening(
                        folder / master_entry["path"], folder / "screening.mp4"
                    )
                except ValueError:
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
            for work in submitted:
                create_upload_review(con, work)
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
                if step in ("caption", "translation"):
                    con.execute("""UPDATE action SET status='waiting',decision=NULL
                        WHERE step=? AND decision='automatic' AND id IN (
                            SELECT id FROM caption_job WHERE state='queued')""", (step,))
                if step in library_ai_drafts.FEATURES:
                    con.execute("""UPDATE action SET status='waiting',decision=NULL
                        WHERE step=? AND decision='automatic' AND id IN (
                            SELECT id FROM library_ai_draft WHERE state='queued')""", (step,))
                if step == studio_scripts.STEP:
                    con.execute("""UPDATE action SET status='waiting',decision=NULL
                        WHERE step=? AND decision='automatic' AND id IN (
                            SELECT id FROM studio_script_job WHERE state='queued')""", (step,))
            if not required:
                con.execute(
                    "UPDATE action SET status='eligible',decision='automatic' WHERE step=? AND status='waiting'",
                    (step,),
                )
                if step == audience_comments.STEP:
                    community_analytics.publish_approved(con)
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
                {key: row[key] for key in row.keys() if row[key] is not None}
                for row in con.execute(
                    """SELECT action.id,step,payload,c.body AS comment_body,a.name AS comment_author
                       FROM action LEFT JOIN audience_comment c ON c.approval=action.id
                       LEFT JOIN audience_account a ON a.id=c.account
                       WHERE action.status='waiting' ORDER BY action.created"""
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
        actor = staff()
        decision = request.json.get("decision")
        if decision not in ("approve", "reject"):
            abort(400)
        with tx() as con:
            changed = con.execute(
                "UPDATE action SET status=?,decision=?,actor=? WHERE id=? AND status='waiting'",
                (
                    "eligible" if decision == "approve" else "rejected",
                    decision,
                    actor,
                    uid,
                ),
            ).rowcount
            if not changed:
                abort(409)
            if decision == "approve" and uid.startswith(audience_comments.APPROVAL_PREFIX):
                community_analytics.publish_approved(con, uid)
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
            titles = dict(con.execute("SELECT work,title FROM library_title"))
        return [
            dict(work=w["id"], version=f["sha256"], title=titles.get(w["id"], w["title"]))
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
            if request.path == "/screen/" + token:
                title = library_actions.title_for(con, works[grant["work"]])
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
        tracks = library_languages.tracks_for(works[grant["work"]], selected)
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
            response = media_bandwidth.measure(response, tx, grant["work"])
        else:
            referring = urlsplit(request.referrer or "")
            if f"{referring.scheme}://{referring.netloc}" != grant["origin"]:
                abort(403)
            page = render_template(
                "screening.html",
                title=title,
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
            enforce_sharing(con, feature="outbound", settings=library_privacy.DEFAULTS | {"visibility": "public"})
            if library_peer_handoff.get(con, slug):
                abort(409, "This library video uses CuePorch-managed viewer links.")
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

    @app.route("/watch/<kind>/<slug>", methods=["GET", "POST"])
    @app.route("/embed/<kind>/<slug>", methods=["GET", "POST"], endpoint="embed_watch")
    def watch(kind, slug):
        embed = request.path.startswith("/embed/")
        if kind == "work":
            frame = request.headers.get("Sec-Fetch-Dest") in ("iframe", "frame")
            local = local_watch(slug, embed, frame)
            if local is not None:
                return local
        with tx() as con:
            if library_peer_handoff.get(con, slug):
                abort(404)
            row = con.execute(
                """SELECT * FROM route WHERE kind=? AND slug=?
                   AND NOT EXISTS (SELECT 1 FROM library_deleted_item d WHERE d.kind='work' AND d.item=route.slug)""",
                (kind, slug),
            ).fetchone()
            creator = creator_profiles.for_work(con, slug) if row and kind == "work" and not embed else None
            if row and kind in ("work", "meeting") and not embed and request.method in ("GET", "HEAD"):
                retained = works.get(slug) if kind == "work" else None
                notice = content_rating.screening_notice(
                    row["description"] or row["slug"], viewing_preferences(con),
                    library_details.details_for(con, retained) if retained else {},
                )
                if notice is not None:
                    return notice
        if not row:
            abort(404)
        if request.method == "POST":
            abort(405)
        if embed:
            return redirect(library_embed.peertube_address(row["destination"], request.args), code=302)
        return library_discovery.no_store(render_template(
            "watch.html",
            title=row["description"] or row["slug"],
            url=row["destination"],
            slug=slug, save_kind=kind if kind in ("work", "meeting") else None, save_slug=slug,
            creator=creator,
        ))

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
            operator_access = operator_name(con)
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

    def public_listing(con, item=None):
        return library_discovery.listing(con, read_library_works, library_actions.title_for, vimeo_privacy.permits, item)

    viewing_preferences = audience_accounts.register(
        app, tx, os.environ.get("CUEPORCH_PUBLIC_URL", ""), operator, public_listing, action, staff,
    )
    library_discovery.register(app, tx, staff, public_listing, root, institution, viewing_preferences)
    authorize_editor = team_accounts.register(app, tx, team_origin, operator, workspace_brand, operator_name,
                                               read_library_works, library_actions.title_for, root,
                                               public_listing, library_discovery.entry_payload, sharing_access)
    library_folders.register(app, tx, staff, action, read_library_works, enforce_sharing)
    library_folder_privacy.register(app, tx, staff, action, read_library_works, sharing_access, enforce_sharing)
    library_trash.register(app, tx, staff, read_library_works)
    def remember_work(work):
        works[work["id"]] = work

    start_editor, start_file_links, start_ai_drafts = library_actions.register(
        app, tx, staff, read_library_works, root, remember_work, vimeo_privacy.permits, action, operator,
        authorize_editor, operator_name, enforce_sharing, team_origin, ai_actor,
    )
    analytics_reports.register(app, tx, staff, read_library_works)
    library_events.register(app, tx, staff, action, database, os.environ.get("CUEPORCH_PUBLIC_URL", ""),
                            operator, root, read_library_works, institution, remember_work, sharing_access, enforce_sharing,
                            ai_actor)
    event_api.register(app, tx, team_origin, credential_owner)
    app.extensions["stop_event_webhooks"] = event_webhooks.register(app, tx, team_origin, credential_owner, database)
    library_showcases.register(
        app, tx, staff, read_library_works, library_actions.title_for, root, operator,
        vimeo_privacy.permits, os.environ.get("CUEPORCH_PUBLIC_URL", ""),
    )
    library_upload.register(app, tx, staff, team_contributor, action, root, database, institution, remember_work,
                            enforce_sharing)
    vimeo_privacy.register(app, tx, staff, action, read_library_works, enforce_sharing)
    local_watch = library_privacy.register(
        app, tx, staff, action, read_library_works, root, library_actions.title_for, operator,
        vimeo_privacy.permits, library_details.details_for, creator_profiles.for_work,
        operator_name, authorize_editor, sharing_access, enforce_sharing, viewing_preferences, team_origin,
        lambda con: team_ai_policy.load(con)[1]["viewer_questions"],
    )

    @app.get("/api/library")
    def library():
        actor = staff()
        data = read_library_works()
        with tx() as con:
            jobs = con.execute(
                "SELECT j.work,j.status,j.remote,j.error,r.kind AS route_kind FROM publication_job j LEFT JOIN route r ON r.slug=j.work AND r.kind=j.kind ORDER BY j.rowid"
            ).fetchall()
            placement = dict(con.execute("SELECT work,folder FROM library_placement"))
            visibility = {
                row["work"]: json.loads(row["settings"])["visibility"]
                for row in con.execute("SELECT work,settings FROM library_privacy")
            }
            protected = {row[0] for row in con.execute("SELECT slug FROM route WHERE public_record=1")}
            local_delivery = {row[0] for row in con.execute(
                "SELECT work FROM library_privacy UNION SELECT work FROM library_privacy_change "
                "UNION SELECT work FROM library_peer_handoff"
            )}
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
            titles = {
                row["work"]: row for row in con.execute("SELECT * FROM library_title")
            }
            queue = {
                row[0]
                for row in con.execute(
                    "SELECT work FROM library_watch_later WHERE actor=?", (actor,)
                )
            }
        latest = {r["work"]: dict(r) for r in jobs}
        return [
            dict(
                id=w["id"],
                asset_type=w.get("asset_type", "video"),
                title=titles[w["id"]]["title"] if w["id"] in titles else w["title"],
                folder=placement.get(w["id"]),
                watch_later=w["id"] in queue,
                title_sync_error=bool(titles[w["id"]]["error"])
                if w["id"] in titles
                else False,
                source_privacy=w.get("source_privacy"),
                vimeo_source=w["provider"] == "vimeo",
                editable_privacy=w["id"] not in protected and library_privacy.editable(w),
                visibility=visibility.get(w["id"]) if w["id"] not in protected else None,
                duration_seconds=w.get("duration_seconds"),
                creator=w.get("source_creator"),
                added_at=w.get("library_added_at"),
                modified_at=max(
                    filter(
                        None,
                        (
                            datetime.fromtimestamp(
                                titles[w["id"]]["changed"], timezone.utc
                            ).isoformat()
                            if w["id"] in titles
                            else None,
                            w.get("library_version_changed"),
                            w.get("source_modified_time"),
                        ),
                    ),
                    default=None,
                ),
                version_count=1 + len(w.get("versions_history", [])),
                starred=w["id"] in starred,
                source=w["source_url"],
                quality=w["disposition"],
                exceptions=w.get("transfer_issues", []),
                public_eligible=w.get("source_privacy") in ("public", "anybody")
                and w.get("source_embed_policy") in ("anywhere", "public")
                and w["id"] not in local_delivery,
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
                       (SELECT 1 FROM library_deleted_item d WHERE d.kind='work' AND d.item=route.slug)
                       AND NOT EXISTS (SELECT 1 FROM library_peer_handoff h WHERE h.work=route.slug)"""
                )
            }
            local_routes = {
                row["work"]: row for row in con.execute("SELECT * FROM library_privacy")
                if not library_privacy.blocked(con, row["work"])
                and not library_privacy.protected_route(con, row["work"])
            }
            embed_pages = {row["work"]: json.loads(row["pages"])
                           for row in con.execute("SELECT work,pages FROM library_embed_pages")}
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
            local = local_routes.get(w["id"])
            if local and vimeo_privacy.permits(w, json.loads(local["settings"])):
                paths = library_privacy.share_paths(w["id"], local)
            else:
                paths = dict(watch="/watch/" + prefix if kind else None,
                             embed_url="/embed/" + prefix if kind else None)
            for site in embed_pages.get(w["id"], w.get("known_embed_pages", [])) or [""]:
                csvout.writerow(
                    [
                        w["source_id"],
                        w["source_url"],
                        w["id"],
                        base + paths["watch"] if paths["watch"] else "",
                        base + paths["embed_url"] if paths["embed_url"] else "",
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
        return render_template(
            "library_console.html", public_url=os.environ.get("CUEPORCH_PUBLIC_URL", "")
        )

    @app.get("/operator/videos/<uuid:work>")
    def operator_video(work):
        return render_template("video_details.html", work=str(work), public_url=os.environ.get("CUEPORCH_PUBLIC_URL", ""))

    @app.get("/operator/videos/<uuid:work>/appearance")
    def operator_appearance(work):
        return render_template("video_appearance.html", work=str(work))

    @app.get("/operator/videos/<uuid:work>/languages")
    def operator_languages(work):
        return render_template("video_languages.html", work=str(work))

    @app.get("/operator/videos/<uuid:work>/interactivity")
    def operator_interactivity(work):
        return render_template("video_interactivity.html", work=str(work))

    @app.get("/operator/videos/<uuid:work>/analytics")
    def operator_analytics(work):
        return render_template("video_analytics.html", work=str(work))

    @app.get("/operator/analytics")
    def analytics_dashboard():
        return render_template("analytics_dashboard.html")

    @app.get("/operator/record")
    def record_studio():
        embedded = request.args.get("timeline") == "1"
        response = Response(render_template("record_studio.html", embedded=embedded, **studio_scripts.CONTROLS),
                            mimetype="text/html")
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'" if embedded else "frame-ancestors 'none'"
        return response

    @app.get("/operator/videos/<uuid:work>/editing")
    def operator_editing(work):
        return render_template("video_editing.html", work=str(work))

    @app.get("/operator/videos/<uuid:work>/timeline")
    def operator_timeline(work):
        return render_template("timeline_studio.html", work=str(work))

    @app.get("/player.js")
    def player_sdk():
        response = app.send_static_file("ui/embed_player.js")
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

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
        try:
            with tx() as con:
                enforce_sharing(con, feature="outbound")
                if not os.environ.get("CUEPORCH_PEERTUBE_ORIGIN"):
                    abort(503, "Configure the PeerTube destination")
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
                if public:
                    enforce_sharing(con, settings=library_privacy.DEFAULTS | {"visibility": "public"})
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
            enforce_sharing(con, feature="outbound", settings=library_privacy.DEFAULTS | {"visibility": "public"})
            for item in manifest["works"]:
                if (
                    item["provider"] != "vimeo"
                    or library_trash.hidden(con, "work", item["id"])
                    or item.get("source_privacy") not in ("public", "anybody")
                    or item.get("source_embed_policy") not in ("public", "anywhere")
                    or item.get("transfer_issues")
                    or library_privacy.manages_sharing(con, item["id"])
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
    app.extensions["stop_caption_jobs"] = library_caption_jobs.start(app, root, tx, remember_work)
    app.extensions["stop_ai_drafts"] = start_ai_drafts()
    app.extensions["stop_editor"] = start_editor()
    app.extensions["stop_file_links"] = start_file_links()
    review_push.start(app, tx, database)
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
