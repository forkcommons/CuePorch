"""Workspace controls for generated caption and subtitle jobs."""

import csv
from datetime import datetime, timezone
import io
import json
import re
import secrets
import time

from flask import abort, render_template, request

from browser_account_security import json_mutation, private, same_origin, secure_site
from library_languages import language_tag
from team_accounts import note
from team_group_roles import EFFECTIVE_ROLE


SCHEMA = """
CREATE TABLE IF NOT EXISTS team_ai_policy(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision TEXT NOT NULL, settings TEXT NOT NULL);
"""
FEATURES = {"caption": "Generate captions", "translation": "Translate subtitles",
            "ai_details": "Generate video details", "ai_chapters": "Generate chapters"}
DEFAULTS = {
    "features": {key: {"enabled": True, "administrators": True} for key in FEATURES},
    "dictionary": [],
}
MAX_TERMS = 200


def load(con):
    row = con.execute("SELECT revision,settings FROM team_ai_policy WHERE singleton=1").fetchone()
    if not row:
        return "", DEFAULTS
    settings = json.loads(row["settings"])
    settings["features"] = DEFAULTS["features"] | settings["features"]
    return row["revision"], settings


def validate(settings):
    if not isinstance(settings, dict) or settings.keys() != DEFAULTS.keys():
        abort(400, "Supply generation settings and the subtitle dictionary.")
    features = settings["features"]
    if not isinstance(features, dict) or features.keys() != FEATURES.keys():
        abort(400, "Choose settings for the listed generation workflows.")
    for rule in features.values():
        if (not isinstance(rule, dict) or rule.keys() != {"enabled", "administrators"}
                or any(type(value) is not bool for value in rule.values())):
            abort(400, "Use the listed workflow controls.")
    dictionary = settings["dictionary"]
    if not isinstance(dictionary, list) or len(dictionary) > MAX_TERMS:
        abort(400, f"Use up to {MAX_TERMS} dictionary entries.")
    terms = []
    seen = set()
    for entry in dictionary:
        if (not isinstance(entry, dict) or entry.keys() != {"language", "source", "preferred"}
                or any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 100
                       or re.search(r"[\x00-\x1f<>]", value) for value in entry.values())):
            abort(400, "Each dictionary line needs a language, original phrase and preferred phrase.")
        language = "*" if entry["language"].strip() == "*" else language_tag(entry["language"])
        source, preferred = entry["source"].strip(), entry["preferred"].strip()
        key = (language, source.lower())
        if key in seen:
            abort(400, "Each original phrase can appear once per language.")
        seen.add(key)
        terms.append(dict(language=language, source=source, preferred=preferred))
    return dict(features=features, dictionary=terms)


def restriction(con, feature, member, settings=None):
    settings = load(con)[1] if settings is None else settings
    rule = settings["features"][feature]
    if not rule["enabled"]:
        return "This workflow is disabled in workspace AI settings."
    if member is not None:
        row = con.execute(f"""SELECT {EFFECTIVE_ROLE} AS role FROM team_member m
            WHERE m.id=? AND m.removed IS NULL""", (member,)).fetchone()
        if not row or (row["role"] != "owner" and (row["role"] != "admin" or not rule["administrators"])):
            return "Your team role cannot run this workflow under workspace AI settings."
    return None


def apply_dictionary(cues, language, entries):
    applicable = [entry for entry in entries if entry["language"] in ("*", language)]
    if not applicable:
        return cues
    # Exact language entries take precedence over all-language entries for the same phrase.
    terms = {entry["source"].lower(): entry["preferred"] for entry in sorted(
        applicable, key=lambda item: item["language"] != "*",
    )}
    ordered = sorted(terms, key=len, reverse=True)
    alternatives = "|".join(f"({re.escape(term)})" for term in ordered)
    pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)

    def preferred(text):
        # Preserve WebVTT formatting and entities while matching prose case-insensitively.
        return "".join(
            chunk if index % 2 else pattern.sub(lambda match: terms[ordered[match.lastindex - 1]], chunk)
            for index, chunk in enumerate(re.split(r"(<[^<>]*>|&(?:\w+|#\d+|#x[0-9a-fA-F]+);)", text))
        )

    return [dict(cue, text=preferred(cue["text"])) for cue in cues]


def usage_report(con):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(("requested_at_utc", "workflow", "job_id", "work_id", "requested_by", "language", "status",
                     "published_cues"))
    for job in con.execute("""WITH jobs AS (
        SELECT j.id,j.work,j.language,j.created,j.requested_by,j.state,
            CASE WHEN j.requested_by IS NULL AND j.state='complete' AND j.cue_count=0
                THEN NULL ELSE j.cue_count END AS cue_count
        FROM caption_job j
        UNION ALL SELECT id,work,source_language,created,requested_by,state,NULL FROM library_ai_draft
        )
        SELECT j.*,a.step,CASE
            WHEN a.status='rejected' AND j.state='queued' THEN 'rejected'
            WHEN a.status='waiting' AND j.state='queued' THEN 'waiting'
            ELSE j.state END AS public_state
        FROM jobs j JOIN action a ON a.id=j.id WHERE j.created>=? ORDER BY j.created DESC,j.id""",
        (time.time() - 90 * 86400,),
    ):
        actor = job["requested_by"] or ""
        if actor.lstrip()[:1] in ("=", "+", "-", "@") or actor.startswith(("\t", "\r", "\n")):
            actor = "'" + actor
        writer.writerow((datetime.fromtimestamp(job["created"], timezone.utc).isoformat(), job["step"], job["id"],
                         job["work"], actor, job["language"], job["public_state"], job["cue_count"]))
    response = private(output.getvalue())
    response.mimetype = "text/csv"
    response.headers["Content-Disposition"] = 'attachment; filename="cueporch-ai-usage.csv"'
    return response


def register(app, tx, origin, operator_name, team_member):
    def actor(con):
        member = team_member(con)
        name = operator_name(con)
        if not name:
            abort(403)
        return name, member["id"] if member else None

    def owner(con):
        member = team_member(con)
        name = operator_name(con)
        if not name or (member and member["role"] != "owner"):
            abort(403, "Open AI settings with the workspace Owner or installation credential.")
        return name

    @app.get("/team/ai")
    def team_ai_page():
        secure_site(origin)
        return private(render_template("team_ai_policy.html", features=FEATURES, max_terms=MAX_TERMS))

    @app.route("/api/team/ai", methods=("GET", "PUT"))
    def team_ai_settings():
        secure = secure_site(origin)
        same_origin(secure)
        request.max_content_length = 256 * 1024
        body = json_mutation(secure, "X-CuePorch-Team") if request.method == "PUT" else None
        with tx() as con:
            name = owner(con)
            revision, settings = load(con)
            if body is not None:
                if body.get("revision") != revision:
                    abort(409, "AI settings changed. Reload before saving.")
                settings, revision = validate(body.get("settings")), secrets.token_hex(16)
                con.execute("""INSERT INTO team_ai_policy VALUES(1,?,?) ON CONFLICT(singleton)
                    DO UPDATE SET revision=excluded.revision,settings=excluded.settings""",
                    (revision, json.dumps(settings)))
                note(con, name, "ai_policy_changed", "workspace")
            return private(dict(revision=revision, settings=settings))

    @app.get("/api/team/ai/usage")
    def team_ai_usage():
        secure = secure_site(origin)
        same_origin(secure)
        with tx() as con:
            owner(con)
            return usage_report(con)

    return actor
