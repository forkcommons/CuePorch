"""Drive the packaged operator's folder workflow with imported records."""

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
    page.get_by_label("View").select_option("grid")
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
    page.get_by_label("Source privacy").select_option(
        label=page.locator("#items tr td:nth-child(3)").first.inner_text()
    )
    expect(page.locator("#items tr")).to_have_count(1)
    root.click()
    page.get_by_label("Source privacy").select_option("")
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
