"""Translate documented Vimeo visibility metadata into CuePorch sharing rules."""

import json

from flask import abort, request

import library_privacy as sharing
from library_trash import hidden

VIEW_MODES = {
    "private": "private", "nobody": "private",
    "public": "public", "anybody": "public",
    "unlisted": "unlisted", "password": "password",
    "disable": "embed_only", "embed_only": "embed_only",
}


def target_settings(work):
    """Return an enforceable target rule and the reason for any access reduction."""
    mode = VIEW_MODES.get(work.get("source_privacy"))
    if mode is None:
        return sharing.DEFAULTS, "Source access depends on account permissions or an unsupported privacy setting."
    if mode == "private":
        return sharing.DEFAULTS, None

    source_embed = work.get("source_embed_policy")
    embed = "disabled"
    domains = []
    issue = None
    if source_embed in {"public", "anywhere"}:
        embed = "anywhere"
    elif source_embed == "whitelist":
        recorded = work.get("allowed_domains", [])
        try:
            domains = sorted(set(map(sharing.normalize_domain, recorded))) if isinstance(recorded, list) else []
        except (ValueError, UnicodeError):
            domains = []
        if domains and len(domains) <= 50:
            embed = "domains"
        else:
            domains = []
            issue = "The Vimeo embed allowlist could not be recovered. Embedding stays off."
    elif source_embed not in {"private", "disabled"}:
        issue = "The Vimeo embed setting could not be read. Embedding stays off."
    if mode == "embed_only" and embed == "disabled":
        return sharing.DEFAULTS, issue or "The source video is embed only and no embed sites are authorized."

    details = work.get("source_privacy_details", {})
    downloads = isinstance(details, dict) and details.get("download") is True
    return dict(visibility=mode, embed=embed, domains=domains, download=downloads), issue


def permits(work, settings):
    return work["provider"] != "vimeo" or target_settings(work)[0] == settings


def register(app, tx, staff, action, read_works):
    @app.route("/api/library/work/<uid>/source-privacy", methods=["GET", "PUT"])
    def restore_source_privacy(uid):
        staff()
        work = next((item for item in read_works() if item["id"] == uid), None)
        with tx() as con:
            if not work or work["provider"] != "vimeo" or hidden(con, "work", uid):
                abort(404)
            settings, issue = target_settings(work)
            hosted = con.execute(
                "SELECT 1 FROM route WHERE slug=? UNION ALL "
                "SELECT 1 FROM publication_job WHERE work=? AND want_public=1 AND status IN ('queued','waiting','processing') LIMIT 1",
                (uid, uid),
            ).fetchone()
            eligible = sharing.playback(work) is not None and not hosted
            current = sharing.get_privacy(con, uid)
            if request.method == "PUT":
                if not eligible:
                    abort(409, "Recover an MP4 viewing copy and complete any public hosted handoff before restoring source privacy.")
                body = request.get_json()
                if not isinstance(body, dict):
                    abort(400)
                settings, password = sharing.requested_settings(
                    dict(settings, password=body.get("password", "")),
                    current["password_hash"] if current and json.loads(current["settings"])["visibility"] == "password" else None,
                )
                sharing.apply_settings(con, action, work, settings, password)
                sharing.apply_pending(con)
                current = sharing.get_privacy(con, uid)
            pending = con.execute("SELECT 1 FROM library_privacy_change WHERE work=?", (uid,)).fetchone()
            return dict(
                settings=settings,
                issue=issue,
                eligible=eligible,
                has_password=bool(current and current["password_hash"]),
                pending=bool(pending),
                paths=sharing.share_paths(uid, current) if current and permits(work, json.loads(current["settings"])) else None,
            )
