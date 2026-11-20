"""Queue and resume an explicitly selected Library cut at an outbound destination."""

import json
import re
from threading import Event, Thread
from time import time
from uuid import UUID

from cryptography.fernet import InvalidToken
from filelock import FileLock, Timeout
from flask import abort, render_template, request
from werkzeug.exceptions import HTTPException

from browser_account_security import json_mutation, private, same_origin, secure_site
from catalog import video_version_key
import facebook_upload
import library_privacy
import linkedin_upload
import team_accounts
from team_group_roles import EFFECTIVE_ROLE
import vimeo_privacy
import youtube_upload


SCHEMA = """
CREATE TABLE IF NOT EXISTS social_publication(
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, connection TEXT NOT NULL, actor TEXT NOT NULL,
 work TEXT NOT NULL, cut TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, media_type TEXT NOT NULL,
 metadata TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued', due INTEGER NOT NULL,
 session BLOB, offset INTEGER NOT NULL DEFAULT 0, probe INTEGER NOT NULL DEFAULT 0,
 final_sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
 remote TEXT, remote_privacy TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS social_publication_due ON social_publication(state,due);
CREATE INDEX IF NOT EXISTS social_publication_work ON social_publication(work,provider);
"""
MARKER = "X-CuePorch-Social"
RETRY_SECONDS = (5, 20, 60, 240)
MIME = {"mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "mkv": "video/x-matroska"}


def metadata_for(body, provider, pages):
    title, description = body.get("title"), body.get("description", "")
    visibility, made_for_kids = body.get("visibility"), body.get("made_for_kids")
    if provider in ("linkedin", "facebook"):
        page = body.get("company")
        if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 100 or any(c in title for c in "\n\r\0")
                or not isinstance(description, str) or len(description) > 3000 or "\0" in description
                or visibility != "public" or not isinstance(page, str) or page not in pages):
            abort(400, "Check the title, post text, connected Page and public visibility.")
        return dict(title=title.strip(), description=description, company=page, visibility=visibility)
    category = body.get("category", "22")
    if (not isinstance(title, str) or not 1 <= len(title.strip()) <= 100 or any(c in title for c in "<>\n\r\0")
            or not isinstance(description, str) or len(description.encode("utf8", errors="replace")) > 5000
            or any(c in description for c in "<>\0") or visibility not in youtube_upload.PRIVACY
            or type(made_for_kids) is not bool or not isinstance(category, str) or not re.fullmatch(r"[0-9]{1,4}", category)):
        abort(400, "Check the YouTube title, description, category, visibility and audience settings.")
    return dict(snippet=dict(title=title.strip(), description=description, categoryId=category),
                status=dict(privacyStatus=visibility, selfDeclaredMadeForKids=made_for_kids))


def visibility_for(metadata):
    return metadata["visibility"] if "visibility" in metadata else metadata["status"]["privacyStatus"]


def transferable(con, work, visibility, provider):
    if work is None or work.get("asset_type", "video") != "video" or library_privacy.blocked(con, work["id"]):
        abort(404, "The selected video is unavailable.")
    if work["provider"] == "vimeo" and visibility != "private":
        source, _ = vimeo_privacy.target_settings(work)
        if source["visibility"] != "public" and source["visibility"] != visibility:
            abort(403, "The Vimeo source privacy restricts this video to a narrower destination audience.")
    formats = ("mp4",) if provider == "linkedin" else MIME
    limits = {"linkedin": (linkedin_upload.MIN_BYTES, linkedin_upload.MAX_BYTES),
              "facebook": (1, facebook_upload.MAX_BYTES)}
    minimum, maximum = limits.get(provider, (1, 256 * 1000**3))
    for role in ("original", "playback"):
        for file in work["files"]:
            if (file["role"] == role and file["format"] in formats and minimum <= file["bytes"] <= maximum
                    and re.fullmatch(r"[0-9a-f]{64}", file["sha256"])):
                return file
    if provider == "linkedin":
        abort(409, "LinkedIn needs a retained MP4 between 75 KB and 500 MB.")
    if provider == "facebook":
        abort(409, "Facebook needs a retained video under 4 GB.")
    abort(409, "This version needs a retained video file before it can be published.")


def disconnect(con, provider):
    con.execute("""UPDATE action SET status='canceled',decision='disconnect' WHERE status IN ('waiting','eligible')
                   AND id IN (SELECT id FROM social_publication WHERE provider=? AND state='queued')""", (provider,))
    con.execute("""UPDATE social_publication SET state=CASE WHEN final_sent THEN 'unconfirmed' ELSE 'canceled' END,
                   session=NULL,error=?
                   WHERE provider=? AND state='queued'""",
                ("The destination account disconnected. Check its channel for any transfer already accepted.", provider))


