"""Workspace integration directory and verified cloud video intake."""

import hashlib
import hmac
import json
import re
import uuid

import requests
from flask import abort, render_template, request

from browser_account_security import json_mutation, private, same_origin, secure_site
from library_analytics import source_origin
from library_upload import VIDEO_INPUT, max_upload_bytes
from workspace_oauth import (
    CALLBACK_ROOT, DIRECTORY, Config, Connection, check_provider, response_object,
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
                        checksum.update(chunk)
                        stream.write(chunk)
            except requests.RequestException:
                abort(502, "The provider interrupted the transfer. Try again.")
            if total != size or not hmac.compare_digest(checksum.hexdigest(), expected):
                abort(422, "The video did not match its listed size and checksum. Try again.")

    work = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cueporch/{provider}/{identity}/{expected}"))
    return import_video(actor, work, name, folder, save, authorize)


def dropbox_entries(connection, revision, source="", cursor=None):
    suffix = "/continue" if cursor else ""
    fields = {"cursor": cursor} if cursor else {"path": source, "recursive": False, "limit": 50,
                                               "include_non_downloadable_files": False}
    with connection.api(revision, "POST", DROPBOX_RPC + "/files/list_folder" + suffix, json=fields) as response:
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


def register(app, tx, staff, operator, origin, import_video):
    drive = Connection(Config("google-drive", "Google Drive", AUTH, TOKEN_URL, REVOKE, SCOPE,
                              dict(access_type="offline", prompt="consent")), tx, operator)
    dropbox = Connection(Config(
        "dropbox", "Dropbox", DROPBOX_AUTH, DROPBOX_TOKEN, DROPBOX_RPC + "/auth/token/revoke",
        DROPBOX_SCOPE, dict(token_access_type="offline"), revoke_using_access_token=True,
    ), tx, operator)
    connections = {item.config.key: item for item in (drive, dropbox)}

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

    @app.after_request
    def protect_connection_responses(response):
        if request.path.startswith((CALLBACK_ROOT, "/api/integrations")):
            response.cache_control.no_store = True
            response.cache_control.private = True
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/api/integrations")
    def integrations():
        read_api()
        with tx() as con:
            return private(dict(integrations=[dict(
                id=item.config.key, name=item.config.name, category="Cloud storage",
                description="Browse and copy videos into your private Library.",
                configured=item.configured, connected=item.connection(con) is not None,
                browse_folders=item is dropbox,
            ) for item in connections.values()]))

    @app.post("/api/integrations/<provider>/connect")
    def connect(provider):
        _, _, secure, current = mutate()
        return configured_provider(provider).prepare(secure, current)

    @app.get(CALLBACK_ROOT + "<provider>/callback")
    def oauth_callback(provider):
        return configured_provider(provider).complete(*site())

    @app.post("/api/integrations/<provider>/disconnect")
    def disconnect(provider):
        mutate()
        return private(dict(connected=False, provider_revoked=configured_provider(provider).disconnect()))

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
        url = FILES + "/" + item
        with drive.api(revision, "GET", url, params=dict(fields=FIELDS, supportsAllDrives="true")) as response:
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
            return drive.api(revision, "GET", url, params=dict(alt="media", supportsAllDrives="true"), stream=True)

        receipt, status = import_verified(import_video, "google-drive", actor, f"{item}/{version}", row["name"],
                                          folder, int(size), checksum, expected, download,
                                          lambda con: drive.authorize(con, revision))
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
