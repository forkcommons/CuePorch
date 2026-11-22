"""Curate public Watch entries and confirm privacy changes withdraw their covers."""

from base64 import b64encode
import json
import uuid

from playwright.sync_api import expect
import requests


def check_discovery(page, browser, base, auth, sample):
    def send(method, path, **options):
        response = requests.request(method, base + path, headers=auth, timeout=30, **options)
        response.raise_for_status()
        return response.json() if response.content else None

    def public(path):
        return requests.get(base + path, timeout=15)

    public_settings = dict(visibility="public", embed="anywhere", domains=[], download=False)
    private_settings = dict(visibility="private", embed="disabled", domains=[], download=False)

    def upload(title, settings, category, tag):
        with sample.open("rb") as media:
            uid = send("POST", "/api/library/uploads", files={"file": (title + ".mp4", media, "video/mp4")},
                       data={"upload_id": str(uuid.uuid4()), "privacy": json.dumps(settings)})["id"]
        endpoint = f"/api/library/work/{uid}/details"
        details = send("GET", endpoint)
        send("PUT", endpoint, json=details | dict(categories=[category], tags=[tag]))
        return uid

    first_title = "Cyanotype Workshop"
    other_title = "Community Screening"
    private_title = "Unreleased Assembly"
    first = upload(first_title, public_settings, "Art and film", "analogue")
    other = upload(other_title, public_settings, "Civic life", "meeting")
    hidden = upload(private_title, private_settings, "Confidential category", "concealed")
    first_watch = f"/watch/work/{first}"
    other_watch = f"/watch/work/{other}"
    send("POST", f"/api/library/work/{first}/thumbnail", json={
        "seconds": 0, "media": send("GET", f"/api/library/work/{first}")["files"][0]["sha256"],
    })
    send("PUT", "/api/routes/channel/partner-program", json={
        "revision": "one", "destination": "https://station.example/api/public/embed-player/channel/partner-program",
        "description": "Partner programs", "public_authorized": True,
    })

    picks_api = "/api/watch/staff-picks"
    assert public(picks_api).status_code == 403
    initial = send("GET", picks_api)
    assert not initial["items"]
    assert requests.put(base + picks_api, headers=auth, timeout=15,
                        json=dict(revision=initial["revision"], items=[["work", hidden]])).status_code == 422
    page.goto(base + "/operator/library")
    page.get_by_role("link", name="Staff Picks", exact=True).click()
    page.locator("#key").fill(auth["Authorization"].removeprefix("Bearer "))
    page.get_by_role("button", name="Load Staff Picks").click()
    expect(page.get_by_role("button", name=f"Add {first_title}")).to_be_visible()
    page.get_by_label("Find a public video").fill("Art and film")
    expect(page.get_by_role("button", name=f"Add {other_title}")).to_have_count(0)
    page.get_by_role("button", name=f"Add {first_title}").click()
    page.get_by_label("Find a public video").fill("")
    page.get_by_role("button", name=f"Add {other_title}").click()
    page.get_by_role("button", name=f"Move up {other_title}").click()
    page.get_by_role("button", name="Save Staff Picks").click()
    expect(page.locator("#picksStatus")).to_have_text("Staff Picks saved.")
    assert requests.put(base + picks_api, headers=auth, timeout=15,
                        json=dict(revision=initial["revision"], items=[])).status_code == 409

    context = browser.new_context(viewport=dict(width=420, height=760))
    viewer = context.new_page()
    try:
        viewer.goto(base + "/")
        assert viewer.locator("body").evaluate("body => body.scrollWidth <= innerWidth")
        assert "no-store" in public("/").headers["Cache-Control"]
        featured = viewer.get_by_role("region", name="Staff Picks")
        expect(featured.get_by_role("link", name=other_title)).to_be_visible()
        featured.get_by_role("button", name="Next pick").click()
        expect(featured.get_by_role("link", name=first_title)).to_be_visible()
        image = featured.locator("article:not([hidden]) img")
        expect(image).to_be_visible()
        cover = image.get_attribute("src")
        response = public(cover)
        assert response.ok and response.headers["Content-Type"].startswith("image/")
        assert image.evaluate("image => image.decode().then(() => image.naturalWidth)") > 0
        featured.get_by_role("link", name=first_title).click()
        assert viewer.url == base + first_watch
        width = viewer.locator("video").evaluate("async video => {video.muted = true; await video.play(); return video.videoWidth;}")
        assert width > 0

        viewer.goto(base + "/watch/categories")
        categories = viewer.get_by_role("navigation", name="Video categories")
        expect(categories.get_by_role("link", name="Confidential category")).to_have_count(0)
        categories.get_by_role("link", name="Art and film").click()
        expect(viewer.get_by_role("link", name=other_title)).to_have_count(0)
        viewer.get_by_label("Search public videos").fill("ANALOGUE")
        viewer.get_by_role("button", name="Search", exact=True).click()
        expect(viewer.get_by_role("link", name=first_title)).to_have_attribute("href", first_watch)
        category_url = base + "/watch/categories?category=Art+and+film"
        viewer.get_by_role("link", name="Sign in to follow this category").click()
        signup = viewer.locator("#viewerSignUp")
        signup.get_by_label("Display name").fill("Feed viewer")
        signup.get_by_label("Email").fill("feed-viewer@example.invalid")
        signup.get_by_label("Password").fill("Category feed fixture 2026!")
        signup.get_by_role("button", name="Create account").click()
        expect(viewer.locator("#viewerDetails")).to_be_visible()

        group_path = "/groups/arts-screenings"
        group_api = base + "/api/viewer" + group_path
        group_name = "Arts screenings"
        group_marker = {"X-CuePorch-Viewer": "1"}
        viewer.get_by_role("link", name="Groups", exact=True).click()
        viewer.get_by_label("Name", exact=True).fill(group_name)
        viewer.get_by_label("Link name").fill("arts-screenings")
        viewer.get_by_role("button", name="Create group").click()
        expect(viewer).to_have_url(base + group_path)
        other_details_url = f"/api/library/work/{other}/details"
        other_details = send("GET", other_details_url)
        picker = viewer.get_by_label("Video to add")
        expect(picker.locator(f'option[value="work/{other}"]')).to_have_count(1)
        send("PUT", other_details_url, json=other_details | {"allow_viewer_collections": False})
        viewer.reload()
        expect(picker.locator(f'option[value="work/{other}"]')).to_have_count(0)
        assert viewer.request.put(group_api + "/videos/work/" + other, headers=group_marker, data={}).status == 403
        assert public(other_watch).status_code == 200
        send("PUT", other_details_url, json=other_details | {"allow_viewer_collections": True})
        viewer.reload()
        expect(picker.locator(f'option[value="work/{other}"]')).to_have_count(1)
        viewer.get_by_label("Video to add").select_option("work/" + first)
        viewer.get_by_role("button", name="Add video", exact=True).click()
        expect(viewer.get_by_role("link", name=first_title)).to_be_visible()
        assert viewer.request.put(group_api + "/videos/work/" + hidden, headers=group_marker, data={}).status == 404
        assert group_name in public("/watch/groups").text

        with browser.new_context() as member:
            join = member.request.post(base + "/api/viewer/signup", headers=group_marker, data={
                "name": "Group member", "email": "group-member@example.invalid",
                "password": "Group member fixture password!",
            })
            assert join.status == 201, join.text()
            member_page = member.new_page()
            member_page.goto(base + group_path)
            member_page.get_by_role("button", name="Join group", exact=True).click()
            expect(member_page.get_by_role("button", name="Leave group", exact=True)).to_be_visible()
            other_group_video = group_api + "/videos/work/" + other
            member_page.get_by_label("Video to add").select_option("work/" + other)
            member_page.get_by_role("button", name="Submit video", exact=True).click()
            expect(member_page.get_by_role("link", name=other_title, exact=True)).to_be_visible()
            viewer.reload()
            viewer.locator(f"[data-remove-group-video='work/{other}']").click()
            expect(viewer.get_by_role("link", name=other_title, exact=True)).to_have_count(0)
            viewer.get_by_label("Allow members to submit videos").uncheck()
            with viewer.expect_navigation():
                viewer.get_by_role("button", name="Save group", exact=True).click()
            expect(viewer.get_by_label("Allow members to submit videos")).not_to_be_checked()
            assert member.request.put(other_group_video, headers=group_marker, data={}).status == 403
            viewer.reload()
            membership = viewer.get_by_text("Manage group membership", exact=True)
            moderation = viewer.locator("[data-group-moderator]")
            membership.click()
            viewer.get_by_role("button", name="Grant moderator to Group member", exact=True).click()
            expect(moderation).to_have_attribute("data-enabled", "false")
            member_page.reload()
            member_page.get_by_label("Video to add").select_option("work/" + other)
            member_page.get_by_role("button", name="Add video", exact=True).click()
            expect(member_page.get_by_role("link", name=other_title, exact=True)).to_be_visible()
            expect(member_page.locator("#groupSettings")).to_have_count(0)
            assert member.request.put(group_api + "/videos/work/" + hidden, headers=group_marker, data={}).status == 404
            assert member.request.post(group_api + "/ownership", headers=group_marker, data={}).status == 404
            membership.click()
            viewer.get_by_role("button", name="Revoke moderator for Group member", exact=True).click()
            expect(moderation).to_have_attribute("data-enabled", "true")
            member_page.reload()
            expect(member_page.get_by_label("Video to add")).to_have_count(0)
            assert member.request.delete(other_group_video, headers=group_marker, data={}).status == 404
            viewer.locator(f"[data-remove-group-video='work/{other}']").click()
            expect(viewer.get_by_role("link", name=other_title, exact=True)).to_have_count(0)
            viewer.get_by_label("Allow members to submit videos").check()
            viewer.get_by_label("Require review for member submissions").check()
            with viewer.expect_navigation():
                viewer.get_by_role("button", name="Save group", exact=True).click()
            member_page.reload()
            member_page.get_by_label("Video to add").select_option("work/" + other)
            member_page.get_by_role("button", name="Submit video", exact=True).click()
            expect(member_page.get_by_role("heading", name="Pending submissions", exact=True)).to_be_visible()
            expect(member_page.locator("#groupVideoList").get_by_role("link", name=other_title,
                                                                      exact=True)).to_have_count(0)
            assert "Pending submissions" not in public(group_path).text
            viewer.reload()
            expect(viewer.get_by_role("heading", name="Pending submissions", exact=True)).to_be_visible()
            viewer.get_by_role("button", name="Approve", exact=True).click()
            expect(viewer.get_by_role("link", name=other_title, exact=True)).to_be_visible()
            viewer.locator(f"[data-remove-group-video='work/{other}']").click()
            expect(viewer.get_by_role("link", name=other_title, exact=True)).to_have_count(0)
            assert member.request.put(other_group_video, headers=group_marker, data={}).status == 202
            member_page.reload()
            member_page.get_by_role("button", name="Withdraw", exact=True).click()
            expect(member_page.get_by_role("heading", name="Pending submissions", exact=True)).to_have_count(0)
            assert member.request.put(other_group_video, headers=group_marker, data={}).status == 202
            viewer.reload()
            viewer.get_by_label("Require review for member submissions").uncheck()
            with viewer.expect_navigation():
                viewer.get_by_role("button", name="Save group", exact=True).click()
            expect(viewer.get_by_role("link", name=other_title, exact=True)).to_be_visible()
            expect(viewer.get_by_role("heading", name="Pending submissions", exact=True)).to_have_count(0)

            create_album = viewer.locator("[data-album-form][data-album-id='']")
            create_album.get_by_label("Album name").fill("Opening night")
            for work in (other, first):
                create_album.locator("[data-album-choices]").select_option("work/" + work)
                create_album.get_by_role("button", name="Add video to album", exact=True).click()
            create_album.locator(f"[data-album-video='work/{first}'] [data-album-move='up']").click()
            create_album.get_by_role("button", name="Create album", exact=True).click()
            playlist = viewer.locator("[data-album-playlist] a")
            expect(playlist).to_have_text([first_title, other_title])
            member_page.reload()
            member_playlist = member_page.locator("[data-album-playlist] a")
            expect(member_playlist).to_have_text([first_title, other_title])
            expect(member_page.locator("[data-album-form]")).to_have_count(0)
            member_playlist.first.click()
            assert member_page.locator("video").evaluate(
                "async video => {video.muted = true; await video.play(); return video.videoWidth;}"
            ) > 0
            member_page.goto(base + group_path)

            viewer.get_by_text("Edit album", exact=True).click()
            album_editor = viewer.locator("[data-album-form]:not([data-album-id=''])")
            album_api = group_api + "/albums/" + album_editor.get_attribute("data-album-id")
            album_revision = album_editor.locator("[name=revision]").input_value()
            assert member.request.delete(album_api, headers=group_marker, data={"revision": album_revision}).status == 404
            album_editor.get_by_label("Sort videos").select_option("title")
            album_editor.get_by_role("button", name="Save album", exact=True).click()
            expect(playlist).to_have_text([other_title, first_title])
            assert viewer.request.delete(album_api, headers=group_marker, data={"revision": album_revision}).status == 409
            viewer.get_by_text("Edit album", exact=True).click()
            album_editor.get_by_role("button", name="Delete album", exact=True).click()
            expect(viewer.get_by_role("heading", name="Opening night", exact=True)).to_have_count(0)
            member_page.reload()

            before = viewer.locator("#groupSettings input[name=revision]").input_value()
            viewer.get_by_label("Group access").select_option("members")
            viewer.get_by_role("button", name="Save group", exact=True).click()
            expect(viewer.locator("#groupAccess")).to_have_attribute("data-access", "members")
            assert viewer.request.put(group_api, headers=group_marker, data={"revision": before}).status == 409
            assert public(group_path).status_code == 404
            assert group_name not in public("/watch/groups").text
            member_page.reload()
            member_page.get_by_role("link", name=first_title, exact=True).click()
            assert member_page.url == base + first_watch
            assert member_page.locator("video").evaluate(
                "async video => {video.muted = true; await video.play(); video.pause(); return video.videoWidth}"
            ) > 0
            member_page.goto(base + group_path)
            member_page.get_by_role("button", name="Leave group", exact=True).click()
            expect(member_page.get_by_role("heading", name="Not Found")).to_be_visible()

        viewer.get_by_text("Delete this group", exact=True).click()
        viewer.get_by_role("button", name="Delete group", exact=True).click()
        expect(viewer).to_have_url(base + "/watch/groups")
        assert public(group_path).status_code == 404
        assert public(first_watch).ok
        viewer.goto(category_url)
        viewer.get_by_role("button", name="Follow category", exact=True).click()
        expect(viewer.get_by_role("button", name="Unfollow category")).to_be_visible()
        viewer.get_by_role("link", name="Feed", exact=True).click()
        feed_link = viewer.get_by_role("link", name=first_title)
        expect(feed_link).to_have_count(1)
        expect(viewer.get_by_role("link", name=other_title)).to_have_count(0)
        expect(viewer.get_by_role("link", name=private_title)).to_have_count(0)
        follow_api = base + "/api/viewer/feed/category"
        marker = {"X-CuePorch-Viewer": "1"}
        assert context.request.put(follow_api, headers=marker, data={"name": "Confidential category"}).status == 404
        assert context.request.put(follow_api, headers=marker | {"Origin": "https://unrelated.invalid"},
                                   data={"name": "Art and film"}).status == 403
        assert context.request.get(follow_api + "?name=art+AND+film").json() == {"following": True}
        send("PUT", f"/api/library/work/{first}/privacy", json=private_settings)
        viewer.reload()
        expect(feed_link).to_have_count(0)
        send("PUT", f"/api/library/work/{first}/privacy", json=public_settings)
        viewer.reload()
        expect(feed_link).to_have_count(1)
        viewer.get_by_text("Manage your feed", exact=True).click()
        viewer.get_by_role("button", name="Unfollow Art and film").click()
        expect(feed_link).to_have_count(0)
        assert context.request.get(follow_api + "?name=Art+and+film").json() == {"following": False}
        viewer.goto(category_url)
        viewer.get_by_role("link", name="Channels", exact=True).click()
        expect(viewer.get_by_role("link", name="Partner programs")).to_have_attribute("href", "/watch/channel/partner-program")
        viewer.get_by_role("link", name="Staff Picks", exact=True).click()
        assert viewer.locator(".watch-card strong").all_text_contents() == [other_title, first_title]
        assert private_title not in public("/watch?q=concealed").text
        home = check_operator_home(browser, base, auth, sample, first_title, other_title)

        # Require approval for republication, then switch back to the automatic policy.
        send("PUT", f"/api/library/work/{first}/privacy", json=private_settings)
        send("PUT", "/api/policy/publication", json={"required": True})
        pending = send("PUT", f"/api/library/work/{first}/privacy", json=public_settings)
        assert pending["pending"]["visibility"] == "public"
        assert public(cover).status_code == public(first_watch).status_code == 404
        assert all(first_title not in public(path).text for path in ("/", "/watch/picks", "/watch/categories?category=Art+and+film"))
        send("PUT", "/api/policy/publication", json={"required": False})
        assert first_title in public("/watch/picks").text and public(cover).ok

        send("POST", "/api/library/recently-deleted", json=dict(kind="work", items=[other]))
        assert public(other_watch).status_code == 404
        assert all(other_title not in public(path).text for path in ("/", "/watch/picks"))
        assert public(first_watch).ok
        return dict(editor_reordered=True, stale_change_denied=True, unauthorized_change_denied=True,
                    legacy_channel_link=True, category_and_keyword_search=True, mobile_layout=True, decoded_cover=True,
                    public_playback_width=width, private_and_pending_excluded=True, previous_cover_revoked=True,
                    deleted_excluded=True, automatic_republication_restored_pick=True, operator_home=home)
    finally:
        context.close()


