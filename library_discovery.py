"""Public browsing and operator-managed Staff Picks."""

import json
import uuid

from flask import abort, make_response, render_template, request, send_file, url_for

import library_details
import library_showcases
import content_rating
import team_branding
from library_privacy import playback
from library_thumbnail import selected_thumbnail, thumbnail_type

SCHEMA = """
CREATE TABLE IF NOT EXISTS public_staff_picks(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision TEXT NOT NULL, items TEXT NOT NULL);
"""
PAGE_SIZE = 24


def listing(con, read_works, title_for, permits, item=None):
    works = {work["id"]: work for work in read_works() if item is None or item[1] == work["id"]}
    private = {row[0] for row in con.execute(
        """SELECT item FROM library_deleted_item WHERE kind='work'
           UNION SELECT work FROM case_hold WHERE active=1"""
    )}
    handoffs = dict(con.execute("SELECT work,state FROM library_peer_handoff"))
    access = {row[0]: json.loads(row[1]) for row in con.execute("SELECT work,settings FROM library_privacy")}
    routes = con.execute("SELECT kind,slug,description,public_record FROM route").fetchall()
    protected = {row["slug"] for row in routes if row["public_record"]}
    entries = {}

    def retain(kind, slug, title, work):
        details = library_details.details_for(con, work) if work else {}
        entries[(kind, slug)] = dict(
            kind=kind, slug=slug, title=title, description=details.get("description", ""),
            categories=details.get("categories", []), tags=details.get("tags", []),
            rating=details.get("rating", "unrated"), content=details.get("content", []),
            allow_viewer_collections=details.get("allow_viewer_collections", True),
            added=work.get("library_added_at", "") if work else "",
            image=selected_thumbnail(con, work) if work else None,
        )

    for row in routes:
        kind, slug = row["kind"], row["slug"]
        if (item and item != (kind, slug)) or slug in private or slug in handoffs:
            continue
        if slug in access and slug not in protected:
            continue
        work = works.get(slug) if kind == "work" else None
        retain(kind, slug, row["description"] or (title_for(con, work) if work else slug), work)

    for slug, settings in access.items():
        work = works.get(slug)
        if ((item and item != ("work", slug)) or slug in private or slug in protected
                or handoffs.get(slug) == "pending" or not work
                or settings["visibility"] != "public" or not playback(work) or not permits(work, settings)):
            continue
        retain("work", slug, title_for(con, work), work)
    return entries


def picks(con):
    saved = con.execute("SELECT revision,items FROM public_staff_picks WHERE singleton=1").fetchone()
    return (saved["revision"], [tuple(key) for key in json.loads(saved["items"])]) if saved else ("", [])


def entry_payload(entry):
    kind, slug = entry["kind"], entry["slug"]
    return {
        "kind": kind, "slug": slug, "title": entry["title"], "categories": entry["categories"],
        "watch": entry.get("watch") or url_for("watch", kind=kind, slug=slug),
        "cover": entry.get("cover") or (
            url_for("discovery_cover", kind=kind, slug=slug, digest=entry["image"])
            if entry["image"] else None
        ),
    }


def ordered(entries):
    # Stable ordering for older records that have no library-added timestamp.
    by_title = sorted(entries, key=lambda item: (item["title"].casefold(), item["kind"], item["slug"]))
    return sorted(by_title, key=lambda item: item["added"] or "", reverse=True)


def matches(entry, terms):
    text = " ".join((entry["title"], entry["description"], *entry["categories"], *entry["tags"])).casefold()
    return all(term in text for term in terms)


def no_store(body):
    response = make_response(body)
    response.cache_control.no_store = True
    return response


