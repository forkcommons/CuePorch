"""Manual language variants for Event Series viewer pages."""

from flask import abort, request

import registration_locale
from library_interactivity import text_field

LANGUAGES = {k: v for k, v in registration_locale.LANGUAGES.items() if k != "zh-Hans"}
DEFAULTS = {"base": "en", "translations": {}}
TRANSLATION_FIELDS = {"published", "title", "description", "date_label", "timezone_label"}


def validate(raw, prior):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        abort(422, "Unknown locale field.")
    if set(raw) - set(DEFAULTS):
        abort(422, "Unknown locale field.")

    values = dict(DEFAULTS)
    if isinstance(prior, dict):
        for key in DEFAULTS:
            if key in prior and key not in raw:
                values[key] = prior[key]
    values.update(raw)

    base = values["base"]
    if not isinstance(base, str) or base not in LANGUAGES:
        abort(422, "Select a supported base language.")

    translations = values["translations"]
    if translations is None:
        translations = {}
    if not isinstance(translations, dict):
        abort(422, "Invalid translations.")
    if len(translations) > 6:
        abort(422, "At most 6 translations.")

    prior_base = prior.get("base") if isinstance(prior, dict) else None
    prior_translations = prior.get("translations") if isinstance(prior, dict) else None
    if prior_translations and base != prior_base:
        abort(422, "Clear translations before changing base language.")

    result = {"base": base, "translations": {}}
    for lang, entry in translations.items():
        if not isinstance(lang, str) or lang not in LANGUAGES:
            abort(422, "Unsupported translation language.")
        if lang == base:
            abort(422, "Base language cannot also be a translation.")
        if not isinstance(entry, dict):
            abort(422, "Invalid translation.")
        if set(entry) - TRANSLATION_FIELDS:
            abort(422, "Unknown translation field.")

        published = entry.get("published", False)
        if not isinstance(published, bool):
            abort(422, "Invalid published flag.")

        result["translations"][lang] = {
            "published": published,
            "title": text_field(entry.get("title", ""), "title", 128),
            "description": text_field(entry.get("description", ""), "description", 5000),
            "date_label": text_field(entry.get("date_label", ""), "date label", 100),
            "timezone_label": text_field(entry.get("timezone_label", ""), "timezone label", 100),
        }

    return result


def resolve(localization, settings, presentation, preview=False):
    base = localization["base"]
    translations = localization["translations"]

    available = [base] + [
        code
        for code in LANGUAGES
        if code in translations and (preview or translations[code]["published"])
    ]

    language = None

    requested = request.args.get("lang")
    if requested is not None:
        for code in available:
            if code.lower() == requested.lower():
                language = code
                break

    if language is None:
        for tag, quality in request.accept_languages:
            if quality <= 0:
                continue
            primary = tag.split("-", 1)[0].lower()
            for code in available:
                if code.lower() == tag.lower() or code.split("-", 1)[0].lower() == primary:
                    language = code
                    break
            if language is not None:
                break

    if language is None:
        language = base

    translated = translations.get(language, {})

    language_options = [
        {"code": code, "label": LANGUAGES[code]}
        for code in available
    ]

    viewer_settings = {
        "title": translated.get("title") or settings.get("title"),
        "description": translated.get("description") or settings.get("description"),
    }

    presentation_copy = dict(presentation)
    presentation_copy["date_label"] = (
        translated.get("date_label") or presentation.get("date_label")
    )
    presentation_copy["timezone_label"] = (
        translated.get("timezone_label") or presentation.get("timezone_label")
    )

    return language, language_options, viewer_settings, presentation_copy


KEYS = (
    "language",
    "upcoming",
    "no_upcoming",
    "view",
    "on_demand",
    "no_recordings",
    "watch",
    "agenda",
    "no_agenda",
    "faq",
    "private_preview",
    "protected",
    "password",
    "incorrect",
    "unlock",
)

STRINGS = {
    "en": (
        "Language",
        "Upcoming events",
        "No upcoming events.",
        "View",
        "On demand",
        "No recordings yet.",
        "Watch",
        "Agenda",
        "No agenda items.",
        "FAQ",
        "Private operator preview",
        "Protected event series",
        "Event password",
        "That password did not match.",
        "Open event series",
    ),
    "fr": (
        "Langue",
        "À venir",
        "Aucun événement à venir",
        "Voir",
        "À la demande",
        "Aucun enregistrement disponible",
        "Regarder",
        "Programme",
        "Aucun programme disponible",
        "FAQ",
        "Aperçu privé",
        "Protégé",
        "Mot de passe",
        "Mot de passe incorrect",
        "Déverrouiller",
    ),
    "es": (
        "Idioma",
        "Próximos",
        "No hay eventos próximos",
        "Ver",
        "Bajo demanda",
        "No hay grabaciones disponibles",
        "Reproducir",
        "Programa",
        "No hay programa disponible",
        "Preguntas frecuentes",
        "Vista previa privada",
        "Protegido",
        "Contraseña",
        "Contraseña incorrecta",
        "Desbloquear",
    ),
    "de": (
        "Sprache",
        "Kommende",
        "Keine kommenden Veranstaltungen",
        "Ansehen",
        "Auf Abruf",
        "Keine Aufzeichnungen verfügbar",
        "Wiedergeben",
        "Agenda",
        "Keine Agenda verfügbar",
        "FAQ",
        "Private Vorschau",
        "Geschützt",
        "Passwort",
        "Falsches Passwort",
        "Entsperren",
    ),
    "ja": (
        "言語",
        "今後の予定",
        "今後のイベントはありません",
        "表示",
        "オンデマンド",
        "利用可能な録画はありません",
        "視聴",
        "プログラム",
        "プログラムはありません",
        "よくある質問",
        "プライベートプレビュー",
        "保護されています",
        "パスワード",
        "パスワードが正しくありません",
        "ロックを解除",
    ),
    "ko": (
        "언어",
        "예정된 이벤트",
        "예정된 이벤트가 없습니다",
        "보기",
        "주문형",
        "사용 가능한 녹화가 없습니다",
        "시청",
        "아젠다",
        "아젠다가 없습니다",
        "자주 묻는 질문",
        "비공개 미리보기",
        "보호됨",
        "비밀번호",
        "비밀번호가 올바르지 않습니다",
        "잠금 해제",
    ),
    "pt-BR": (
        "Idioma",
        "Próximos",
        "Nenhum evento próximo",
        "Ver",
        "Sob demanda",
        "Nenhuma gravação disponível",
        "Assistir",
        "Programação",
        "Nenhuma programação disponível",
        "Perguntas frequentes",
        "Prévia privada",
        "Protegido",
        "Senha",
        "Senha incorreta",
        "Desbloquear",
    ),
}


def labels(language):
    return dict(zip(KEYS, STRINGS[language], strict=True))
