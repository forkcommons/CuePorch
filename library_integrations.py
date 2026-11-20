"""Workspace integration directory and verified cloud video intake."""

from email.errors import HeaderParseError
from email.headerregistry import Address
import hashlib
import hmac
import json
import os
import re
from urllib.parse import urlsplit
import uuid

import requests
from flask import abort, render_template, request
from werkzeug.exceptions import TooManyRequests

from browser_account_security import json_mutation, private, same_origin, secure_site
from intake_watch import CursorReset, Watch, changed_since
import editor_exports
import event_hubspot_sync
import facebook_upload
import google_meet_recordings
import hubspot_crm
import linkedin_upload
import mail_relay
import social_publication
from team_accounts import CONTRIBUTORS, MANAGERS, current as signed_in_member
import shopify_product_media
import tiktok_business
import webex_recordings
import x_upload
import zoom_recordings
import youtube_upload
from library_analytics import source_origin
from library_upload import VIDEO_INPUT, max_upload_bytes
from workspace_oauth import (
    CALLBACK_ROOT, DIRECTORY, Config, Connection, check_provider, provider_request, response_object,
)

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE = "https://oauth2.googleapis.com/revoke"
FILES = "https://www.googleapis.com/drive/v3/files"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
CALLBACK = CALLBACK_ROOT + "google-drive/callback"
MARKER = "X-CuePorch-Integration"
FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")
FIELDS = "id,name,mimeType,trashed,size,version,md5Checksum,sha256Checksum,capabilities(canDownload)"
DROPBOX_AUTH = "https://www.dropbox.com/oauth2/authorize"
DROPBOX_TOKEN = "https://api.dropboxapi.com/oauth2/token"
DROPBOX_RPC = "https://api.dropboxapi.com/2"
DROPBOX_CONTENT = "https://content.dropboxapi.com/2"
DROPBOX_SCOPE = "files.metadata.read files.content.read"
DROPBOX_ID = re.compile(r"id:[A-Za-z0-9_-]{1,256}")
BOX_AUTH = "https://account.box.com/api/oauth2/authorize"
BOX_ORIGIN = "https://api.box.com"
BOX_SCOPE = "root_readwrite"
BOX_ID = re.compile(r"[0-9]{1,24}")
BOX_FIELDS = "id,type,name,permissions,is_download_available"
FOLDER_ID = re.compile(r"[a-f0-9]{32}")
BLOCK_BYTES = 4 * 1024 * 1024


def listed_name(value):
    return isinstance(value, str) and 0 < len(value) <= 512


def video_name(value):
    return listed_name(value) and value.rpartition(".")[2].lower() in VIDEO_INPUT


def video_file(row):
    return (
        isinstance(row, dict)
        and isinstance(row.get("id"), str) and FILE_ID.fullmatch(row["id"])
        and video_name(row.get("name"))
        and isinstance(row.get("mimeType"), str) and row["mimeType"].startswith("video/")
        and not row.get("trashed")
        and isinstance(row.get("capabilities"), dict) and row["capabilities"].get("canDownload") is True
    )


def box_video(row):
    if row.get("type") != "file" or not video_name(row.get("name")):
        return False
    permissions = row.get("permissions")
    return (row.get("is_download_available") is not False
            and (permissions is None or isinstance(permissions, dict) and permissions.get("can_download") is True))


def box_download_url(location):
    try:
        target = urlsplit(location)
        allowed = (target.scheme == "https" and target.hostname is not None
                   and re.fullmatch(r"dl[0-9]*\.boxcloud\.com", target.hostname)
                   and target.port in (None, 443) and target.username is None and target.password is None
                   and target.path.startswith("/d/") and not target.fragment)
    except ValueError:
        allowed = False
    if not allowed:
        abort(502, "Box returned an unexpected download address.")
    return location


def valid_hash(value, digits):
    return isinstance(value, str) and len(value) == digits and re.fullmatch(r"[0-9a-f]+", value)


def destination_folder(folder):
    if folder is not None and (not isinstance(folder, str) or not FOLDER_ID.fullmatch(folder)):
        abort(400, "Choose a Library destination folder.")
    return folder


