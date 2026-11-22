import event_series_locale
from flask import abort
from library_interactivity import text_field

DEFAULTS = event_series_locale.DEFAULTS

SPEAKERS = {
    "en": "Speakers",
    "fr": "Intervenants",
    "es": "Ponentes",
    "de": "Referenten",
    "ja": "登壇者",
    "ko": "발표자",
    "pt-BR": "Palestrantes",
}

LANGUAGES = event_series_locale.LANGUAGES


def _speaker_entry(entry):
    allowed = {"role", "bio", "base_role", "base_bio"}
    if set(entry) - allowed:
        abort(422, "Unknown speaker field.")
    return {
        "role": text_field(entry.get("role", ""), "a translated role", 160),
        "bio": text_field(entry.get("bio", ""), "a translated bio", 3000),
        "base_role": text_field(entry.get("base_role", ""), "a base role", 160),
        "base_bio": text_field(entry.get("base_bio", ""), "a base bio", 3000),
    }


def _translation(entry, speaker_ids):
    allowed = {"published", "title", "description", "speakers"}
    if set(entry) - allowed:
        abort(422, "Unknown translation field.")
    result = {
        "title": text_field(entry.get("title", ""), "a translated title", 128),
        "description": text_field(entry.get("description", ""), "a translated description", 5000),
    }
    raw = entry.get("speakers")
    if raw is not None:
        if not isinstance(raw, dict):
            abort(422, "Malformed speakers mapping.")
        speakers = {}
        for sid, rec in raw.items():
            if sid not in speaker_ids:
                continue
            if not isinstance(rec, dict):
                abort(422, "Malformed speaker record.")
            speakers[sid] = _speaker_entry(rec)
        if speakers:
            result["speakers"] = speakers
    return result


def validate(raw, prior, speaker_ids):
    return event_series_locale.validate_with(
        raw, prior, lambda entry: _translation(entry, speaker_ids)
    )


def translate_speakers(translated, speakers):
    translations = (translated or {}).get("speakers", {}) or {}
    result = []
    for speaker in speakers:
        item = dict(speaker)
        translation = translations.get(speaker.get("id")) or {}
        item["role"] = translation.get("role") or speaker.get("role")
        item["bio"] = translation.get("bio") or speaker.get("bio")
        result.append(item)
    return result


def resolve(localization, event, speakers, preview=False):
    language, language_options = event_series_locale.choose_language(localization, preview)
    translated = localization['translations'].get(language, {})
    title = translated.get("title") or event.get("title", "")
    description = translated.get("description") or event.get("description", "")
    labels = event_series_locale.labels(language) | {
        "speakers": SPEAKERS[language]
    }
    return {
        "language": language,
        "language_options": language_options,
        "title": title,
        "description": description,
        "labels": labels,
        "speakers": translate_speakers(translated, speakers),
    }
