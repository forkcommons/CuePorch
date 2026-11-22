"""Translate documented Vimeo visibility metadata into CuePorch sharing rules."""

import json

from flask import abort, request

import library_privacy as sharing
import library_peer_handoff as handoff
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


def catalog_state(con, work):
    if work.get("provider") != "vimeo":
        return None
    settings, issue = target_settings(work)
    if settings["visibility"] != "public":
        return None

    uid = work["id"]
    if sharing.blocked(con, uid) or sharing.playback(work) is None:
        return dict(state="unavailable", issue=issue)
    transferring = con.execute(
        "SELECT 1 FROM publication_job WHERE work=? AND want_public=1 AND status IN ('queued','waiting','processing') LIMIT 1",
        (uid,),
    ).fetchone()
    if handoff.managed_route(con, uid) is not None or handoff.get(con, uid) is not None or transferring:
        state = "local"
    elif con.execute("SELECT 1 FROM library_privacy_change WHERE work=?", (uid,)).fetchone():
        state = "pending"
    elif (row := sharing.get_privacy(con, uid)) is not None:
        state = "existing" if json.loads(row["settings"]) == settings else "local"
    else:
        state = "ready"
    return dict(state=state, issue=issue)


def register(app, tx, staff, action, read_works, enforce_sharing):
    @app.route("/api/library/work/<uid>/source-privacy", methods=["GET", "PUT"])
    def restore_source_privacy(uid):
        staff()
        work = next((item for item in read_works() if item["id"] == uid), None)
        with tx() as con:
            if not work or work["provider"] != "vimeo" or hidden(con, "work", uid):
                abort(404)
            settings, issue = target_settings(work)
            managed_video = handoff.managed_route(con, uid)
            recent_handoff = handoff.get(con, uid)
            transferring = con.execute(
                "SELECT 1 FROM publication_job WHERE work=? AND want_public=1 AND status IN ('queued','waiting','processing') LIMIT 1",
                (uid,),
            ).fetchone()
            eligible = sharing.playback(work) is not None and managed_video is not False and not transferring
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
                enforce_sharing(con, settings=settings, prior=json.loads(current["settings"]) if current else None)
                if managed_video:
                    handoff.request_handoff(con, uid, managed_video)
                    recent_handoff = handoff.get(con, uid)
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
                hosted=bool(managed_video),
                handoff=recent_handoff["state"] if recent_handoff else None,
                handoff_error=recent_handoff["error"] if recent_handoff else None,
                paths=sharing.share_paths(uid, current) if current and permits(work, json.loads(current["settings"])) else None,
            )