class AwaitingApproval(Exception):
    pass


class Publisher:
    def __init__(self, app, tx, database, root, connection, find_work, sharing_access, can_edit):
        self.app, self.tx, self.root = app, tx, root
        self.connection, self.find_work, self.sharing_access = connection, find_work, sharing_access
        self.can_edit = can_edit
        self.provider = connection.config.key
        self.stop = Event()
        self.lock = FileLock(str(database.with_suffix(f".{self.provider}-publish.lock")), timeout=0)
        self.worker = None
        if connection.configured:
            self.worker = Thread(target=self.run, name=f"CuePorch {connection.config.name} publication", daemon=True)
            self.worker.start()

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(10)

    def authorize(self, con, job):
        self.connection.authorize(con, job["connection"])
        if not con.execute("SELECT 1 FROM social_publication WHERE id=? AND state='queued'", (job["id"],)).fetchone():
            abort(409, "The upload was canceled.")
        if not con.execute("SELECT 1 FROM action WHERE id=? AND status='eligible'", (job["id"],)).fetchone():
            raise AwaitingApproval
        if self.stop.is_set():
            abort(503, "CuePorch is stopping the upload worker.")
        metadata = json.loads(job["metadata"])
        visibility = visibility_for(metadata)
        actor = job["actor"]
        if actor.startswith("team:"):
            row = con.execute(f"""SELECT m.id,{EFFECTIVE_ROLE} AS role FROM team_member m
                                  WHERE m.id=? AND m.removed IS NULL""", (actor[5:],)).fetchone()
            if not self.can_edit(con, row, job["work"]):
                abort(403, "The team member who queued this upload no longer has access.")
            rules = self.sharing_access(con, row)
            if not rules["outbound"] or visibility not in rules["modes"]:
                abort(403, "The current workspace sharing policy prevents this upload.")
        work = self.find_work(job["work"])
        file = transferable(con, work, visibility, self.provider)
        if job["cut"] != video_version_key(work) or job["sha256"] != file["sha256"]:
            abort(409, "The video changed while this upload was queued. Select its current cut to publish.")
        return metadata

    def media_part(self, job, offset, count):
        source = self.root / "objects" / job["sha256"]
        try:
            with source.open("rb") as media:
                media.seek(offset)
                body = media.read(count)
        except OSError:
            abort(409, "The retained video file is unavailable.")
        if len(body) != count:
            abort(409, "The retained video file no longer matches its recorded size.")
        return body

    def save_session(self, job, session, offset, delay=0):
        with self.tx() as con:
            con.execute("""UPDATE social_publication SET session=?,offset=?,attempts=0,error=NULL,due=?
                           WHERE id=? AND state='queued'""",
                        (self.connection.cipher.encrypt(json.dumps(session).encode()), offset,
                         int(time()) + delay, job["id"]))

    def saved_session(self, job):
        try:
            return json.loads(self.connection.cipher.decrypt(job["session"]))
        except (InvalidToken, ValueError):
            abort(409, "The video upload session could not be reopened.")

    def unconfirmed(self, job, destination):
        with self.tx() as con:
            con.execute("""UPDATE social_publication SET state='unconfirmed',session=NULL,error=?
                           WHERE id=? AND state='queued'""",
                        (f"The provider may have published the video. Check {destination} before sending another.",
                         job["id"]))

    def finalize_public_post(self, job, send, destination):
        with self.tx() as con:
            self.authorize(con, job)
            con.execute("UPDATE social_publication SET final_sent=1 WHERE id=?", (job["id"],))
        try:
            post_id = send()
        except HTTPException as error:
            if error.code >= 500:
                self.unconfirmed(job, destination)
                return
            with self.tx() as con:
                con.execute("UPDATE social_publication SET final_sent=0 WHERE id=? AND state='queued'", (job["id"],))
                if error.code == 429:
                    con.execute("""UPDATE action SET status='waiting',decision=NULL WHERE id=? AND decision='automatic'
                                   AND EXISTS(SELECT 1 FROM policy WHERE step='publication' AND required=1)""",
                                (job["id"],))
            raise
        with self.tx() as con:
            con.execute("""UPDATE social_publication SET state='delivered',session=NULL,remote=?,
                           remote_privacy='public',error=NULL WHERE id=? AND state IN ('queued','unconfirmed')""",
                        (post_id, job["id"]))

    def tick(self):
        with self.tx() as con:
            job = con.execute("""SELECT p.* FROM social_publication p JOIN action a ON a.id=p.id
                                 WHERE p.provider=? AND p.state='queued' AND a.status='eligible' AND p.due<=?
                                 ORDER BY p.due,p.rowid LIMIT 1""", (self.provider, int(time()))).fetchone()
        if not job:
            return False
        try:
            self.post_next(job)
        except AwaitingApproval:
            return False
        except HTTPException as error:
            if self.stop.is_set():
                return False
            attempts = job["attempts"] + 1
            retry = (error.code == 429 or error.code >= 500) and attempts <= len(RETRY_SECONDS)
            wait = max(RETRY_SECONDS[attempts - 1], int(getattr(error, "retry_after", 0) or 0)) if retry else 0
            with self.tx() as con:
                con.execute("""UPDATE social_publication SET state=?,due=?,attempts=?,error=?,
                               session=CASE WHEN ? THEN session ELSE NULL END WHERE id=? AND state='queued'""",
                            ("queued" if retry else "failed", int(time()) + wait, attempts,
                             error.description, retry, job["id"]))
        return True

    def run(self):
        while not self.stop.is_set():
            try:
                with self.lock, self.app.app_context():
                    if self.tick():
                        continue
            except Timeout:
                pass
            except Exception:
                self.app.logger.exception("Social publication failed to process a queued upload.")
            self.stop.wait(1)


