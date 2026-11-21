import unicodedata
from library_registration import UTM_FIELDS

_ALLOWED = {"text", "select", "checkbox", "hidden"}


def _clip(s, limit):
    units = 0
    out = []
    for ch in s:
        u = 2 if ord(ch) > 0xFFFF else 1
        if units + u > limit:
            break
        out.append(ch)
        units += u
    return "".join(out)


def _clean(value, limit):
    s = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in value)
    return _clip(s.strip(), limit)


def configured(form):
    if form.get("collect_utm"):
        return True
    return any(f["type"] in _ALLOWED for f in form.get("fields", ()))


def selected(form, answers):
    result = []
    for f in form.get("fields", ()):
        t = f["type"]
        if t not in _ALLOWED:
            continue
        value = answers.get(f["id"])
        if t == "checkbox" and isinstance(value, bool):
            value = "Yes" if value else "No"
        if not isinstance(value, str):
            continue
        value = _clean(value, 250)
        if not value:
            continue
        label = _clean(f["label"], 240)
        ts = "Hidden Field" if t == "hidden" else "Registration Field"
        result.append((f["id"], label, ts, value))
    if form.get("collect_utm"):
        for key in UTM_FIELDS:
            value = answers.get(key)
            if not isinstance(value, str):
                continue
            value = _clean(value, 250)
            if not value:
                continue
            result.append((key, _clean(key, 240), "UTM Field", value))
    return result
