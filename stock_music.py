"""Scott Buckley CC-BY-4.0 licensed production music catalogue."""

CREDIT = "Scott Buckley"
LICENSE = "CC-BY-4.0"
PAGE_SIZE = 20

_URL_BASE = "https://www.scottbuckley.com.au/library/wp-content/uploads/"
_SOURCE_BASE = "https://www.scottbuckley.com.au/library/"

_TRACK_DATA = (
    (
        "signal-to-noise",
        "Signal to Noise",
        "Documentary",
        "Mysterious",
        "Neoclassical",
        "Instrumental",
        "2020/04/sb_signaltonoise.mp3",
    ),
    (
        "undertow",
        "Undertow",
        "Documentary",
        "Somber",
        "Ambient",
        "Instrumental",
        "2019/12/sb_undertow.mp3",
    ),
    (
        "simplicity",
        "Simplicity",
        "Background",
        "Calm",
        "Neoclassical",
        "Instrumental",
        "2024/03/Simplicity.mp3",
    ),
    (
        "glow",
        "Glow",
        "Background",
        "Warm",
        "Folk",
        "Vocals",
        "2022/02/Glow.mp3",
    ),
    (
        "resolutions",
        "Resolutions",
        "Highlights",
        "Hopeful",
        "Cinematic",
        "Instrumental",
        "2022/01/Resolutions.mp3",
    ),
    (
        "affirmations",
        "Affirmations",
        "Background",
        "Calm",
        "Ambient",
        "Instrumental",
        "2021/06/sb_affirmations.mp3",
    ),
    (
        "convergence",
        "Convergence",
        "Highlights",
        "Driving",
        "Electronic",
        "Instrumental",
        "2026/04/Convergence.mp3",
    ),
)


def _make_record(slug, title, category, mood, genre, music_type, file_path):
    return {
        "id": slug,
        "title": title,
        "kind": "audio",
        "type": music_type,
        "url": _URL_BASE + file_path,
        "size": None,
        "sha1": None,
        "extension": "mp3",
        "rights": {
            "category": category,
            "mood": mood,
            "genre": genre,
            "credit": CREDIT,
            "source_url": _SOURCE_BASE + slug + "/",
            "license": LICENSE,
        },
    }


RECORDS = tuple(_make_record(*row) for row in _TRACK_DATA)

CATEGORIES = tuple(sorted({rec["rights"]["category"] for rec in RECORDS}))
MOODS = tuple(sorted({rec["rights"]["mood"] for rec in RECORDS}))
GENRES = tuple(sorted({rec["rights"]["genre"] for rec in RECORDS}))
TYPES = tuple(sorted({rec["type"] for rec in RECORDS}))


def filters():
    """Return the normalized, sorted filter options for the catalogue."""
    return {
        "categories": list(CATEGORIES),
        "moods": list(MOODS),
        "genres": list(GENRES),
        "types": list(TYPES),
    }


def search(category=None, mood=None, genre=None, music_type=None, query=None, offset=0):
    """Search the catalogue.

    Filters must be exact matches against known options; empty/None values are
    ignored. Returns a tuple of (records page, next_offset or None).
    """
    selections = (
        ("category", category, CATEGORIES, "rights.category"),
        ("mood", mood, MOODS, "rights.mood"),
        ("genre", genre, GENRES, "rights.genre"),
        ("music_type", music_type, TYPES, "type"),
    )

    normalized = {}
    for name, value, allowed, _field in selections:
        if value is None or value == "":
            normalized[name] = None
        elif not isinstance(value, str) or value not in allowed:
            raise ValueError("Invalid %s filter: %r" % (name, value))
        else:
            normalized[name] = value

    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Invalid offset: %r" % (offset,))

    if query is None:
        needle = None
    elif not isinstance(query, str):
        raise ValueError("Invalid query: %r" % (query,))
    else:
        stripped = query.strip()
        needle = stripped.casefold() if stripped else None

    results = []
    for record in RECORDS:
        rights = record["rights"]

        if normalized["category"] is not None and rights["category"] != normalized["category"]:
            continue
        if normalized["mood"] is not None and rights["mood"] != normalized["mood"]:
            continue
        if normalized["genre"] is not None and rights["genre"] != normalized["genre"]:
            continue
        if normalized["music_type"] is not None and record["type"] != normalized["music_type"]:
            continue

        if needle is not None:
            haystacks = (
                record["title"],
                rights["credit"],
                rights["category"],
                rights["mood"],
                rights["genre"],
                record["type"],
            )
            if not any(needle in value.casefold() for value in haystacks):
                continue

        results.append(record)

    results.sort(key=lambda rec: (rec["title"].casefold(), rec["id"]))

    page = results[offset:offset + PAGE_SIZE]
    next_offset = offset + PAGE_SIZE if offset + PAGE_SIZE < len(results) else None
    return page, next_offset