class YoutubePublisher(Publisher):

    def post_next(self, job):
        with self.tx() as con:
            metadata = self.authorize(con, job)
        args = (self.connection, job["connection"])
        media_type, size = job["media_type"], job["bytes"]
        if not job["session"]:
            address = youtube_upload.start(*args, metadata, media_type, size)
            with self.tx() as con:
                self.authorize(con, job)
                con.execute("UPDATE social_publication SET session=?,attempts=0,error=NULL WHERE id=?",
                            (self.connection.cipher.encrypt(address.encode("ascii")), job["id"]))
            return
        try:
            address = self.connection.cipher.decrypt(job["session"]).decode("ascii")
        except (InvalidToken, UnicodeError):
            abort(409, "The saved upload session cannot be opened. Reconnect the YouTube account.")
        body = b""
        offset = job["offset"]
        if not job["probe"] and offset < size:
            body = self.media_part(job, offset, min(youtube_upload.CHUNK, size - offset))
            with self.tx() as con:
                self.authorize(con, job)
                con.execute("UPDATE social_publication SET probe=1,final_sent=max(final_sent,?) WHERE id=?",
                            (offset + len(body) == size, job["id"]))
        result = youtube_upload.transfer(*args, address, media_type, size, offset, body)
        with self.tx() as con:
            if "id" in result:
                warning = None if result["privacy"] == metadata["status"]["privacyStatus"] else (
                    "YouTube did not confirm the requested privacy. Check the video in its channel."
                )
                con.execute("""UPDATE social_publication SET state='delivered',session=NULL,offset=bytes,
                               remote=?,remote_privacy=?,error=? WHERE id=? AND state IN ('queued','unconfirmed')""",
                            (result["id"], result["privacy"], warning, job["id"]))
            elif result.get("expired"):
                if job["final_sent"] or (body and offset + len(body) == size):
                    abort(409, "The upload session expired after a final request. Check the channel before publishing again.")
                con.execute("""UPDATE social_publication SET session=NULL,offset=0,probe=0,attempts=0,error=NULL
                               WHERE id=? AND state='queued'""", (job["id"],))
            else:
                acknowledged = result["offset"]
                if acknowledged == size:
                    abort(502, "YouTube received all bytes and has yet to confirm the video ID.")
                if body and acknowledged < offset + len(body):
                    con.execute("UPDATE social_publication SET offset=?,probe=1,due=? WHERE id=? AND state='queued'",
                                (acknowledged, int(time()) + max(1, result["delay"]), job["id"]))
                else:
                    con.execute("""UPDATE social_publication SET offset=?,probe=0,attempts=0,error=NULL,due=?
                                   WHERE id=? AND state='queued'""",
                                (acknowledged, int(time()) + result["delay"], job["id"]))

