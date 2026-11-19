"""Copy only the retained slide files referenced by an institutional database."""

import re
from shutil import copy2


def has_table(database, name):
    return database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def copy_decks(database, source, destination, verified=None):
    if not has_table(database, "live_slide_deck"):
        return []
    records = []
    rows = database.execute("SELECT event,id,title,pages,format FROM live_slide_deck ORDER BY event,id")
    for event, identity, title, pages, kind in rows:
        if not (isinstance(identity, str) and re.fullmatch(r"[a-f0-9]{32}", identity)
                and kind in ("pdf", "pptx") and type(pages) is int and 1 <= pages <= 500):
            raise ValueError("Invalid slide deck record in the institutional database")
        folder = destination / "live-slide-decks" / identity
        folder.mkdir(parents=True)
        prefix = folder.relative_to(destination)
        original = prefix / f"source.{kind}"
        slides = [prefix / f"{index}.png" for index in range(pages)]
        for relative in [original, *slides]:
            file = source / relative
            if not file.is_file() or not file.resolve().is_relative_to(source.resolve()):
                raise ValueError(f"Missing slide deck asset {relative.as_posix()}")
            if verified is not None and relative.as_posix() not in verified:
                raise ValueError(f"Slide deck asset is missing from the snapshot manifest: {relative.as_posix()}")
            copy2(file, destination / relative)
        records.append(dict(event=event, id=identity, title=title, pages=pages, format=kind,
                            original=original.as_posix(), slides=[file.as_posix() for file in slides]))
    return records
