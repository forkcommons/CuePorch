"""Workspace privacy restrictions for team roles and their Library operations."""

import json
import secrets

from flask import abort, render_template, request

from browser_account_security import json_mutation, private, same_origin, secure_site
import library_privacy as sharing
from team_accounts import CONTRIBUTORS, note


SCHEMA = """
CREATE TABLE IF NOT EXISTS team_sharing_policy(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision TEXT NOT NULL, rules TEXT NOT NULL);
"""
ROLE_NAMES = {"admin": "Administrators", "contributor": "Contributors and Contributors Plus"}
MODES = {
    "private": "Private", "password": "Password", "unlisted": "Unlisted",
    "public": "Public", "embed_only": "Embed only",
}
FEATURES = {
    "domains": "Update embed settings",
    "outbound": "Publish to third-party destinations",
    "file_links": "Manage direct video file links",
}
UNRESTRICTED = dict(modes=list(MODES), **dict.fromkeys(FEATURES, True))
DEFAULTS = {
    "admin": UNRESTRICTED,
    "contributor": dict(modes=["private"], **dict.fromkeys(FEATURES, False)),
}


def load(con):
    row = con.execute("SELECT revision,rules FROM team_sharing_policy WHERE singleton=1").fetchone()
    return (row["revision"], json.loads(row["rules"])) if row else ("", DEFAULTS)


def validate(rules):
    if not isinstance(rules, dict) or rules.keys() != ROLE_NAMES.keys():
        abort(400, "Choose sharing rules for both team roles.")
    for value in rules.values():
        if (not isinstance(value, dict) or value.keys() != {"modes", *FEATURES}
                or not isinstance(value["modes"], list) or any(
                    not isinstance(mode, str) or mode not in MODES for mode in value["modes"]
                ) or "private" not in value["modes"] or any(type(value[key]) is not bool for key in FEATURES)):
            abort(400, "Keep Private available and use the listed sharing options.")
    admin, contributor = rules["admin"], rules["contributor"]
    if set(contributor["modes"]) - set(admin["modes"]) or any(contributor[key] and not admin[key] for key in FEATURES):
        abort(400, "A Contributor cannot have sharing options that an Administrator lacks.")
    return {role: value | {"modes": [mode for mode in MODES if mode in value["modes"]]} for role, value in rules.items()}


def withdraw_removed_modes(con, removed):
    if not removed:
        return 0
    withdrawn = 0
    for row in con.execute("SELECT * FROM library_privacy_change").fetchall():
        if json.loads(row["settings"])["visibility"] in removed and not sharing.protected_route(con, row["work"]):
            sharing.cancel_pending(con, row)
    for row in con.execute("SELECT work,settings FROM library_privacy").fetchall():
        if json.loads(row["settings"])["visibility"] in removed and not sharing.protected_route(con, row["work"]):
            sharing.save(con, row["work"], json.dumps(sharing.DEFAULTS), None, secrets.token_hex(16), None)
            withdrawn += 1
    for table in ("library_upload_defaults", "library_folder_privacy"):
        con.executemany(f"""UPDATE {table} SET settings=?,password_hash=NULL
            WHERE json_extract(settings,'$.visibility')=?""", [(json.dumps(sharing.DEFAULTS), mode) for mode in removed])
    return withdrawn


def register(app, tx, origin, team_member, operator_name):
    def access(con, member=None):
        member = member if member is not None else team_member(con)
        if member and member["role"] != "owner":
            _, rules = load(con)
            role = "contributor" if member["role"] in CONTRIBUTORS else member["role"]
            return rules.get(role, DEFAULTS["contributor"])
        return UNRESTRICTED

    def enforce(con, *, settings=None, prior=None, feature=None):
        allowed = access(con)
        if feature and not allowed[feature]:
            abort(403, "This setting is restricted by the workspace sharing policy.")
        if settings is None:
            return
        if settings["visibility"] not in allowed["modes"]:
            abort(403, "This visibility is restricted by the workspace sharing policy.")
        if settings["visibility"] == "private" or allowed["domains"]:
            return
        previous = prior if prior is not None else sharing.DEFAULTS
        if (settings["embed"], settings["domains"]) != (previous["embed"], previous["domains"]):
            abort(403, "Changing embed settings is restricted by the workspace sharing policy.")

    @app.get("/team/sharing-policy")
    def team_sharing_policy_page():
        secure_site(origin)
        return private(render_template("team_sharing_policy.html", roles=ROLE_NAMES, modes=MODES, features=FEATURES))

    @app.route("/api/team/sharing-policy", methods=("GET", "PUT"))
    def team_sharing_policy():
        secure = secure_site(origin)
        same_origin(secure)
        body = json_mutation(secure, "X-CuePorch-Team") if request.method == "PUT" else None
        with tx() as con:
            actor = operator_name(con)
            member = team_member(con)
            if not actor or (member and member["role"] != "owner"):
                abort(403, "Opening the sharing policy requires the Owner or installation credential.")
            revision, rules = load(con)
            withdrawn = 0
            if body is not None:
                if body.get("revision") != revision:
                    abort(409, "Sharing restrictions changed. Reload before saving.")
                candidate = validate(body.get("rules"))
                removed = set(rules["admin"]["modes"]) - set(candidate["admin"]["modes"])
                withdrawn = withdraw_removed_modes(con, removed)
                rules, revision = candidate, secrets.token_hex(16)
                con.execute("""INSERT INTO team_sharing_policy VALUES(1,?,?) ON CONFLICT(singleton)
                    DO UPDATE SET revision=excluded.revision,rules=excluded.rules""", (revision, json.dumps(rules)))
                note(con, actor, "sharing_policy_changed", "workspace", f"{withdrawn} videos moved to Private")
            return private(dict(revision=revision, rules=rules, withdrawn=withdrawn))

    return access, enforce