class LinkedinPublisher(Publisher):
    def post_next(self, job):
        if job["final_sent"]:
            self.unconfirmed(job, "the LinkedIn Company Page")
            return
        with self.tx() as con:
            metadata = self.authorize(con, job)
        company = metadata["company"]
        if company != self.connection.company:
            abort(409, "The LinkedIn Company Page changed. Open a new publication request.")
        args = (self.connection, job["connection"])
        if not job["session"]:
            session = linkedin_upload.start(*args, company, job["bytes"])
            session["started"] = int(time())
            self.save_session(job, session, 0)
            return
        session = self.saved_session(job)
        index = len(session["tags"])
        if index < len(session["parts"]):
            if time() * 1000 >= session["expires"]:
                abort(409, "LinkedIn's video upload addresses expired. Open a new publication request.")
            first, last, address = session["parts"][index]
            data = self.media_part(job, first, last - first + 1)
            with self.tx() as con:
                self.authorize(con, job)
                con.execute("UPDATE social_publication SET probe=1 WHERE id=?", (job["id"],))
            session["tags"].append(linkedin_upload.send_part(session, address, data))
            self.save_session(job, session, last + 1)
            return
        state = linkedin_upload.status(*args, session, company)
        if state == "AVAILABLE":
            self.finalize_public_post(job, lambda: linkedin_upload.post(*args, session["video"], metadata),
                                      "the LinkedIn Company Page")
            return
        if time() - session["started"] > 2 * 3600:
            abort(409, "LinkedIn is still processing the video. Check the Company Page before publishing again.")
        if state == "WAITING_UPLOAD" and not session["finalized"]:
            with self.tx() as con:
                self.authorize(con, job)
            linkedin_upload.finalize(*args, session)
            session["finalized"] = True
        self.save_session(job, session, job["bytes"], linkedin_upload.POLL_SECONDS)


class FacebookPublisher(Publisher):
    def post_next(self, job):
        if job["final_sent"]:
            self.unconfirmed(job, "the Facebook Page")
            return
        with self.tx() as con:
            metadata = self.authorize(con, job)
        args = self.connection, job["connection"], metadata["company"]
        size, offset = job["bytes"], job["offset"]
        if not job["session"]:
            self.save_session(job, facebook_upload.start(*args, size), 0)
            return
        session = self.saved_session(job)
        if offset == size:
            self.finalize_public_post(job, lambda: facebook_upload.post(*args, session, metadata), "the Facebook Page")
            return
        body = self.media_part(job, offset, session["end"] - offset)
        with self.tx() as con:
            self.authorize(con, job)
            con.execute("UPDATE social_publication SET probe=1 WHERE id=?", (job["id"],))
        acknowledged, end = facebook_upload.send_part(*args, session, size, offset, body)
        session["end"] = end
        self.save_session(job, session, acknowledged)


