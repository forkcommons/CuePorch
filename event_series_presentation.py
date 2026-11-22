import re
from flask import abort
import library_appearance
from library_interactivity import image_id, text_field, web_link

BLOCKS = ("navigation", "hero", "upcoming", "on_demand", "agenda", "faq")
DEFAULTS = dict(
    blocks=["hero", "upcoming", "on_demand"],
    accent="#2b7e7d",
    dark=False,
    logo=None,
    hero_image=None,
    navigation=[],
    date_label="",
    timezone_label="",
    faq=[],
)


def validate(con, raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        abort(422, "Unknown presentation field.")

    values = DEFAULTS | raw

    blocks = values["blocks"]
    if (
        not isinstance(blocks, list)
        or any(not isinstance(b, str) for b in blocks)
        or len(set(blocks)) != len(blocks)
        or any(b not in BLOCKS for b in blocks)
    ):
        abort(422, "Choose presentation blocks.")

    accent = values["accent"]
    if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        abort(422, "Enter an accent color as #RRGGBB.")
    accent = accent.lower()

    dark = values["dark"]
    if not isinstance(dark, bool):
        abort(422, "Choose light or dark mode.")

    logo = image_id(con, values["logo"])
    hero_image = image_id(con, values["hero_image"])

    navigation = []
    nav = values["navigation"]
    if not isinstance(nav, list):
        abort(422, "Enter navigation items.")
    for item in nav:
        if not isinstance(item, dict) or set(item) != {"label", "href"}:
            abort(422, "Enter navigation items with a label and link.")
        label = text_field(item["label"], "a navigation label", 100, required=True)
        href = web_link(item["href"])
        if not href:
            abort(422, "Enter a navigation link.")
        navigation.append({"label": label, "href": href})

    date_label = text_field(values["date_label"], "a date label", 100)
    timezone_label = text_field(values["timezone_label"], "a timezone label", 100)

    faq = []
    faq_raw = values["faq"]
    if not isinstance(faq_raw, list) or len(faq_raw) > 20:
        abort(422, "Enter up to 20 FAQ entries.")
    for item in faq_raw:
        if not isinstance(item, dict) or set(item) != {"question", "answer"}:
            abort(422, "Enter FAQ entries with a question and answer.")
        question = text_field(item["question"], "a question", 200, required=True)
        answer = text_field(item["answer"], "an answer", 1500, required=True)
        faq.append({"question": question, "answer": answer})

    return {
        "blocks": list(blocks),
        "accent": accent,
        "dark": dark,
        "logo": logo,
        "hero_image": hero_image,
        "navigation": navigation,
        "date_label": date_label,
        "timezone_label": timezone_label,
        "faq": faq,
    }


def render(con, stored):
    out = DEFAULTS | (stored or {})
    out["logo_src"] = library_appearance.brand_image(con, out["logo"])
    out["hero_src"] = library_appearance.brand_image(con, out["hero_image"])
    return out
