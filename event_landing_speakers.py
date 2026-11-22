"""Live event landing speaker profiles (schema: live_landing_speaker)."""
import json
import uuid
from flask import abort, jsonify, request

from library_interactivity import text_field

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS live_landing_speaker ("
    "id TEXT PRIMARY KEY, revision TEXT NOT NULL, "
    "name TEXT NOT NULL, role TEXT NOT NULL DEFAULT '', "
    "bio TEXT NOT NULL DEFAULT '');"
)
LIMITS = {"name": 128, "role": 160, "bio": 3000}


def _row(r):
    return dict(id=r[0], revision=r[1], name=r[2], role=r[3], bio=r[4])


def profiles(con):
    rows = con.execute(
        "SELECT id,revision,name,role,bio FROM live_landing_speaker "
        "ORDER BY name COLLATE NOCASE, id").fetchall()
    return [_row(r) for r in rows]


def selected(con, ids):
    if not ids:
        return []
    rows = con.execute(
        "SELECT s.id,s.revision,s.name,s.role,s.bio "
        "FROM live_landing_speaker s "
        "JOIN json_each(?) je ON s.id = je.value "
        "ORDER BY CAST(je.key AS INTEGER)",
        (json.dumps(ids),)).fetchall()
    by_id = {r[0]: _row(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def validate_ids(con, ids):
    if not isinstance(ids, (list, tuple)):
        abort(422)
    for raw in ids:
        if not isinstance(raw, str) or not raw:
            abort(422)
    unique = list(dict.fromkeys(ids))
    if not unique:
        return []
    found = selected(con, unique)
    if len(found) != len(unique):
        abort(422)
    return unique


def _body(allowed):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(422)
    if set(data) - allowed:
        abort(422)
    return data


def _fields(d):
    name = text_field(
        d.get("name"), "a speaker name", LIMITS["name"], required=True)
    role = text_field(d.get("role", ""), "a speaker role", LIMITS["role"])
    bio = text_field(d.get("bio", ""), "a speaker bio", LIMITS["bio"])
    return name, role, bio


def register(app, tx, staff):
    @app.route("/api/live-event-speaker-profiles", methods=["GET"])
    def list_live_event_speaker_profiles():
        staff()
        with tx() as con:
            return jsonify(profiles=profiles(con))

    @app.route("/api/live-event-speaker-profiles", methods=["POST"])
    def create_live_event_speaker_profile():
        staff()
        data = _body({"name", "role", "bio"})
        name, role, bio = _fields(data)
        pid, rev = uuid.uuid4().hex, uuid.uuid4().hex
        with tx() as con:
            con.execute(
                "INSERT INTO live_landing_speaker(id,revision,name,role,bio) "
                "VALUES (?,?,?,?,?)", (pid, rev, name, role, bio))
        return jsonify(id=pid, revision=rev, name=name, role=role, bio=bio), 201

    @app.route("/api/live-event-speaker-profiles/<uid>", methods=["PUT"])
    def update_live_event_speaker_profile(uid):
        staff()
        data = _body({"name", "role", "bio", "revision"})
        rev = data.get("revision")
        if not isinstance(rev, str) or not rev:
            abort(422)
        name, role, bio = _fields(data)
        new_rev = uuid.uuid4().hex
        with tx() as con:
            cur = con.execute(
                "SELECT revision FROM live_landing_speaker WHERE id=?", (uid,)
            ).fetchone()
            if cur is None:
                abort(404)
            if cur[0] != rev:
                abort(409)
            con.execute(
                "UPDATE live_landing_speaker SET revision=?, name=?, "
                "role=?, bio=? WHERE id=?",
                (new_rev, name, role, bio, uid))
        return jsonify(id=uid, revision=new_rev, name=name, role=role, bio=bio)
