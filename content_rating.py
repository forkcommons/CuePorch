"""Shared ratings and disclosures for the retained library and live events."""

from urllib.parse import urlencode

from flask import abort, make_response, render_template, request


DESCRIPTORS = {"language": "Profanity", "drugs": "Drug or alcohol use", "violence": "Violence", "nudity": "Nudity"}
RATINGS = {"unrated", "all", "mature"}
DEFAULT = dict(rating="unrated", content=[], advertisement=False, realistic_ai=False)
PREFERENCE_DEFAULT = dict(mode="all", excluded=[])
PREFERENCE_CHOICES = {"unrated": "Unrated videos", **DESCRIPTORS}


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


def validate_preference(body):
    mode, excluded = body.get("mode"), body.get("excluded", [])
    if (not isinstance(mode, str) or mode not in {"all", "hide_mature", "custom"}
            or not isinstance(excluded, list) or len(excluded) > len(PREFERENCE_CHOICES)
            or any(not isinstance(key, str) or key not in PREFERENCE_CHOICES for key in excluded)):
        abort(400, "Choose a viewing preference.")
    return dict(mode=mode, excluded=list(dict.fromkeys(excluded)) if mode == "custom" else [])


def filtered(preference, values):
    if values.get("kind") == "channel":
        return False
    rating = values.get("rating", "unrated")
    if preference["mode"] == "hide_mature":
        return rating == "mature"
    if preference["mode"] == "custom":
        excluded = preference["excluded"]
        return (rating == "unrated" and "unrated" in excluded
                or rating == "mature" and any(key in excluded for key in values.get("content", [])))
    return False


def screening_notice(title, preference, values):
    if request.args.get("show_content") == "1" or not filtered(preference, values):
        return None
    query = request.args.to_dict(flat=False)
    query["show_content"] = ["1"]
    response = make_response(render_template("viewer_content_notice.html", title=title,
                                             continue_url=request.path + "?" + urlencode(query, doseq=True)))
    response.cache_control.private = True
    response.cache_control.no_store = True
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response
