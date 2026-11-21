"""Editor stock catalog search and signed private-library intake."""
import json
import unicodedata
import uuid

from flask import abort, jsonify, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

import stock_assets
import stock_commons
import stock_music
from stock_catalog_fetch import download


def get_param(name, limit):
    value = request.args.get(name, "").strip()
    if len(value) > limit or any(unicodedata.category(char).startswith("C") for char in value):
        abort(422, "Choose a valid catalog filter.")
    return value


def get_kind():
    kind = get_param("kind", 10) or "audio"
    if kind not in ("audio", "image", "video"):
        abort(422, "Choose music, images, or video.")
    return kind


def get_filters(kind):
    if kind == "audio":
        return stock_music.filters()
    return dict(categories=list(stock_commons.CATEGORIES), moods=[], genres=[], types=[])


def register(app, staff, signing_key, ingest):
    signer = URLSafeTimedSerializer(signing_key, salt="stock-catalog")
    @app.get("/api/library/stock-catalog/filters")
    def stock_catalog_filters():
        staff()
        return get_filters(get_kind())

    @app.get("/api/library/stock-catalog")
    def stock_catalog():
        actor = staff()
        kind = get_kind()
        filters = get_filters(kind)
        category = get_param("category", 64)
        mood = get_param("mood", 64)
        genre = get_param("genre", 64)
        type_ = get_param("type", 64)
        q = get_param("q", 120)
        raw_offset = get_param("offset", 10) or "0"
        try:
            offset = int(raw_offset)
        except (TypeError, ValueError):
            abort(422)
        if not 0 <= offset <= 990:
            abort(422, "Choose a valid catalog page.")
        if kind == "audio":
            try:
                records, next_offset = stock_music.search(category, mood, genre, type_, q, offset)
            except ValueError:
                abort(422, "Choose valid music filters.")
        else:
            category = category or next(iter(stock_commons.CATEGORIES))
            if category not in stock_commons.CATEGORIES or mood or genre or type_:
                abort(422, "Choose valid stock media filters.")
            try:
                records, next_offset = stock_commons.search(kind, category, q, offset)
            except ValueError:
                abort(502, "Wikimedia Commons search is unavailable.")
        entries = []
        for raw in records:
            entry = dict(raw)
            entry["rights"] = stock_assets.normalize_upload(json.dumps(entry["rights"]))
            token = signer.dumps({"actor": actor, "entry": entry})
            entries.append({
                "token": token,
                "title": entry.get("title"),
                "kind": entry.get("kind", kind),
                "type": entry.get("type"),
                "rights": entry["rights"],
            })
        return jsonify({
            "items": entries,
            "next_offset": next_offset,
            "filters": filters,
        })

    @app.post("/api/library/stock-catalog/import")
    def import_stock_catalog():
        actor = staff()
        request.max_content_length = 16384
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            abort(400)
        token = body.get("token")
        if not isinstance(token, str) or len(token) > 8192:
            abort(400)
        folder = body.get("folder")
        if folder is not None and (not isinstance(folder, str) or len(folder) > 100):
            abort(400)
        try:
            data = signer.loads(token, max_age=1200)
        except BadSignature:
            abort(422, "Choose an item from a current catalog search.")
        if data.get("actor") != actor:
            abort(422)
        entry = data["entry"]
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(entry, sort_keys=True)))
        title = entry["title"].replace("/", "_").replace("\\", "_")
        filename = f'{title}.{entry["extension"]}'
        return ingest(actor, identity, filename, folder or None,
                      lambda path: download(entry, path), stock_asset=entry["rights"])
