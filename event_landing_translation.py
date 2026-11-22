"""Translate saved single-event page text with the configured local runtime."""

import hashlib
import json
import uuid

import caption_adapter
import event_landing
import event_landing_locale
import event_landing_speakers
from library_interactivity import text_field


def read_source(con, uid):
    cur = con.execute(
        "SELECT e.revision AS event_revision, e.settings AS event_settings, "
        "l.revision AS landing_revision, l.settings AS landing_settings "
        "FROM live_event e "
        "JOIN live_event_landing l ON l.uid = e.id "
        "WHERE e.id = ?",
        (uid,),
    )
    row = cur.fetchone()
    if row is None:
        return None

    settings = event_landing.DEFAULTS | json.loads(row["landing_settings"])
    if not settings["enabled"]:
        return None

    speakers = [
        {"id": s["id"], "role": s["role"], "bio": s["bio"]}
        for s in event_landing_speakers.selected(con, settings["speaker_ids"])
    ]

    revision = hashlib.sha256(
        json.dumps(
            [row["event_revision"], row["landing_revision"], speakers],
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    event = json.loads(row["event_settings"])
    return {
        "uid": uid,
        "revision": revision,
        "landing_revision": row["landing_revision"],
        "settings": settings,
        "title": event["title"],
        "description": event.get("description", ""),
        "speakers": speakers,
    }


def translation_fields(source, language, stopping):
    result = {"title": "", "description": "", "published": False}
    speakers = {}

    for speaker in source.get("speakers", []):
        speaker_id = speaker.get("id")
        role = speaker.get("role", "")
        bio = speaker.get("bio", "")
        if (role and role.strip()) or (bio and bio.strip()):
            speakers[speaker_id] = {
                "role": "",
                "bio": "",
                "base_role": role,
                "base_bio": bio,
            }

    if speakers:
        result["speakers"] = speakers

    work = [
        (result, "title", source.get("title", ""), 128),
        (result, "description", source.get("description", ""), 5000),
    ]

    for speaker in source.get("speakers", []):
        speaker_id = speaker.get("id")
        if speaker_id not in speakers:
            continue
        target = speakers[speaker_id]
        role = speaker.get("role", "")
        bio = speaker.get("bio", "")
        if role and role.strip():
            work.append((target, "role", role, 160))
        if bio and bio.strip():
            work.append((target, "bio", bio, 3000))

    work = [record for record in work if record[2]]
    translated = caption_adapter.translate_cues(
        [dict(text=text) for _, _, text, _ in work], language, stopping
    )
    if not isinstance(translated, list) or len(translated) != len(work):
        raise caption_adapter.CaptionError("The translation was incomplete.")
    for (target, key, _, limit), item in zip(work, translated, strict=True):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("text"), str)
            or not item["text"].strip()
        ):
            raise caption_adapter.CaptionError("The translation was incomplete.")
        target[key] = text_field(
            item["text"], "a translated " + key, limit, required=True
        )
    return result


def save_translation(con, source, language, translated):
    settings = source["settings"]
    prior = settings["localization"]
    translations = prior["translations"] | {language: translated | {"published": True}}
    localized = event_landing_locale.validate(
        dict(base=prior["base"], translations=translations),
        prior,
        settings["speaker_ids"],
    )
    updated = settings | {"localization": localized}
    con.execute(
        event_landing.UPSERT,
        (source["uid"], uuid.uuid4().hex, json.dumps(updated, sort_keys=True)),
    )
