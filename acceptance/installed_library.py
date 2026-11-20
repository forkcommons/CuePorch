"""Drive the packaged operator's folder workflow with imported records."""

import json
from uuid import uuid4

import requests
from playwright.sync_api import expect


def check_folders(page, base, auth, works):
    page.goto(base + "/operator/library")
    page.wait_for_function("typeof document.querySelector('#load')?.onclick === 'function'")
    assert page.locator("#key").evaluate("input => input.value.length >= 24"), "Operator session unavailable"
    page.get_by_role("button", name="Load library").click()
    expect(page.locator("#result")).to_have_text("Library loaded.", timeout=15000)
    expect(page.locator("#items tr")).to_have_count(3)

    def create(name):
        page.get_by_role("button", name="New folder").click()
        page.get_by_label("Folder name").fill(name)
        page.get_by_role("button", name="Save folder").click()
        expect(page.get_by_role("status")).to_have_text("Folder saved.")

    tree = page.get_by_role("navigation", name="Folders")
    create("Station programs")
    tree.get_by_role("button", name="Station programs", exact=True).click()
    create("November")
    tree.get_by_role("button", name="November", exact=True).click()
    page.get_by_role("button", name="Edit folder").click()
    page.get_by_label("Folder name").fill("November meetings")
    page.get_by_label("Folder color").fill("#794522")
    page.get_by_role("button", name="Save folder").click()
    expect(tree.get_by_role("button", name="November meetings")).to_be_visible()

    root = tree.get_by_role("button", name="Library", exact=True)
    root.click()
    search = page.get_by_label("Search titles")
    search.fill(works[0]["title"])
    expect(page.locator("#items tr")).to_have_count(sum(work["title"] == works[0]["title"] for work in works))
    search.clear()
    columns = page.locator("#items .work-title")
    page.get_by_label("Sort").select_option("title-desc")
    expect(columns).to_have_text(sorted((work["title"] for work in works), reverse=True))
    page.locator("#workBrowse").get_by_label("View").select_option("grid")
    expect(page.locator("#workTable")).to_have_attribute("data-view", "grid")

    boxes = page.locator("#items input[type=checkbox]")
    boxes.nth(0).check()
    boxes.nth(1).check()
    page.get_by_label("Destination").select_option(label="Station programs / November meetings")
    page.get_by_role("button", name="Move selected").click()
    expect(page.get_by_role("status")).to_have_text("Moved 2 works.")
    expect(page.locator("#items tr")).to_have_count(1)

    folders_url = base + "/api/library/folders"
    folders = {item["name"]: item for item in requests.get(folders_url, headers=auth).json()}
    parent = folders["Station programs"]
    child = folders["November meetings"]
    assert child["parent"] == parent["id"] and child["color"] == "#794522"
    assert requests.get(folders_url).status_code == 403
    assert requests.put(
        folders_url + "/" + parent["id"],
        headers=auth,
        json=dict(name=parent["name"], color=parent["color"], parent=child["id"]),
    ).status_code == 409
    placed = requests.get(base + "/api/library", headers=auth).json()
    moved = next(work["id"] for work in placed if work["folder"] == child["id"])
    assert requests.put(
        base + "/api/library/placement",
        headers=auth,
        json=dict(works=[moved, "unknown-work"], folder=None),
    ).status_code == 404

    page.reload()
    page.get_by_role("button", name="Load library").click()
    tree.get_by_role("button", name="November meetings").click()
    expect(page.locator("#items tr")).to_have_count(2)
    page.locator("#privacy").select_option(
        label=page.locator("#items tr td:nth-child(3)").first.inner_text()
    )
    expect(page.locator("#items tr")).to_have_count(1)
    root.click()
    page.locator("#privacy").select_option("")
    page.get_by_role("navigation", name="Folders").get_by_role("button", name="November meetings").click()
    page.get_by_label("Created by").fill("Station")
    expect(page.locator("#items .work-title")).to_have_text(["Station meeting"])
    page.get_by_role("button", name="Star Station meeting", exact=True).click()
    expect(page.get_by_role("status")).to_have_text("Added to Starred.")
    page.get_by_role("button", name="Star folder", exact=True).click()
    expect(page.get_by_role("status")).to_have_text("Added to Starred.")
    page.get_by_label("Created by").clear()
    page.get_by_label("Sort").select_option("added-desc")
    expect(page.locator("#items tr")).to_have_count(2)
    page.get_by_label("Sort").select_option("modified")
    expect(page.locator("#items .work-title")).to_have_text(["Exhibition cut", "Station meeting"])
    page.get_by_label("Sort").select_option("modified-desc")
    expect(page.locator("#items .work-title")).to_have_text(["Station meeting", "Exhibition cut"])

    page.reload()
    page.get_by_role("button", name="Load library").click()
    tree.get_by_role("button", name="Starred", exact=True).click()
    expect(page.locator("#items .work-title")).to_have_text(["Station meeting"])
    expect(tree.get_by_role("button", name="November meetings", exact=True)).to_have_count(2)
    other = {"Authorization": "Bearer fixture-distinct-operator-key"}
    other_works = requests.get(base + "/api/library", headers=other)
    other_works.raise_for_status()
    assert not any(work["starred"] for work in other_works.json())
    other_folders = requests.get(folders_url, headers=other)
    other_folders.raise_for_status()
    assert not any(folder["starred"] for folder in other_folders.json())
    page.get_by_role("button", name="Unstar Station meeting", exact=True).click()
    expect(page.locator("#items tr")).to_have_count(0)
    return {
        "starred_works_and_folders_per_operator": True,
        "creator_filter_and_modification_order": True,
        "nested_edit_and_persistence": True,
        "bulk_move": 2,
        "authorization_and_cycle_check": True,
        "invalid_batch_rejected": True,
    }


