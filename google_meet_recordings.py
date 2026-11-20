"""Google Meet's conference and generated recording resources."""

from datetime import datetime, timezone
import json
import re

from flask import abort

from workspace_oauth import check_provider, response_object

API = "https://meet.googleapis.com/v2/"
SCOPES = "https://www.googleapis.com/auth/meetings.space.readonly https://www.googleapis.com/auth/drive.meet.readonly"
CONFERENCE = re.compile(r"conferenceRecords/([A-Za-z0-9_-]{1,256})")
RECORDING = re.compile(r"conferenceRecords/[A-Za-z0-9_-]{1,256}/recordings/[A-Za-z0-9_-]{1,256}")
DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")
POLL_SECONDS = 120


def get(connection, revision, resource, params=None):
    with connection.api(revision, "GET", API + resource, params=params) as response:
        check_provider(response)
        return response_object(response)


def page(connection, revision, resource, key, cursor=None, **options):
    params = {"pageSize": 30} | options
    if cursor:
        params["pageToken"] = cursor
    result = get(connection, revision, resource, params)
    rows, following = result.get(key, []), result.get("nextPageToken")
    if not isinstance(rows, list) or (following is not None and
                                     (not isinstance(following, str) or len(following) > 2048)):
        abort(502, "Google Meet returned an invalid page of recordings.")
    return rows, following


def label_at(row, label):
    value = row.get("startTime")
    try:
        when = datetime.fromisoformat(value) if isinstance(value, str) else None
        if when and when.tzinfo:
            return f"{label} · {when.astimezone(timezone.utc):%Y-%m-%d %H:%M UTC}"
    except (ValueError, OverflowError):
        pass
    return label


def drive_file(row):
    destination = row.get("driveDestination")
    item = destination.get("file") if isinstance(destination, dict) else None
    if row.get("state") != "FILE_GENERATED" or not isinstance(item, str) or not DRIVE_ID.fullmatch(item):
        return None
    return item


def scan_new_recordings(connection, watch):
    revision = watch["connection"]
    state = json.loads(watch["cursor"]) if watch["cursor"] else {
        "since": max(watch["started"], (watch["checked"] or watch["started"]) - 2 * 86400),
        "parents": [],
    }
    if not state["parents"]:
        since = datetime.fromtimestamp(state["since"], timezone.utc).isoformat().replace("+00:00", "Z")
        conferences, following = page(connection, revision, "conferenceRecords", "conferenceRecords",
                                      state.get("conferences_page"), filter=f'end_time>="{since}"', pageSize=8)
        state["conferences_page"] = following
        for row in conferences:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not CONFERENCE.fullmatch(row["name"]):
                continue
            ended = row.get("endTime")
            try:
                end = datetime.fromisoformat(ended) if isinstance(ended, str) else None
                if end and end.tzinfo and end.timestamp() >= state["since"]:
                    state["parents"].append(row["name"])
            except (ValueError, OverflowError, OSError):
                continue
    entries = []
    if state["parents"]:
        parent = state["parents"][0]
        rows, following = page(connection, revision, parent + "/recordings", "recordings",
                               state.get("recordings_page"))
        for row in rows:
            name = row.get("name") if isinstance(row, dict) else None
            if isinstance(name, str) and RECORDING.fullmatch(name) and name.startswith(parent + "/"):
                if item := drive_file(row):
                    entries.append(dict(id=name, rev=item, name=label_at(row, "Meeting recording")))
        state["recordings_page"] = following
        if not following:
            state["parents"].pop(0)
    more = bool(state["parents"] or state.get("conferences_page"))
    return dict(entries=entries, cursor=json.dumps(state) if more else "", has_more=more)


def browse(connection, revision, conference, cursor, search):
    if conference:
        parent = "conferenceRecords/" + conference
        if not CONFERENCE.fullmatch(parent):
            abort(400, "Choose a Google Meet conference.")
        rows, following = page(connection, revision, parent + "/recordings", "recordings", cursor)
        files = []
        for row in rows:
            name = row.get("name") if isinstance(row, dict) else None
            if (isinstance(name, str) and RECORDING.fullmatch(name) and name.startswith(parent + "/")
                    and drive_file(row)):
                label = label_at(row, "Recording")
                if search in label.casefold():
                    files.append(dict(id=name, name=label))
        return dict(files=files, folders=[], cursor=following)
    rows, following = page(connection, revision, "conferenceRecords", "conferenceRecords", cursor)
    folders = []
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        match = CONFERENCE.fullmatch(name) if isinstance(name, str) else None
        if match and row.get("endTime"):
            label = label_at(row, "Meeting")
            if search in label.casefold():
                folders.append(dict(id=match[1], name=label))
    return dict(files=[], folders=folders, cursor=following)


def recording_file(connection, revision, recording):
    if not isinstance(recording, str) or not RECORDING.fullmatch(recording):
        abort(400, "Choose a Google Meet recording.")
    row = get(connection, revision, recording)
    item = drive_file(row)
    if row.get("name") != recording or not item:
        abort(409, "This Google Meet recording is not ready for import.")
    return item
