import os
import re

from flask import abort

import hubspot_crm


MEDIA_SCOPES = ("media_bridge.read", "media_bridge.write")

_MEDIA_TYPE_RE = re.compile(r"a[1-9][0-9]{0,9}_[A-Za-z][A-Za-z0-9_]{0,63}")


def object_type():
    value = os.environ.get("CUEPORCH_HUBSPOT_MEDIA_TYPE")
    if value and _MEDIA_TYPE_RE.fullmatch(value):
        return value
    return None


def _base(media_type):
    if not isinstance(media_type, str) or not _MEDIA_TYPE_RE.fullmatch(media_type):
        abort(400)
    return hubspot_crm.ORIGIN + "/crm/objects/2026-09/" + media_type


def _returned(data, external_id, remote_id=None):
    if not isinstance(data, dict):
        abort(502)
    object_id = data.get("id")
    if not hubspot_crm.segment_id(object_id):
        abort(502)
    if data.get("archived", False) is not False:
        abort(502)
    properties = data.get("properties")
    if not isinstance(properties, dict):
        abort(502)
    if properties.get("hs_external_id") != external_id:
        abort(502)
    if remote_id is not None and object_id != remote_id:
        abort(502)
    return data


def existing_object(connection, revision, media_type, external_id):
    body = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "hs_external_id",
                        "operator": "EQ",
                        "value": external_id,
                    }
                ]
            }
        ],
        "limit": 2,
        "properties": [
            "hs_external_id",
            "hs_title",
            "hs_oembed_url",
            "hs_details_page_link",
        ],
    }
    data = hubspot_crm.crm(
        connection,
        revision,
        "POST",
        _base(media_type) + "/search",
        json=body,
    )
    rows = hubspot_crm.results(data)
    total = data.get("total")
    if type(total) is not int or total != len(rows) or len(rows) > 1:
        abort(502)
    if not rows:
        return None
    return _returned(rows[0], external_id)


def create_object(connection, revision, media_type, properties, *, before_write):
    path = _base(media_type)
    before_write()
    payload = {"properties": properties}
    with connection.api(revision, "POST", path, json=payload) as response:
        if response.status_code == 201:
            body = hubspot_crm.response_object(response)
            return _returned(body, properties["hs_external_id"])
        hubspot_crm.check_provider(response)
        abort(502)


def update_object(
    connection, revision, media_type, remote_id, properties, *, before_write
):
    if not hubspot_crm.segment_id(remote_id):
        abort(400)
    path = "{}/{}".format(_base(media_type).rstrip("/"), remote_id)
    before_write()
    payload = {"properties": properties}
    with connection.api(revision, "PATCH", path, json=payload) as response:
        if response.status_code == 200:
            body = hubspot_crm.response_object(response)
            return _returned(body, properties["hs_external_id"], remote_id)
        hubspot_crm.check_provider(response)
        abort(502)


def archive_object(connection, revision, media_type, remote_id, *, before_write):
    if not hubspot_crm.segment_id(remote_id):
        abort(400)
    path = "{}/{}".format(_base(media_type).rstrip("/"), remote_id)
    before_write()
    with connection.api(revision, "DELETE", path) as response:
        if response.status_code in (204, 404):
            return None
        hubspot_crm.check_provider(response)
        abort(502)


def send_media_event(connection, revision, kind, payload, *, before_write):
    """POST a media-bridge event for the given kind and verify the echoed session."""
    if kind not in ("media-played", "media-played-percent"):
        abort(400)

    url = f"{hubspot_crm.ORIGIN}/media-bridge/2026-09/events/{kind}"

    before_write()

    with connection.api(revision, "POST", url, json=payload) as response:
        if response.status_code == 200:
            body = hubspot_crm.response_object(response)
            if body.get("sessionId") != payload.get("sessionId") or body.get(
                "mediaType"
            ) != payload.get("mediaType"):
                abort(502)
            return None

        hubspot_crm.check_provider(response)
        abort(502)
