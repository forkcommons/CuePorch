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


def _translation(entry):
    allowed = {"published", "title", "description"}
    unknown = set(entry) - allowed
    if unknown:
        abort(422, "Unknown translation field.")
    title = text_field(entry.get("title", ""), "a translated title", 128)
    description = text_field(
        entry.get("description", ""), "a translated description", 5000
    )
    return {"title": title, "description": description}


def validate(raw, prior):
    return event_series_locale.validate_with(raw, prior, _translation)


def resolve(localization, event, preview=False):
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
    }