class DropboxHash:
    def __init__(self):
        self.total = hashlib.sha256()
        self.pending = bytearray()

    def update(self, chunk):
        self.pending.extend(chunk)
        while len(self.pending) >= BLOCK_BYTES:
            self.total.update(hashlib.sha256(self.pending[:BLOCK_BYTES]).digest())
            del self.pending[:BLOCK_BYTES]

    def hexdigest(self):
        result = self.total.copy()
        if self.pending:
            result.update(hashlib.sha256(self.pending).digest())
        return result.hexdigest()


def import_verified(import_video, provider, actor, identity, name, folder, size, checksum, expected,
                    download, authorize):
    if not 0 < size <= max_upload_bytes():
        abort(413, "The video exceeds the workspace upload limit or is empty.")

    def save(destination):
        with download() as response:
            check_provider(response)
            total = 0
            try:
                with destination.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        total += len(chunk)
                        if total > size:
                            abort(422, "The video changed during transfer. Try again.")
                        if checksum is not None:
                            checksum.update(chunk)
                        stream.write(chunk)
            except requests.RequestException:
                abort(502, "The provider interrupted the transfer. Try again.")
            invalid_hash = expected is not None and not hmac.compare_digest(checksum.hexdigest(), expected)
            if total != size or invalid_hash:
                description = "size and checksum" if expected is not None else "size"
                abort(422, f"The video did not match its listed {description}. Try again.")

    work = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cueporch/{provider}/{identity}/{expected or ''}"))
    return import_video(actor, work, name, folder, save, authorize)


def dropbox_entries(connection, revision, source="", cursor=None, watch=False):
    suffix = "/continue" if cursor else ""
    fields = {"cursor": cursor} if cursor else {"path": source, "recursive": False, "limit": 50,
                                               "include_non_downloadable_files": False}
    with connection.api(revision, "POST", DROPBOX_RPC + "/files/list_folder" + suffix, json=fields) as response:
        if watch and cursor and response.status_code == 409:
            error = response_object(response).get("error")
            if isinstance(error, dict) and error.get(".tag") == "reset":
                raise CursorReset()
        check_provider(response)
        result = response_object(response)
    if not (isinstance(result.get("entries"), list) and isinstance(result.get("cursor"), str)
            and 1 <= len(result["cursor"]) <= 2048 and type(result.get("has_more")) is bool):
        abort(502, "Dropbox returned an invalid folder listing.")
    return result


def dropbox_video(row):
    return (isinstance(row, dict) and row.get(".tag") == "file" and video_name(row.get("name"))
            and isinstance(row.get("id"), str) and DROPBOX_ID.fullmatch(row["id"])
            and row.get("is_downloadable", True) is True)


def dropbox_queue_entry(row, watch):
    if not dropbox_video(row) or not isinstance(row.get("rev"), str):
        return None
    if watch["recovering"] and not changed_since(row, watch["started"]):
        return None
    return {key: row.get(key) for key in (".tag", "id", "name", "rev", "size", "content_hash")}


def import_dropbox(import_video, connection, revision, actor, row, folder, authorize):
    if not dropbox_video(row):
        abort(415, "Choose a supported video that its owner allows you to download.")
    size, version, expected = row.get("size"), row.get("rev"), row.get("content_hash")
    if (type(size) is not int or not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{9,128}", version)
            or not valid_hash(expected, 64)):
        abort(422, "Dropbox did not provide a verifiable video revision.")

    def download():
        return connection.api(revision, "POST", DROPBOX_CONTENT + "/files/download", stream=True,
                              headers={"Dropbox-API-Arg": json.dumps({"path": "rev:" + version})})

    return import_verified(import_video, "dropbox", actor, f"{row['id']}/{version}", row["name"],
                           folder, size, DropboxHash(), expected, download, authorize)


def import_google_video(import_video, connection, revision, actor, item, folder, authorize):
    url = FILES + "/" + item
    with connection.api(revision, "GET", url, params=dict(fields=FIELDS, supportsAllDrives="true")) as response:
        check_provider(response)
        row = response_object(response)
    if row.get("id") != item or not video_file(row):
        abort(415, "Choose a supported video that its owner allows you to download.")
    size, version = str(row.get("size", "")), str(row.get("version", ""))
    sha, md5 = row.get("sha256Checksum"), row.get("md5Checksum")
    if valid_hash(sha, 64):
        checksum, expected = hashlib.sha256(), sha
    elif valid_hash(md5, 32):
        checksum, expected = hashlib.md5(usedforsecurity=False), md5
    else:
        abort(422, "Google Drive did not provide a checksum for this video.")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", size) or not re.fullmatch(r"[0-9]{1,20}", version):
        abort(422, "Google Drive did not provide a verifiable video revision.")

    def download():
        return connection.api(revision, "GET", url, params=dict(alt="media", supportsAllDrives="true"), stream=True)

    return import_verified(import_video, "google-drive", actor, f"{item}/{version}", row["name"],
                           folder, int(size), checksum, expected, download, authorize)


