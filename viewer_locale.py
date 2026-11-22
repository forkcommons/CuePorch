
"""Viewer language preference resolution.

Small, side-effect-free helper. Never touches the database or runtime state;
it only reads Flask's request.accept_languages during a request. Safe to call
from GET handlers without any mutation.
"""

from flask import request

from registration_locale import LANGUAGES as REGISTRATION_LANGUAGES
import viewer_locale_labels as labels


# Viewer locales supplied by the translated Watch interface.
_ORDERED_CODES = ("en", "fr", "es", "de", "pt-BR", "ko", "ja")
LANGUAGES = {code: REGISTRATION_LANGUAGES[code] for code in _ORDERED_CODES}
DEFAULT = "en"


def choose(preferences):
    """Pick the effective viewer language.

    Priority:
      1. Explicit whitelisted ``preferences["language"]``.
      2. First winning tag from ``request.accept_languages`` (Werkzeug keeps
         the client's q-coherent order, so ties resolve consistently):
           - exact match against LANGUAGES
           - pt / pt-*  ->  pt-BR
           - base match (en-US -> en, es-MX -> es, ja-JP -> ja, ...)
      3. ``DEFAULT`` (en).
    """
    if isinstance(preferences, dict):
        explicit = preferences.get("language")
        if isinstance(explicit, str) and explicit in LANGUAGES:
            return explicit
    for tag, quality in request.accept_languages:
        if quality <= 0:
            continue
        normalized = _normalize(tag)
        if normalized:
            return normalized
    return DEFAULT


def _normalize(tag):
    if not tag:
        return None
    lowered = tag.lower().replace("_", "-")
    for code in LANGUAGES:
        if code.lower() == lowered:
            return code
    if lowered == "pt" or lowered.startswith("pt-"):
        return "pt-BR"
    base = lowered.split("-", 1)[0]
    for code in LANGUAGES:
        if code.lower().split("-", 1)[0] == base:
            return code
    return None

def player_context(preferences):
    language = choose(preferences)
    return dict(
        player_language=language,
        player_labels=dict(zip(labels.PLAYER_CORE_KEYS, labels.PLAYER_CORE[language], strict=True)),
    )


def template_context(preferences):
    language = choose(preferences)
    return dict(
        viewer_language=language,
        viewer_languages=LANGUAGES,
        viewer_labels=dict(zip(labels.WATCH_KEYS, labels.WATCH[language], strict=True))
        | dict(zip(labels.VIEWER_KEYS, labels.VIEWER[language], strict=True))
        | dict(zip(labels.FEED_KEYS, labels.FEED[language], strict=True))
        | dict(zip(labels.CATALOG_NAV_KEYS, labels.CATALOG_NAV[language], strict=True))
        | dict(zip(labels.CATALOG_RESULT_KEYS, labels.CATALOG_RESULTS[language], strict=True))
        | dict(zip(labels.CATALOG_PICK_KEYS, labels.CATALOG_PICKS[language], strict=True)),
        feed_feedback=tuple(values[_ORDERED_CODES.index(language)] for values in labels.FEEDBACK.values()),
    )
