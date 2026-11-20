"""Shared ratings and disclosures for the retained library and live events."""

from flask import abort


DESCRIPTORS = {"language": "Profanity", "drugs": "Drug or alcohol use", "violence": "Violence", "nudity": "Nudity"}
RATINGS = {"unrated", "all", "mature"}
DEFAULT = dict(rating="unrated", content=[], advertisement=False, realistic_ai=False)


def validate(body, status=400):
    if not isinstance(body, dict):
        abort(status, "Choose content ratings and disclosures.")
    values = DEFAULT | body
    rating, content = values["rating"], values["content"]
    if not isinstance(rating, str) or rating not in RATINGS:
        abort(status, "Choose a content rating.")
    if (not isinstance(content, list) or len(content) > 100
            or any(not isinstance(value, str) or value.strip() not in DESCRIPTORS for value in content)
            or (content and rating != "mature")):
        abort(status, "Content descriptors are available for mature videos and events.")
    if any(type(values[field]) is not bool for field in ("realistic_ai", "advertisement")):
        abort(status, "Choose a value for each disclosure.")
    return dict(rating=rating, content=list(dict.fromkeys(value.strip() for value in content)),
                advertisement=values["advertisement"], realistic_ai=values["realistic_ai"])


def viewer_labels(values):
    labels = []
    if values.get("rating") == "mature":
        labels.append("Mature audiences")
        labels.extend(DESCRIPTORS[key] for key in values.get("content", []) if key in DESCRIPTORS)
    if values.get("advertisement"):
        labels.append("Contains advertising")
    if values.get("realistic_ai"):
        labels.append("Contains realistic AI-generated material")
    return labels