def import_zoom_video(import_video, connection, revision, actor, occurrence, folder, authorize, exact=None):
    title, recording = zoom_recordings.select(connection, revision, occurrence, exact)
    return import_verified(
        import_video, "zoom", actor, f"{occurrence}/{recording['id']}", title, folder, recording["file_size"],
        None, None, lambda: zoom_recordings.download(connection, revision, recording["download_url"]), authorize,
    )


def import_webex_video(import_video, connection, revision, actor, item, folder, authorize):
    title, size, url = webex_recordings.select(connection, revision, item)
    return import_verified(import_video, "webex", actor, item, title, folder, size, None, None,
                           lambda: webex_recordings.download(url), authorize)


def meeting_archive_notifier(tx, origin):
    relay = mail_relay.configured(origin)
    if not relay:
        return None
    try:
        configured = Address(addr_spec=os.environ.get("CUEPORCH_MEETING_ARCHIVE_EMAIL", "").strip())
        operator_email = configured.addr_spec if configured.domain else None
    except (ValueError, HeaderParseError):
        operator_email = None

    def notify(actor, provider, work):
        with tx() as con:
            if con.execute("SELECT 1 FROM library_deleted_item WHERE kind='work' AND item=?", (work,)).fetchone():
                return None
            if actor.startswith("team:"):
                member = con.execute("""SELECT m.email,m.role FROM team_member m
                    JOIN account_verified_email v ON v.scope='team' AND v.account=m.id AND v.address=m.email
                    WHERE m.id=? AND m.removed IS NULL""", (actor.removeprefix("team:"),)).fetchone()
                address = member["email"] if member and member["role"] in MANAGERS else None
                path = f"/team/video/{work}/details"
            else:
                address = operator_email
                path = f"/operator/videos/{work}"
        if not address:
            return None
        return mail_relay.deliver(relay, address, f"Your {provider} recording is ready",
                                  f"CuePorch saved your {provider} recording to the private Library.\n\n"
                                  f"Open the recording in CuePorch:\n{origin}{path}\n")

    return notify


