"""Ordered albums within a community group's approved program."""

import json

from flask import abort


SCHEMA = """
CREATE TABLE IF NOT EXISTS audience_group_album(
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL, title TEXT NOT NULL,
 sort TEXT NOT NULL, items TEXT NOT NULL, revision TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS audience_group_album_group ON audience_group_album(group_id,created);
"""


def parse(body, allowed):
    title = body.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
        abort(400, "Enter an album name of up to 120 characters.")
    sort = body.get("sort")
    if sort not in ("manual", "recent", "title"):
        abort(400, "Choose an available album sort order.")
    items = body.get("items")
    if not isinstance(items, list) or len(items) > 100:
        abort(400, "Choose up to 100 group videos.")
    seen = set()
    for pair in items:
        if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(value, str) for value in pair):
            abort(400, "Choose videos from the group program.")
        key = tuple(pair)
        if key not in allowed or key in seen:
            abort(422, "Choose distinct videos from the current group program.")
        seen.add(key)
    return title.strip(), sort, items


def rendered(con, group_id, videos):
    lookup = {(video["kind"], video["slug"]): video for video in videos}
    position = {key: index for index, key in enumerate(lookup)}
    rows = con.execute(
        "SELECT id,title,sort,items,revision FROM audience_group_album WHERE group_id=? ORDER BY created,id",
        (group_id,),
    ).fetchall()
    result = []
    for album, title, sort, items, revision in rows:
        manual = [lookup[key] for key in map(tuple, json.loads(items)) if key in lookup]
        if sort == "recent":
            displayed = sorted(manual, key=lambda video: position[(video["kind"], video["slug"])], reverse=True)
        elif sort == "title":
            displayed = sorted(manual, key=lambda video: (video["title"].casefold(), video["kind"], video["slug"]))
        else:
            displayed = manual
        result.append(dict(id=album, title=title, sort=sort, revision=revision,
                           edit_items=manual, videos=displayed))
    return result