def check_operator_home(browser, base, auth, sample, first_pick, second_pick):
    source = requests.post(base + "/api/library/folders", headers=auth, timeout=10,
                           json=dict(name="Home intake", color="#286266", parent=None))
    source.raise_for_status()
    folder = source.json()["id"]
    name = "Home intake fixture"
    with browser.new_context(viewport=dict(width=1320, height=900)) as context:
        page = context.new_page()
        page.goto(base + "/operator")
        overview = page.get_by_role("region", name="Workspace overview")
        expect(overview).to_be_hidden()
        page.get_by_label("Operator credential").fill("invalid rehearsal credential")
        page.get_by_role("button", name="Open workspace overview").click()
        expect(page.locator("#workspaceHomeStatus")).to_contain_text("Administrator session")
        expect(overview).to_be_hidden()
        page.get_by_label("Operator credential").fill(auth["Authorization"][7:])
        page.get_by_role("button", name="Open workspace overview").click()
        expect(overview).to_be_visible()
        assert page.locator("body").evaluate("body => body.scrollWidth <= innerWidth")
        report = requests.post(base + "/api/analytics/date-report", headers=auth, json={}, timeout=10)
        report.raise_for_status()
        for label, metric in (
            ("homeViews", "views"), ("homeImpressions", "impressions"), ("homeUniqueViewers", "unique_viewers"),
        ):
            expect(page.locator("#" + label)).to_have_text(f'{report.json()["totals"][metric]:,}')
        featured = page.get_by_role("region", name="Staff Picks")
        expect(featured.get_by_role("link", name=second_pick)).to_be_visible()
        featured.get_by_role("button", name="Next pick").click()
        expect(featured.get_by_role("link", name=first_pick)).to_be_visible()
        page.evaluate("""([name, bytes]) => {
          const transfer = new DataTransfer();
          transfer.items.add(new File([Uint8Array.from(atob(bytes), ch => ch.charCodeAt(0))], name + '.mp4', {type: 'video/mp4'}));
          document.querySelector('h1').dispatchEvent(new DragEvent('drop', {
            dataTransfer: transfer, bubbles: true, cancelable: true,
          }));
        }""", [name, b64encode(sample.read_bytes()).decode()])
        dialog = page.get_by_role("dialog", name="Upload files")
        expect(dialog).to_be_visible()
        page.get_by_label("Upload to").select_option(folder)
        expect(page.locator("#uploadVisibility")).to_contain_text("private")
        page.get_by_role("button", name="Upload selected files").click()
        expect(page.locator("#uploadStatus")).to_have_text("All files added.", timeout=30000)
        page.get_by_role("button", name="Close", exact=True).click()
        expect(page.locator("#homeRecentWorks").get_by_role("link", name=name)).to_be_visible()
        library = requests.get(base + "/api/library", headers=auth, timeout=10)
        library.raise_for_status()
        saved = next(work for work in library.json() if work["title"] == name)
        assert saved["folder"] == folder
        privacy = requests.get(f"{base}/api/library/work/{saved['id']}/privacy", headers=auth, timeout=10)
        privacy.raise_for_status()
        assert privacy.json()["visibility"] == "private"
        assert requests.get(base + "/watch/work/" + saved["id"], timeout=10).status_code == 404

        page.locator(".workspace-home-actions summary").click()
        page.get_by_label("Start a project with").select_option(saved["id"])
        page.get_by_role("button", name="Open project editor").click()
        expect(page.get_by_role("heading", name="Timeline editor")).to_be_visible()
        navbar = page.get_by_role("navigation", name="Operator workspace")
        expect(navbar.get_by_role("link", name="Library")).to_have_attribute("aria-current", "page")
        page.get_by_text("Workspace navigation", exact=True).press("Enter")
        expect(navbar).to_be_hidden()
        page.wait_for_function("localStorage.getItem('cueporchWorkspaceNavigation') === 'collapsed'")
        page.goto(base + "/operator/analytics")
        expect(navbar).to_be_hidden()
        page.get_by_text("Workspace navigation", exact=True).press("Enter")
        navbar.get_by_role("link", name="Recently deleted").click()
        expect(page.get_by_role("heading", name="Recently deleted")).to_be_visible()
        expect(navbar.get_by_role("link", name="Recently deleted")).to_have_attribute("aria-current", "page")
        navbar.get_by_role("link", name="Home").click()
        page.get_by_label("Search videos", exact=True).fill(name)
        page.get_by_role("search", name="Find videos").get_by_role("button", name="Search").click()
        expect(page.locator("#items .work-title").get_by_text(name, exact=True)).to_be_visible()
        navbar.get_by_role("link", name="Home").click()
        page.get_by_label("Search in").select_option("watch")
        page.get_by_label("Search videos", exact=True).fill(name)
        page.get_by_role("search", name="Find videos").get_by_role("button", name="Search").click()
        expect(page.get_by_role("link", name=name)).to_have_count(0)
        expect(page.get_by_label("Search public videos")).to_have_value(name)
        page.goto(base + "/operator")
        page.get_by_role("link", name="Host an event").click()
        expect(page.get_by_role("heading", name="New event")).to_be_visible()
    return dict(dropped_video_in_selected_folder=True, private_recent_work=True, rejected_invalid_credentials=True,
                real_metrics=True, public_staff_picks=True, scoped_search=True, restored_navigation_preference=True,
                recently_deleted_link=True, project_and_event_entry_points=True)