def register(app, tx, staff, operator, origin, import_video, database, sharing_access, hubspot):
    drive = Connection(Config("google-drive", "Google Drive", AUTH, TOKEN_URL, REVOKE, SCOPE,
                              dict(access_type="offline", prompt="consent")), tx, operator)
    dropbox = Connection(Config(
        "dropbox", "Dropbox", DROPBOX_AUTH, DROPBOX_TOKEN, DROPBOX_RPC + "/auth/token/revoke",
        DROPBOX_SCOPE, dict(token_access_type="offline"), revoke_style="access-bearer",
    ), tx, operator)
    box = Connection(Config("box", "Box", BOX_AUTH, BOX_ORIGIN + "/oauth2/token", BOX_ORIGIN + "/oauth2/revoke",
                            BOX_SCOPE, pkce=False), tx, operator)
    meet = Connection(Config("google-meet", "Google Meet", AUTH, TOKEN_URL, REVOKE,
                             google_meet_recordings.SCOPES, dict(access_type="offline", prompt="consent")), tx, operator)
    zoom = Connection(Config(
        "zoom", "Zoom", zoom_recordings.ORIGIN + "/oauth/authorize", zoom_recordings.ORIGIN + "/oauth/token",
        zoom_recordings.ORIGIN + "/oauth/revoke", zoom_recordings.SCOPES,
        revoke_style="access-form", public_client=True, authorize_scope=False,
    ), tx, operator)
    webex = Connection(Config(
        "webex", "Webex", webex_recordings.API + "/authorize", webex_recordings.API + "/access_token",
        "", webex_recordings.SCOPE,
    ), tx, operator)
    youtube = Connection(youtube_upload.OAUTH, tx, operator)
    linkedin = linkedin_upload.CompanyConnection(tx, operator)
    facebook = facebook_upload.PageConnection(tx, operator)
    x_account = x_upload.AccountConnection(tx, operator)
    tiktok = tiktok_business.AdvertiserConnection(tx, operator)
    shopify = shopify_product_media.StoreConnection(tx, operator)
    publishing = {item.config.key: item for item in (youtube, linkedin, facebook, x_account, tiktok)}
    outbound = publishing | {"shopify": shopify}
    connections = {item.config.key: item for item in (drive, dropbox, box, meet, zoom, webex, hubspot)} | outbound
    watch = Watch(
        app, tx, database, dropbox, lambda state: dropbox_entries(
            dropbox, state["connection"], state["source"], state["cursor"], watch=True,
        ),
        dropbox_queue_entry,
        lambda revision, actor, row, folder, authorize: import_dropbox(
            import_video, dropbox, revision, actor, row, folder, authorize,
        ),
    )
    notify = meeting_archive_notifier(tx, origin)
    meet_watch = Watch(
        app, tx, database, meet, lambda state: google_meet_recordings.scan_new_recordings(meet, state),
        lambda entry, state: entry,
        lambda revision, actor, row, folder, authorize: import_google_video(
            import_video, meet, revision, actor, row["rev"], folder, authorize,
        ), interval=google_meet_recordings.POLL_SECONDS, notify=notify,
    )
    zoom_watch = Watch(
        app, tx, database, zoom, lambda state: zoom_recordings.scan_new_recordings(zoom, state),
        lambda entry, state: entry,
        lambda revision, actor, row, folder, authorize: import_zoom_video(
            import_video, zoom, revision, actor, row["id"], folder, authorize, exact=row["rev"],
        ), interval=zoom_recordings.POLL_SECONDS, notify=notify,
    )
    webex_watch = Watch(
        app, tx, database, webex, lambda state: webex_recordings.scan_new_recordings(webex, state),
        lambda entry, state: entry,
        lambda revision, actor, row, folder, authorize: import_webex_video(
            import_video, webex, revision, actor, row["id"], folder, authorize,
        ), interval=webex_recordings.POLL_SECONDS, notify=notify,
    )
    editor_source = editor_exports.FileSource(os.environ.get("CUEPORCH_EDITOR_EXPORT_FOLDER", ""))
    editor_watch = Watch(
        app,
        tx,
        database,
        editor_source,
        lambda state: editor_exports.changes(editor_source, state),
        lambda entry, state: entry,
        lambda revision, actor, row, folder, authorize: editor_exports.import_export(
            editor_source, import_video, revision, actor, row, folder, authorize
        ),
        interval=editor_exports.POLL_SECONDS,
    )
    meetings = {meet.config.key: meet, zoom.config.key: zoom, webex.config.key: webex}
    watches = {
        dropbox.config.key: watch, meet.config.key: meet_watch, zoom.config.key: zoom_watch,
        webex.config.key: webex_watch,
        "editor-exports": editor_watch,
    }

    def stop_watches():
        for active_watch in watches.values():
            active_watch.stop()

    app.extensions["stop_integration_watches"] = stop_watches

    def site():
        secure = secure_site(origin)
        current = source_origin(("https://" if secure else "http://") + request.host)
        if origin != "Unknown" and current != origin:
            abort(403, "Open Integrations using the configured CuePorch address.")
        return secure, current

    def read_api():
        secure, _ = site()
        same_origin(secure)
        return staff()

    def mutate():
        secure, current = site()
        return staff(), json_mutation(secure, MARKER), secure, current

    def configured_provider(provider):
        if provider not in connections:
            abort(404)
        return connections[provider]

    @app.get(DIRECTORY)
    def integration_directory():
        site()
        return private(render_template("library_integrations.html"))

    @app.get("/team/integrations")
    def personal_integrations():
        secure, _ = site()
        with tx() as con:
            personal_owner(con, secure)
        return private(render_template("library_integrations.html", personal=True))

    def personal_owner(con, secure, expected=None, publishing_grant=False):
        member = signed_in_member(con, secure)
        if not member or (expected is not None and member["id"] != expected):
            abort(403, "Open your own connected apps using your team session.")
        if publishing_grant and (member["role"] not in (*CONTRIBUTORS, *MANAGERS)
                                 or not sharing_access(con, member)["outbound"]):
            abort(403, "Your workspace sharing policy restricts connecting a publishing account.")
        return member

    @app.after_request
    def protect_connection_responses(response):
        if request.path.startswith((CALLBACK_ROOT, "/api/integrations")):
            response.cache_control.no_store = True
            response.cache_control.private = True
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/integrations")
    def integrations():
        if request.args.get("personal") == "1":
            secure, _ = site()
            same_origin(secure)
            with tx() as con:
                member = personal_owner(con, secure)
                allowed = member["role"] in (*CONTRIBUTORS, *MANAGERS) and sharing_access(con, member)["outbound"]
                items = [dict(id=key, name=item.config.name, category="Social publishing",
                              description="Publish a video using your own connected account.",
                              configured=allowed and item.configured, publishing=True,
                              connected=item.for_member(member["id"]).connection(con) is not None)
                         for key, item in publishing.items()]
            return private(dict(integrations=items, publishing_allowed=allowed))
        read_api()
        with tx() as con:
            items = [dict(
                id=item.config.key, name=item.config.name,
                category=("Marketing" if item.config.key == "hubspot" else
                          "Commerce" if item.config.key == "shopify" else
                          "Social publishing" if item.config.key in outbound else
                          "Communications" if item.config.key in meetings else "Cloud storage"),
                description=("Import a contact segment into a webinar occurrence." if item.config.key == "hubspot" else
                             "Add a Library video to a Shopify product's media."
                             if item.config.key == "shopify" else
                             "Upload a native video from the Library to your channel."
                             if item.config.key in outbound else "Copy meeting recordings into your private Library."
                             if item.config.key in meetings else "Browse and copy videos into your private Library."),
                configured=item.configured, connected=item.connection(con) is not None,
                browse_folders=item in (dropbox, box, meet), meeting=item.config.key in meetings,
                publishing=item.config.key in outbound,
            ) for item in connections.values()]
            slack = con.execute("""SELECT 1 FROM folder_slack s WHERE NOT EXISTS(
                SELECT 1 FROM library_deleted_item d WHERE d.kind='folder' AND d.item=s.folder) LIMIT 1""").fetchone()
        items.append(dict(
            id="editor-exports",
            name="Editor exports",
            category="Editing",
            description="Copy new exports from Premiere Pro, Media Encoder or another editor into the private Library.",
            configured=editor_source.configured,
            connected=editor_watch.receipt()["enabled"],
            browse_folders=False,
            meeting=False,
        ))
        items.append(dict(id="slack", name="Slack", category="Collaboration",
                          description="Send upload, review and approval notices from a Library folder.",
                          configured=origin.startswith("https://"), connected=bool(slack),
                          browse_folders=False, meeting=False, manage_path="/operator/library"))
        return private(dict(integrations=items))

    @app.post("/api/integrations/<provider>/connect")
    def connect(provider):
        if request.args.get("personal") == "1":
            secure, current = site()
            json_mutation(secure, MARKER)
            if provider not in publishing:
                abort(404)
            with tx() as con:
                uid = personal_owner(con, secure, publishing_grant=True)["id"]
            return publishing[provider].for_member(uid).prepare(secure, current)
        _, body, secure, current = mutate()
        connection = configured_provider(provider)
        if provider == "shopify":
            return connection.prepare(secure, current, context={"shop": body.get("shop")})
        return connection.prepare(secure, current)

    @app.get(CALLBACK_ROOT + "<provider>/callback")
    def oauth_callback(provider):
        secure, current = site()
        connection = configured_provider(provider)
        if provider in publishing:
            with tx() as con:
                member = signed_in_member(con, secure)
                scoped = connection.for_member(member["id"]) if member else None
                if scoped and not scoped.started(con, request.args.get("state", "")):
                    scoped = None
            if scoped:
                return scoped.complete(secure, current, allow=lambda con: personal_owner(
                    con, secure, expected=scoped.owner, publishing_grant=True,
                ))
        return connection.complete(secure, current)

    @app.post("/api/integrations/<provider>/disconnect")
    def disconnect(provider):
        if request.args.get("personal") == "1":
            secure, _ = site()
            json_mutation(secure, MARKER)
            if provider not in publishing:
                abort(404)
            with tx() as con:
                uid = personal_owner(con, secure)["id"]
            revoked = publishing[provider].for_member(uid).disconnect(
                lambda con: social_publication.disconnect(con, provider, uid),
            )
            return private(dict(connected=False, provider_revoked=revoked))
        mutate()
        connection = configured_provider(provider)
        active_watch = watches.get(provider)
        cleanup = active_watch.clear if active_watch else None
        if provider in outbound:
            cleanup = lambda con: social_publication.disconnect(con, provider)
        elif provider == "hubspot":
            cleanup = event_hubspot_sync.disconnect
        return private(dict(connected=False, provider_revoked=connection.disconnect(cleanup)))

    @app.get("/api/integrations/hubspot/segments")
    def hubspot_segments():
        read_api()
        revision = hubspot.revision()
        result = hubspot_crm.segments(hubspot, revision, request.args.get("after", ""))
        with tx() as con:
            hubspot.authorize(con, revision)
        return private(result)

    @app.get("/api/integrations/google-drive/files")
    def google_drive_files():
        read_api()
        revision = drive.revision()
        search = request.args.get("q", "")[:160].strip().replace("\\", "\\\\").replace("'", "\\'")
        cursor = request.args.get("cursor", "")
        if len(cursor) > 2048:
            abort(400, "The Google Drive page token is invalid.")
        query = "trashed = false and mimeType contains 'video/'"
        if search:
            query += f" and name contains '{search}'"
        params = dict(q=query, pageSize=50, fields=f"nextPageToken,files({FIELDS})",
                      orderBy="modifiedTime desc", supportsAllDrives="true", includeItemsFromAllDrives="true")
        if cursor:
            params["pageToken"] = cursor
        with drive.api(revision, "GET", FILES, params=params) as response:
            check_provider(response)
            rows = response_object(response)
        files, next_page = rows.get("files", []), rows.get("nextPageToken")
        if not isinstance(files, list) or (next_page is not None and
                                          (not isinstance(next_page, str) or len(next_page) > 2048)):
            abort(502, "Google Drive returned an invalid file listing.")
        with tx() as con:
            drive.authorize(con, revision)
        return private(dict(
            files=[dict(id=row["id"], name=row["name"]) for row in files if video_file(row)],
            cursor=next_page,
        ))

    @app.post("/api/integrations/google-drive/import")
    def google_drive_import():
        actor, body, _, _ = mutate()
        item, folder = body.get("file"), destination_folder(body.get("folder"))
        if not isinstance(item, str) or not FILE_ID.fullmatch(item):
            abort(400, "Choose a Google Drive video.")
        revision = drive.revision()
        receipt, status = import_google_video(import_video, drive, revision, actor, item, folder,
                                              lambda con: drive.authorize(con, revision))
        return private(receipt, status)

    @app.get("/api/integrations/google-meet/files")
    def google_meet_files():
        read_api()
        revision = meet.revision()
        source, cursor = request.args.get("folder", ""), request.args.get("cursor", "")
        if len(source) > 256 or len(cursor) > 2048:
            abort(400, "Choose a Google Meet conference and page from the picker.")
        search = request.args.get("q", "")[:160].casefold().strip()
        result = google_meet_recordings.browse(meet, revision, source, cursor, search)
        with tx() as con:
            meet.authorize(con, revision)
        return private(result)

    @app.post("/api/integrations/google-meet/import")
    def google_meet_import():
        actor, body, _, _ = mutate()
        folder = destination_folder(body.get("folder"))
        revision = meet.revision()
        item = google_meet_recordings.recording_file(meet, revision, body.get("file"))
        receipt, status = import_google_video(import_video, meet, revision, actor, item, folder,
                                              lambda con: meet.authorize(con, revision))
        return private(receipt, status)

    @app.post("/api/integrations/<provider>/watch")
    def configure_meeting_archive(provider):
        actor, body, _, _ = mutate()
        if provider not in meetings:
            abort(404)
        active_watch = watches[provider]
        if type(body.get("enabled")) is not bool:
            abort(400, "Choose whether to archive new recordings automatically.")
        if body["enabled"]:
            revision = meetings[provider].revision()
            active_watch.enable(revision, "", destination_folder(body.get("destination")), actor, "", keep_queue=True)
        else:
            active_watch.disable()
        return private(active_watch.receipt())

    @app.get("/api/integrations/zoom/files")
    def zoom_files():
        read_api()
        revision = zoom.revision()
        cursor = request.args.get("cursor", "")
        if len(cursor) > 2048:
            abort(400, "The Zoom page token is invalid.")
        result = zoom_recordings.browse(zoom, revision, cursor, request.args.get("q", "")[:160].casefold().strip())
        with tx() as con:
            zoom.authorize(con, revision)
        return private(result)

    @app.post("/api/integrations/zoom/import")
    def zoom_import():
        actor, body, _, _ = mutate()
        folder = destination_folder(body.get("folder"))
        revision = zoom.revision()
        receipt, status = import_zoom_video(import_video, zoom, revision, actor, body.get("file"), folder,
                                            lambda con: zoom.authorize(con, revision))
        return private(receipt, status)

    @app.get("/api/integrations/webex/files")
    def webex_files():
        read_api()
        revision = webex.revision()
        cursor = request.args.get("cursor", "")
        if len(cursor) > 2048:
            abort(400, "The Webex page token is invalid.")
        result = webex_recordings.browse(webex, revision, cursor, request.args.get("q", "")[:160].casefold().strip())
        with tx() as con:
            webex.authorize(con, revision)
        return private(result)

    @app.post("/api/integrations/webex/import")
    def webex_import():
        actor, body, _, _ = mutate()
        folder = destination_folder(body.get("folder"))
        revision = webex.revision()
        receipt, status = import_webex_video(import_video, webex, revision, actor, body.get("file"), folder,
                                             lambda con: webex.authorize(con, revision))
        return private(receipt, status)

    @app.get("/api/integrations/dropbox/files")
    def dropbox_files():
        read_api()
        revision = dropbox.revision()
        source, cursor = request.args.get("folder", ""), request.args.get("cursor", "")
        if (source and not DROPBOX_ID.fullmatch(source)) or len(cursor) > 2048:
            abort(400, "Choose a Dropbox folder and page from the picker.")
        rows = dropbox_entries(dropbox, revision, source, cursor)
        search = request.args.get("q", "")[:160].casefold().strip()
        files, folders = [], []
        for row in rows["entries"]:
            if dropbox_video(row) and search in row["name"].casefold():
                files.append(dict(id=row["id"], name=row["name"]))
            if (isinstance(row, dict) and row.get(".tag") == "folder" and listed_name(row.get("name"))
                    and isinstance(row.get("id"), str) and DROPBOX_ID.fullmatch(row["id"])):
                folders.append(dict(id=row["id"], name=row["name"]))
        with tx() as con:
            dropbox.authorize(con, revision)
        return private(dict(files=files, folders=folders, cursor=rows["cursor"] if rows["has_more"] else None))

    @app.post("/api/integrations/dropbox/import")
    def dropbox_import():
        actor, body, _, _ = mutate()
        item, folder = body.get("file"), destination_folder(body.get("folder"))
        if not isinstance(item, str) or not DROPBOX_ID.fullmatch(item):
            abort(400, "Choose a Dropbox video.")
        revision = dropbox.revision()
        with dropbox.api(revision, "POST", DROPBOX_RPC + "/files/get_metadata", json={"path": item}) as response:
            check_provider(response)
            row = response_object(response)
        if row.get("id") != item:
            abort(422, "Dropbox returned a different file.")
        receipt, status = import_dropbox(import_video, dropbox, revision, actor, row, folder,
                                         lambda con: dropbox.authorize(con, revision))
        return private(receipt, status)

    @app.get("/api/integrations/<provider>/watch")
    def import_watch_status(provider):
        read_api()
        if provider not in watches:
            abort(404)
        return private(watches[provider].receipt())

    @app.post("/api/integrations/dropbox/watch")
    def configure_dropbox_watch():
        actor, body, _, _ = mutate()
        if type(body.get("enabled")) is not bool:
            abort(400, "Choose whether to watch a Dropbox folder.")
        if not body["enabled"]:
            watch.disable()
            return private(watch.receipt())
        source, destination = body.get("source"), destination_folder(body.get("destination"))
        if not isinstance(source, str) or (source and not DROPBOX_ID.fullmatch(source)):
            abort(400, "Choose a folder in Dropbox.")
        revision = dropbox.revision()
        with dropbox.api(revision, "POST", DROPBOX_RPC + "/files/list_folder/get_latest_cursor",
                         json=dict(path=source, recursive=False, include_non_downloadable_files=False)) as response:
            check_provider(response)
            cursor = response_object(response).get("cursor")
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
            abort(502, "Dropbox returned an invalid folder cursor.")
        watch.enable(revision, source, destination, actor, cursor)
        return private(watch.receipt())

    @app.post("/api/integrations/editor-exports/watch")
    def watch_editor_exports():
        actor, body, _, _ = mutate()
        enabled = body.get("enabled")
        if type(enabled) is not bool:
            abort(400, "Choose whether to watch for editor exports.")

        if enabled:
            destination = destination_folder(body.get("destination"))
            editor_watch.enable(
                editor_source.revision,
                "",
                destination,
                actor,
                editor_exports.baseline(editor_source),
                keep_queue=True,
            )
        else:
            editor_watch.disable()

        return private(editor_watch.receipt())

    @app.post("/api/integrations/<provider>/watch/<int:job>/retry")
    def retry_watch_job(provider, job):
        mutate()
        if provider not in watches:
            abort(404)
        watches[provider].retry(job)
        return private(watches[provider].receipt())

    @app.get("/api/integrations/box/files")
    def box_files():
        read_api()
        revision = box.revision()
        source = request.args.get("folder") or "0"
        cursor = request.args.get("cursor", "")
        if not BOX_ID.fullmatch(source) or len(cursor) > 2048:
            abort(400, "Choose a Box folder and page from the picker.")
        params = dict(usemarker="true", limit=50, fields=BOX_FIELDS)
        if cursor:
            params["marker"] = cursor
        with box.api(revision, "GET", f"{BOX_ORIGIN}/2.0/folders/{source}/items", params=params) as response:
            check_provider(response)
            page = response_object(response)
        rows, marker = page.get("entries"), page.get("next_marker")
        if not isinstance(rows, list) or (marker is not None and
                                         (not isinstance(marker, str) or len(marker) > 2048)):
            abort(502, "Box returned an invalid folder listing.")
        search = request.args.get("q", "")[:160].casefold().strip()
        files, folders = [], []
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not BOX_ID.fullmatch(row["id"]) or not listed_name(row.get("name"))):
                continue
            item = dict(id=row["id"], name=row["name"])
            if row.get("type") == "folder":
                folders.append(item)
            elif box_video(row) and search in row["name"].casefold():
                files.append(item)
        with tx() as con:
            box.authorize(con, revision)
        return private(dict(files=files, folders=folders, cursor=marker))

    @app.post("/api/integrations/box/import")
    def box_import():
        actor, body, _, _ = mutate()
        item, folder = body.get("file"), destination_folder(body.get("folder"))
        if not isinstance(item, str) or not BOX_ID.fullmatch(item):
            abort(400, "Choose a Box video.")
        revision = box.revision()
        url = f"{BOX_ORIGIN}/2.0/files/{item}"
        with box.api(revision, "GET", url,
                     params={"fields": BOX_FIELDS + ",size,sha1,file_version,item_status"}) as response:
            check_provider(response)
            row = response_object(response)
        if row.get("id") != item or row.get("item_status") != "active" or not box_video(row):
            abort(415, "Choose a supported video that its owner allows you to download.")
        size, expected, version = row.get("size"), row.get("sha1"), row.get("file_version")
        version = version.get("id") if isinstance(version, dict) else None
        if type(size) is not int or not valid_hash(expected, 40) or not isinstance(version, str) or not BOX_ID.fullmatch(version):
            abort(422, "Box did not provide a verifiable video version.")

        def download():
            response = box.api(revision, "GET", url + "/content", params={"version": version}, stream=True)
            if response.status_code == 202:
                with response:
                    wait = response.headers.get("Retry-After", "")
                    raise TooManyRequests("Box is preparing this file. Wait and try again.",
                                          retry_after=int(wait) if re.fullmatch(r"\d{1,8}", wait) else None)
            if response.status_code != 302:
                return response
            with response:
                target = box_download_url(response.headers.get("Location", ""))
            # The signed Box URL already contains its own grant. Keep the workspace bearer on the API origin.
            return provider_request("GET", target, stream=True)

        receipt, status = import_verified(import_video, "box", actor, f"{item}/{version}", row["name"], folder,
                                          size, hashlib.sha1(usedforsecurity=False), expected, download,
                                          lambda con: box.authorize(con, revision))
        return private(receipt, status)

    return outbound