def register(app, tx, database, root, origin, connections, read_works, title_for, operator_name,
             team_member, authorize_editor, can_edit, action, sharing_access, enforce_sharing):
    def find_work(uid):
        return next((work for work in read_works() if work["id"] == uid), None)

    workers = [cls(app, tx, database, root, connections[provider], find_work, sharing_access, can_edit)
               for provider, cls in (("youtube", YoutubePublisher), ("linkedin", LinkedinPublisher),
                                     ("facebook", FacebookPublisher))]

    def close():
        for worker in workers:
            worker.close()

    app.extensions["stop_social_publication"] = close

    def destination(provider):
        connection = connections.get(provider)
        if connection is None:
            abort(404)
        return connection

    def pages_for(con, connection):
        if connection.config.key == "facebook":
            return {page: row["name"] for page, row in connection.pages(con).items()}
        if connection.config.key == "linkedin" and connection.company:
            return {connection.company: connection.company}
        return {}

    def identity(con, uid):
        actor = operator_name(con)
        if actor:
            return actor, None
        authorize_editor(con, uid)
        actor = team_accounts.actor_id(team_member(con))
        return actor, actor

    @app.get("/operator/videos/<uuid:work>/social")
    def social_page(work):
        secure_site(origin)
        provider = request.args.get("destination", "youtube")
        connection = destination(provider)
        return private(render_template("social_publication.html", work=str(work), provider=provider,
                                       provider_name=connection.config.name, team=request.args.get("team") == "1"))

    def receipt(con, uid, work, connection, own_jobs):
        provider = connection.config.key
        entries = con.execute("""SELECT p.id,p.state,p.offset,p.bytes,p.metadata,p.remote,p.remote_privacy,p.error,p.final_sent,
                                  a.status AS approval FROM social_publication p JOIN action a ON a.id=p.id
                                  WHERE p.work=? AND p.provider=? AND (? IS NULL OR p.actor=?)
                                  ORDER BY p.rowid DESC LIMIT 20""", (uid, provider, own_jobs, own_jobs)).fetchall()
        jobs = []
        for job in entries:
            metadata = json.loads(job["metadata"])
            state = job["state"]
            if state == "queued" and job["approval"] in ("waiting", "rejected"):
                state = "waiting for publication approval" if job["approval"] == "waiting" else "rejected"
            remote = job["remote"]
            link = None
            if remote:
                if provider == "linkedin":
                    link = linkedin_upload.link(remote)
                elif provider == "facebook":
                    link = facebook_upload.link(metadata["company"], remote)
                else:
                    link = f"https://www.youtube.com/watch?v={remote}"
            jobs.append(dict(id=job["id"], state=state, sent=job["offset"], bytes=job["bytes"], company=metadata.get("company"),
                             cancelable=job["state"] == "queued" and not job["final_sent"],
                             requested_privacy=visibility_for(metadata),
                             privacy=job["remote_privacy"], error=job["error"], link=link))
        policy = sharing_access(con)
        modes = [mode for mode in youtube_upload.PRIVACY if mode in policy["modes"]]
        allowed = policy["outbound"] and bool(modes if provider == "youtube" else "public" in modes)
        return dict(cut=video_version_key(work), title=title_for(con, work)[:100],
                    configured=connection.configured, connected=bool(connection.connection(con)), jobs=jobs,
                    allowed=allowed, modes=modes, company=getattr(connection, "company", None) if allowed else None,
                    pages=[dict(id=page, name=name) for page, name in pages_for(con, connection).items()] if allowed else [])

    @app.route("/api/library/work/<uuid:work>/social/<provider>", methods=("GET", "POST"))
    def publication(work, provider):
        secure = secure_site(origin)
        same_origin(secure)
        body = json_mutation(secure, MARKER) if request.method == "POST" else None
        connection = destination(provider)
        uid = str(work)
        with tx() as con:
            actor, own_jobs = identity(con, uid)
            item = find_work(uid)
            if item is None or item.get("asset_type", "video") != "video" or library_privacy.blocked(con, uid):
                abort(404, "The selected video is unavailable.")
            if body is None:
                return private(receipt(con, uid, item, connection, own_jobs))
            metadata = metadata_for(body, provider, pages_for(con, connection))
            visibility = visibility_for(metadata)
            enforce_sharing(con, feature="outbound", settings=library_privacy.DEFAULTS | {"visibility": visibility})
            connection.require_configured()
            grant = connection.connection(con)
            if not grant:
                abort(409, "Connect this provider to continue.")
            revision = grant["revision"]
            file = transferable(con, item, visibility, provider)
            cut = video_version_key(item)
            if cut != body.get("cut"):
                abort(409, "The current cut changed. Reload the video before publishing.")
            try:
                request_id = provider + "-" + UUID(str(body.get("id"))).hex
            except ValueError:
                abort(400, "Open a new request before sending this video.")
            encoded = json.dumps(metadata, sort_keys=True)
            prior = con.execute("SELECT work,actor,cut,metadata FROM social_publication WHERE id=?", (request_id,)).fetchone()
            if prior:
                if tuple(prior) != (uid, actor, cut, encoded):
                    abort(409, "This publication request has already been used.")
            else:
                action(con, request_id, "publication", dict(work=uid, destination=connection.config.name,
                                                            company=metadata.get("company"), visibility=visibility, cut=cut))
                con.execute("""INSERT INTO social_publication(id,provider,connection,actor,work,cut,sha256,bytes,media_type,metadata,due)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (request_id, provider, revision, actor, uid, cut, file["sha256"], file["bytes"],
                             MIME[file["format"]], encoded, int(time())))
            return private(receipt(con, uid, item, connection, own_jobs), 200 if prior else 202)

    @app.post("/api/library/work/<uuid:work>/social/<provider>/<job>/cancel")
    def cancel_upload(work, provider, job):
        secure = secure_site(origin)
        json_mutation(secure, MARKER)
        connection = destination(provider)
        uid = str(work)
        with tx() as con:
            actor, own_jobs = identity(con, uid)
            item = find_work(uid)
            if item is None or library_privacy.blocked(con, uid):
                abort(404, "The selected video is unavailable.")
            canceled = con.execute("""UPDATE social_publication SET state='canceled',session=NULL,error=NULL
                                       WHERE id=? AND work=? AND provider=? AND state='queued' AND final_sent=0
                                       AND (? IS NULL OR actor=?)""", (job, uid, provider, own_jobs, own_jobs)).rowcount
            if not canceled:
                abort(409, "The upload can no longer be canceled. Check its current status.")
            con.execute("""UPDATE action SET status='canceled',decision='cancel',actor=?
                           WHERE id=? AND status IN ('waiting','eligible')""", (actor, job))
            return private(receipt(con, uid, item, connection, own_jobs))