def register(app, tx, staff, public_listing, root, institution, viewing_preferences):
    @app.get("/")
    @app.get("/watch")
    @app.get("/watch/<any(picks,categories,channels):section>")
    def catalog(section="explore"):
        query = request.args.get("q", "").strip()[:120]
        category = request.args.get("category", "").strip()[:120]
        with tx() as con:
            preference = viewing_preferences(con)
            entries = {key: entry for key, entry in public_listing(con).items()
                       if not content_rating.filtered(preference, entry)}
            _, keys = picks(con)
            if section == "channels":
                for row in con.execute("SELECT * FROM video_showcase ORDER BY created DESC"):
                    settings = library_showcases.state(row)
                    if not library_showcases.channel_visible(settings):
                        continue
                    category = settings["channel_category"]
                    entries[("native-channel", row["id"])] = dict(
                        kind="channel",
                        slug=row["slug"],
                        title=settings["title"],
                        description=settings["description"],
                        categories=[category] if category else [],
                        tags=[],
                        added=row["created"],
                        image=None,
                        watch=url_for("showcase_channel", slug=row["slug"]),
                        cover=(
                            url_for(
                                "showcase_channel_cover",
                                slug=row["slug"],
                                digest=settings["channel_cover"],
                            )
                            if settings["channel_cover"] else None
                        ),
                    )
            brand = team_branding.read(con, institution)
        featured = [entries[key] for key in keys if key in entries]
        videos = ordered(entry for entry in entries.values() if entry["kind"] != "channel")
        labels = {}
        label_source = (
            [entry for entry in entries.values() if entry["kind"] == "channel"]
            if section == "channels" else videos
        )
        for entry in label_source:
            for label in entry["categories"]:
                labels.setdefault(label.casefold(), label)
        categories = sorted(labels.values(), key=str.casefold)
        if section == "picks":
            videos = featured
        elif section == "channels":
            videos = ordered(entry for entry in entries.values() if entry["kind"] == "channel")
        if query:
            terms = query.casefold().split()
            videos = [entry for entry in videos if matches(entry, terms)]
        if category:
            category_key = category.casefold()
            videos = [entry for entry in videos if category_key in (name.casefold() for name in entry["categories"])]
        count = (len(videos) + PAGE_SIZE - 1) // PAGE_SIZE
        page = max(1, min(count or 1, request.args.get("page", 1, type=int)))
        start = (page - 1) * PAGE_SIZE
        show_spotlight = section == "explore" and not query and not category and page == 1

        def page_link(number):
            return url_for("catalog", section=None if section == "explore" else section,
                           page=number, q=query or None, category=category or None)

        return no_store(render_template(
            "catalog.html", brand=brand, section=section, q=query, category=category, categories=categories,
            rows=[entry_payload(entry) for entry in videos[start:start + PAGE_SIZE]],
            featured=[entry_payload(entry) for entry in featured[:6]] if show_spotlight else [],
            previous=page_link(page - 1) if page > 1 else None, next_page=page_link(page + 1) if page < count else None,
        ))

    @app.get("/watch-cover/<kind>/<slug>/<digest>")
    def discovery_cover(kind, slug, digest):
        if kind != "work":
            abort(404)
        key = (kind, slug)
        with tx() as con:
            entry = public_listing(con, key).get(key)
        if not entry or not entry["image"] or digest != entry["image"]:
            abort(404)
        path = root / "objects" / entry["image"]
        if not path.is_file() or not (media_type := thumbnail_type(path)):
            abort(404)
        response = send_file(path, mimetype=media_type, conditional=False)
        response.cache_control.no_store = True
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/operator/staff-picks")
    def operator_picks():
        return render_template("staff_picks.html")

    @app.route("/api/watch/staff-picks", methods=("GET", "PUT"))
    def staff_picks():
        staff()
        with tx() as con:
            revision, keys = picks(con)
            entries = {key: entry for key, entry in public_listing(con).items()
                       if entry["kind"] != "channel"}
            if request.method == "PUT":
                body = request.get_json()
                if not isinstance(body, dict) or not isinstance(body.get("items"), list):
                    abort(400, "Choose public videos for Staff Picks.")
                if body.get("revision") != revision:
                    abort(409, "Staff Picks changed. Reload before saving.")
                proposed = []
                for item in body["items"]:
                    if (not isinstance(item, list) or len(item) != 2
                            or any(not isinstance(value, str) for value in item)):
                        abort(400, "Choose public videos for Staff Picks.")
                    proposed.append(tuple(item))
                if len(set(proposed)) != len(proposed) or set(proposed) - (set(entries) | set(keys)):
                    abort(422, "Select each eligible video once.")
                if proposed != keys:
                    revision, keys = uuid.uuid4().hex, proposed
                    con.execute("INSERT OR REPLACE INTO public_staff_picks VALUES(1,?,?)", (revision, json.dumps(keys)))
            return no_store({
                "revision": revision, "items": [list(key) for key in keys],
                "videos": [entry_payload(entry) for entry in ordered(entries.values())],
            })