def check_bulk_library(page, base, auth, clip):
    def call(method, path, **values):
        response = requests.request(method, base + path, headers=auth, timeout=25, **values)
        response.raise_for_status()
        return response.json()

    def copy(uid, title):
        return call("POST", f"/api/library/work/{uid}/copy", json={"title": title})["id"]

    def showcase(title, visibility):
        route = "/api/showcases"
        item = call("POST", route)
        return call("PUT", route + "/" + item["id"], json=item | {
            "settings": item["settings"] | {"title": title, "visibility": visibility},
        })

    def ratings(uid):
        return call("GET", f"/api/library/work/{uid}/rating")

    with clip.open("rb") as stream:
        public = call("POST", "/api/library/uploads", files={"file": ("Batch public.mp4", stream, "video/mp4")},
                      data={"upload_id": str(uuid4()), "privacy": json.dumps(dict(visibility="public", embed="anywhere",
                                                                                 domains=[], download=False))})["id"]
    withheld = copy(public, "Batch private")
    first = showcase("Station schedule", "public")
    second = showcase("Archive shortlist", "private")
    page.goto(base + "/operator/library")
    page.get_by_role("button", name="Load library").click()
    expect(page.locator("#showcaseSelected")).to_be_disabled()
    expect(page.locator("#ratingSelected")).to_be_disabled()
    page.get_by_role("checkbox", name="Select Batch public", exact=True).check()
    page.get_by_role("checkbox", name="Select Batch private", exact=True).check()
    page.locator("#showcaseSelected").click()
    dialog = page.locator("#workDialog")
    dialog.get_by_role("checkbox", name="Station schedule (public)").check()
    dialog.get_by_role("checkbox", name="Archive shortlist (private)").check()
    save = dialog.get_by_role("button", name="Add videos", exact=True)
    save.click()
    status = page.locator("#workDialogStatus")
    expect(status).to_have_text("Added 4 entries across 2 showcases.")
    save.click()
    expect(status).to_have_text("These videos are already in the selected showcases.")
    for item in (first, second):
        saved = call("GET", "/api/showcases/" + item["id"])
        assert saved["settings"]["videos"] == [public, withheld] and saved["revision"] != item["revision"]
    with page.context.browser.new_context() as guest:
        view = guest.new_page()
        assert view.goto(base + first["watch"]).ok
        expect(view.get_by_role("button", name="Batch public")).to_be_visible()
        expect(view.get_by_role("button", name="Batch private")).to_have_count(0)
        assert view.goto(base + f"/watch/work/{withheld}").status == 404

    dialog.get_by_role("button", name="Close", exact=True).click()
    page.locator("#ratingSelected").click()
    dialog.get_by_label("Rating prompt").fill("Would you recommend this program?")
    dialog.get_by_role("button", name="Apply rating").click()
    expect(status).to_have_text("Saved rating settings for 2 videos.")
    assert ratings(public)["settings"] == ratings(withheld)["settings"]
    with page.context.browser.new_context() as guest:
        view = guest.new_page()
        view.goto(base + f"/watch/work/{public}")
        assert view.locator("video").evaluate("async v => {v.muted=true; await v.play(); v.pause(); v.currentTime=.25; return v.videoWidth}") == 256
        expect(view.get_by_role("button", name="4 stars", exact=True)).to_be_visible()
        view.get_by_role("button", name="4 stars", exact=True).click()
        expect(view.get_by_text("Rating recorded.")).to_be_visible()

    batch = [public, withheld, *(copy(public, f"Batch item {index}") for index in range(48))]
    path = "/api/library/ratings"
    payload = dict(works=batch, mode="thumbs", prompt="Was the program useful?", start=0)
    vote = ratings(public)["report"]
    assert sum(record["responses"] for record in vote) == 1
    assert requests.put(base + path, json=payload, timeout=15).status_code == 403
    assert requests.put(base + path, headers=auth, json=payload | {"works": batch + ["missing"]}, timeout=15).status_code == 422
    assert requests.put(base + path, headers=auth, json=payload | {"works": [public, "missing"]}, timeout=15).status_code == 404
    assert requests.put(base + path, headers=auth, json=payload | {"start": 100000}, timeout=15).status_code == 422
    assert ratings(public)["settings"]["mode"] == "stars" and ratings(batch[-1])["settings"]["mode"] == "none"
    assert call("PUT", path, json=payload) == {"selected": 50, "updated": 50}
    assert call("PUT", path, json=payload) == {"selected": 50, "updated": 0}
    assert ratings(batch[-1])["settings"]["mode"] == "thumbs" and ratings(public)["report"] == vote

    showcase_path = "/api/showcases/selected-videos"
    payload = {"works": batch, "showcases": [first["id"], "missing"]}
    assert requests.post(base + showcase_path, headers=auth, json=payload, timeout=15).status_code == 404
    assert call("GET", f"/api/showcases/{first['id']}")["settings"]["videos"] == [public, withheld]
    payload["showcases"] = [first["id"], second["id"]]
    assert call("POST", showcase_path, json=payload) == {"showcases": 2, "added": 96}
    assert call("POST", showcase_path, json=payload) == {"showcases": 2, "added": 0}
    assert call("GET", f"/api/showcases/{first['id']}")["settings"]["videos"] == batch
    return dict(multi_showcase_and_rating_actions=True, public_showcase_respects_source_privacy=True,
                selected_rating_records_a_real_vote=True, retained_votes_after_rating_change=True,
                validated_single_transaction_and_retry=50)
