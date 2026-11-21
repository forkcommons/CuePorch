import json
import os
import secrets
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from flask import Flask
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


class TestWooStorefront(unittest.TestCase):
    def test_product_video_playback_and_revocation(self):
        storefront = os.environ.get("CUEPORCH_WOO_STOREFRONT")
        product_raw = os.environ.get("CUEPORCH_WOO_PRODUCT_ID")
        if not storefront or not product_raw:
            self.skipTest("CUEPORCH_WOO_STOREFRONT and CUEPORCH_WOO_PRODUCT_ID are required")
        self.assertEqual(os.environ.get("CUEPORCH_WOO_DISPOSABLE"), "1", "CUEPORCH_WOO_DISPOSABLE=1 is required")
        parsed = urlparse(storefront)
        self.assertTrue(parsed.scheme in ("http", "https"), "storefront must use http or https")
        self.assertTrue(parsed.hostname in ("127.0.0.1", "localhost"), "storefront must target loopback")
        self.assertTrue(parsed.username is None and parsed.password is None, "storefront must not include credentials")
        self.assertEqual(parsed.path, "", "storefront must not include a path")
        self.assertTrue(not parsed.query and not parsed.fragment, "storefront must not include query or fragment")
        self.assertTrue(parsed.port is None or parsed.port > 0, "storefront port must be valid")
        self.assertTrue(product_raw.isascii() and product_raw.isdigit() and int(product_raw) > 0, "CUEPORCH_WOO_PRODUCT_ID must be a positive integer")
        product_id = int(product_raw)

        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps(dict(institution="station", schema=1, works=[]))
            )
            clip = root / "Embed clip.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "testsrc2=size=320x180:rate=24", "-t", "5", "-c:v",
                 "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt",
                 "yuv420p", "-movflags", "+faststart", str(clip)],
                check=True, capture_output=True,
            )
            remote = Flask("cueporch-service")
            service = make_server(
                "127.0.0.1", 0, remote,
                request_handler=QuietHandler, threaded=True,
            )
            base = f"http://127.0.0.1:{service.server_port}"
            operator = secrets.token_urlsafe(32)
            database = root / "workspace.sqlite"

            cleanup.enter_context(patch.dict(os.environ, {"CUEPORCH_PUBLIC_URL": base}))
            with patch("institution.library_worker.start", return_value=lambda: None):
                app = create_app(root, database, operator, "station")
            stops = [
                stop for name, stop in app.extensions.items()
                if name.startswith("stop_")
            ]

            def stop_app():
                while stops:
                    stops.pop()()

            cleanup.callback(stop_app)
            service.app = app
            cleanup.enter_context(service_worker(service))

            with sync_playwright() as playwright, chromium(playwright) as browser:
                page = browser.new_page()
                api = page.context.request
                h = {"Authorization": f"Bearer {operator}"}
                from uuid import uuid4

                r = api.post(
                    f"{base}/api/library/uploads",
                    headers=h,
                    multipart={
                        "upload_id": str(uuid4()),
                        "file": {
                            "name": clip.name,
                            "mimeType": "video/mp4",
                            "buffer": clip.read_bytes(),
                        },
                    },
                )
                assert r.status == 201, r.text
                uid = r.json()["id"]
                pr = api.put(
                    f"{base}/api/library/work/{uid}/privacy",
                    headers={**h, "Content-Type": "application/json"},
                    data=json.dumps({
                        "visibility": "public",
                        "embed": "domains",
                        "domains": [urlparse(storefront).netloc],
                        "download": False,
                    }),
                )
                assert pr.ok, pr.text
                verify_storefront(
                    browser, storefront, product_id, base, operator, uid, clip
                )


def verify_woocommerce_player(iframe, expected_src):
    expect(iframe).to_be_visible()
    expect(iframe).to_have_attribute("src", expected_src)

    video = iframe.content_frame.locator("video")
    expect(video).to_be_visible()

    video.evaluate("""async (el) => {
        el.muted = true;
        await el.play();
    }""")

    result = video.evaluate("""(el) => new Promise((resolve, reject) => {
        const width = el.videoWidth;
        if (width !== 320) {
            reject(new Error(`Expected video width 320, got ${width}`));
            return;
        }
        const startTime = el.currentTime;
        const timer = setTimeout(() => {
            el.removeEventListener('timeupdate', onTimeUpdate);
            reject(new Error('Video currentTime did not advance within 6s'));
        }, 6000);
        function onTimeUpdate() {
            if (el.currentTime > startTime) {
                clearTimeout(timer);
                el.removeEventListener('timeupdate', onTimeUpdate);
                resolve({ width, currentTime: el.currentTime });
            }
        }
        el.addEventListener('timeupdate', onTimeUpdate);
    })""")

    assert result["width"] == 320
    assert result["currentTime"] > 0


def create_woocommerce_video(ap, base, operator):
    before = ap.locator("#_cueporch_video_urls").input_value()
    with ap.expect_popup() as pinfo:
        ap.click("#cueporchCreateProductVideo")
    popup = pinfo.value
    popup.fill("#pickerKey", operator)
    popup.click("#pickerAuth button")
    def h(route): route.abort() if route.request.resource_type == 'fetch' else route.continue_()
    ap.route('**/wp-content/uploads/**', h)
    try:
        popup.click('#pickerCreateVideo')
        expect(popup.locator('#pickerStatus')).to_contain_text('Invalid product source.')
        expect(popup.locator('#pickerCreateVideo')).to_be_enabled()
    finally:
        ap.unroute('**/wp-content/uploads/**', h)
    popup.click("#pickerCreateVideo")
    draft = popup.locator("#pickerEditDraft")
    expect(draft).to_be_visible(timeout=45000)
    href = draft.get_attribute("href") or ""
    verify_created_video(popup, base, operator, href)
    assert ap.locator("#_cueporch_video_urls").input_value() == before
    popup.close()


def verify_created_video(popup, base, operator, href):
    parts = urlparse(href).path.strip("/").split("/")
    assert len(parts) == 4 and parts[:2] == ["operator", "videos"] and parts[3] == "timeline", href
    uid = parts[2]
    r = popup.context.request.get(
        f"{base}/api/library/work/{uid}/privacy",
        headers={"Authorization": f"Bearer {operator}"},
    )
    data = r.json()
    assert data.get("visibility") == "private" and data.get("embed_url") is None
    request = popup.context.request
    headers = {'Authorization': f'Bearer {operator}'}
    project = request.get(f'{base}/api/library/work/{uid}/timeline', headers=headers)
    expect(project).to_be_ok()
    resp = request.post(f'{base}/api/library/work/{uid}/timeline/preview', headers=headers, data={'cut': project.json()['current']})
    expect(resp).to_be_ok()
    preview = resp.json()['preview']
    popup.evaluate("src => { const v = document.createElement('video'); v.id = 'owned-preview-video'; v.src = src; v.muted = true; v.playsInline = true; v.autoplay = true; document.body.appendChild(v); v.play(); }", f'{base}{preview}')
    popup.wait_for_function("() => { const v = document.querySelector('#owned-preview-video'); return !!v && v.videoWidth === 640 && v.videoHeight === 360 && v.currentTime >= 0.2; }", timeout=10000)
    expect(popup.locator('#owned-preview-video')).to_have_js_property('videoWidth', 640)
    expect(popup.locator('#owned-preview-video')).to_have_js_property('videoHeight', 360)
    popup.locator("#owned-preview-video").evaluate("element => element.remove()")


def verify_wordpress_photo_draft(popup, base, operator, clip):
    png = subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(clip),
         '-frames:v', '1', '-c:v', 'png', '-f', 'image2pipe', 'pipe:1'],
        check=True, capture_output=True,
    ).stdout
    popup.click('#pickerPhotoCreation summary')
    popup.fill('#pickerPhotoTitle', 'WordPress photo draft')
    popup.set_input_files('#pickerPhotoFiles', {'name':'photo.png','mimeType':'image/png','buffer':png})
    popup.click('#pickerPhotoCreateStart')
    expect(popup.locator('#pickerPhotoEditDraft')).to_be_visible(timeout=45000)
    verify_created_video(popup,base,operator,popup.get_attribute('#pickerPhotoEditDraft','href') or '')


def verify_private_picker_upload(popup, clip):
    popup.click("#uploadLibrary")
    popup.set_input_files("#libraryFiles", {
        "name": "Private product clip.mp4",
        "mimeType": "video/mp4",
        "buffer": clip.read_bytes(),
    })
    popup.click("#startUpload")
    popup.locator("#uploadStatus").filter(has_text="All files added.").wait_for()
    popup.locator("#uploadFiles").filter(has_text="Added privately").wait_for()
    popup.click("#closeUpload")
    popup.get_by_role("searchbox").fill("Private product clip")
    assert popup.get_by_role("button", name="Choose").count() == 0


def select_in_cueporch_picker(ap, base, operator, uid, clip):
    with ap.expect_popup() as pi:
        ap.locator('#cueporchChooseVideo').click()
    popup = pi.value
    auth = popup.locator('#pickerKey')
    expect(auth).to_be_visible()
    auth.fill('wrong')
    with popup.expect_response(lambda response: response.url == f'{base}/api/library') as rejected:
        popup.locator('#pickerAuth button').click()
    assert rejected.value.status in (401, 403)
    expect(popup.locator('#pickerStatus')).to_contain_text('Session expired')
    expect(popup.locator('#pickerLibrary')).to_be_hidden()
    auth.fill(operator)
    popup.locator('#pickerAuth button').click()
    expect(popup.locator('#pickerLibrary')).to_be_visible()
    verify_private_picker_upload(popup, clip)
    popup.locator('#pickerSearch').fill('Embed clip')
    destination = urlparse(ap.url).scheme + '://' + urlparse(ap.url).netloc
    expect(popup.locator('#pickerDestination')).to_have_text(destination)
    embed = f'{base}/embed/work/{uid}'
    before = ap.locator("#_cueporch_video_urls").input_value()
    ap.evaluate("""() => { window.__cpMsg = null; window.__cpPromise = new Promise(r => window.addEventListener('message', e => { window.__cpMsg = e.data; r(); }, { once: true })); }""")
    popup.evaluate("([data, destination]) => window.opener.postMessage(data, destination)", [{"type": "cueporch-wordpress-selected", "request": "wrong", "embedUrl": embed}, destination])
    ap.wait_for_function("() => window.__cpMsg && window.__cpMsg.type === 'cueporch-wordpress-selected' && window.__cpMsg.request === 'wrong'")
    expect(ap.locator("#_cueporch_video_urls")).to_have_value(before)
    with popup.expect_event('close'):
        popup.get_by_role('button', name='Choose Embed clip', exact=False).click()
    expect(ap.locator('#_cueporch_video_urls')).to_have_value(embed)


def verify_wordpress_post_video_block(ap, guest, storefront, base, operator, uid, clip):
    ap.goto(f'{storefront}/wp-admin/post-new.php?post_type=page')
    ap.wait_for_function("window.wp?.blocks?.getBlockType('cueporch/video') && wp.data.select('core/editor').getCurrentPostType() === 'page'")
    ap.evaluate("() => { const b = wp.blocks.createBlock('cueporch/video'); wp.data.dispatch('core/block-editor').insertBlocks(b); wp.data.dispatch('core/editor').editPost({title: 'CuePorch Video Test'}); }")
    editor = ap
    try:
        ap.wait_for_selector('[data-type="cueporch/video"]', timeout=5000)
    except Exception:
        editor = ap.frame_locator('iframe[name="editor-canvas"]')
        editor.locator('[data-type="cueporch/video"]').wait_for()
    button = editor.get_by_role('button', name='Choose from CuePorch', exact=True)
    expect(button).to_be_enabled()
    for _ in range(3):
        overlay = ap.locator(".components-modal__screen-overlay").first
        if not overlay.is_visible():
            break
        dialog = ap.get_by_role("dialog").last
        close = dialog.locator('button[aria-label="Close"], button[aria-label="Dismiss"]')
        close.first.click() if close.count() else ap.keyboard.press("Escape")
        expect(overlay).to_be_hidden()
    with ap.expect_popup() as popup_info:
        button.click()
    popup = popup_info.value
    popup.fill('#pickerKey', operator)
    popup.click('#pickerAuth button')
    popup.wait_for_selector('#pickerLibrary', state='visible')
    verify_wordpress_photo_draft(popup, base, operator, clip)
    popup.fill('#pickerSearch', 'Embed clip')
    with popup.expect_event('close'):
        popup.get_by_role('button', name='Choose Embed clip', exact=False).click()
    expect(editor.get_by_label('Video URL')).to_have_value(f'{base}/embed/work/{uid}')
    post_id = ap.evaluate("async () => { await wp.data.dispatch('core/editor').editPost({status:'publish'}); await wp.data.dispatch('core/editor').savePost(); const id = wp.data.select('core/editor').getCurrentPostId(); if (wp.data.select('core/editor').getEditedPostAttribute('status') !== 'publish') throw new Error('not published'); return id; }")
    page = guest.new_page()
    try:
        page.goto(f'{storefront}/?page_id={post_id}')
        embed = f"{base}/embed/work/{uid}"
        iframe = page.locator(f'iframe[src="{base}/embed/work/{uid}"]')
        verify_woocommerce_player(iframe, embed)
    finally:
        page.close()


def verify_block_gallery(page, mode):
    video_region = page.locator('.cueporch-block-product-videos')
    gallery = page.locator('.wp-block-woocommerce-product-gallery')
    expect(video_region).to_have_count(1)
    expect(gallery).to_have_count(1)
    expect(gallery.locator('img').first).to_be_visible()
    after = gallery.evaluate("g => !!(g.compareDocumentPosition(document.querySelector('.cueporch-block-product-videos')) & Node.DOCUMENT_POSITION_FOLLOWING)")
    assert after == (mode == 'after'), f'Block gallery order disagrees with {mode}'
    return video_region.locator('iframe')


def verify_storefront(browser, storefront, product_id, base, operator, uid, clip):
    user = os.environ["CUEPORCH_WOO_USERNAME"]
    pwd = os.environ["CUEPORCH_WOO_PASSWORD"]
    admin = browser.new_context()
    guest = browser.new_context()
    embed = f"{base}/embed/work/{uid}"
    watch = f"{base}/watch/work/{uid}"
    try:
        ap = admin.new_page()
        ap.goto(f"{storefront}/wp-login.php")
        ap.fill("#user_login", user)
        ap.fill("#user_pass", pwd)
        ap.click("#wp-submit")
        ap.wait_for_url("**/wp-admin/**")
        ap.goto(f"{storefront}/wp-admin/options-general.php?page=cueporch-video")
        ap.fill("#cueporch_woo_origin", base)
        ap.click("#submit")
        expect(ap.locator("#cueporch_woo_origin")).to_have_value(base)

        block_gallery = os.environ.get("CUEPORCH_WOO_BLOCK_GALLERY") == "1"
        modes = ("before", "after") if block_gallery else ("tab", "before", "after")
        for mode in modes:
            ap.goto(f"{storefront}/wp-admin/post.php?post={product_id}&action=edit")
            if mode == "tab":
                ap.fill("#_cueporch_video_urls", "")
                create_woocommerce_video(ap, base, operator)
                select_in_cueporch_picker(ap, base, operator, uid, clip)
            else:
                ap.fill("#_cueporch_video_urls", watch)
            ap.locator("#_cueporch_video_placement").select_option(mode)
            ap.click("#publish")
            expect(ap.locator("#message.updated, .notice-success").first).to_be_visible()

            gp = guest.new_page()
            gp.goto(f"{storefront}/?post_type=product&p={product_id}")
            if block_gallery:
                iframe = verify_block_gallery(gp, mode)
            elif mode == "tab":
                gp.click('a[href="#tab-cueporch_videos"]')
                iframe = gp.locator("#tab-cueporch_videos iframe").first
            else:
                thumb = gp.locator('.woocommerce-product-gallery .flex-control-thumbs img[alt="Video slide 1"]')
                if "active" not in (thumb.get_attribute("class") or ""):
                    thumb.click()
                iframe = gp.locator(".cueporch-video-slide iframe").first
                pos = gp.evaluate(
                    "()=>{const s=document.querySelector('.cueporch-video-slide');"
                    "const i=document.querySelector('.woocommerce-product-gallery img.wp-post-image');"
                    "if(!s||!i)return null;"
                    "return (i.compareDocumentPosition(s)&Node.DOCUMENT_POSITION_FOLLOWING)?'after':'before';}")
                assert pos == mode, f"gallery order {pos} != {mode}"
            expect(iframe).to_be_visible()
            verify_woocommerce_player(iframe, embed)
            gp.close()
        if not block_gallery:
            verify_wordpress_post_video_block(ap, guest, storefront, base, operator, uid, clip)
        verify_woocommerce_revocation(guest, storefront, product_id, base, operator, uid)
    finally:
        admin.close()
        guest.close()


def verify_woocommerce_revocation(guest, storefront, product_id, base, operator, uid):
    r = guest.request.put(
        f"{base}/api/library/work/{uid}/privacy",
        headers={"Authorization": f"Bearer {operator}"},
        data={"visibility": "private", "embed": "disabled", "domains": [], "download": False},
    )
    assert r.ok
    page = guest.new_page()
    try:
        with page.expect_response(
            lambda resp: resp.url.split("?")[0].endswith(f"/embed/work/{uid}")
            and resp.request.resource_type == "document"
        ) as info:
            page.goto(f"{storefront}/?post_type=product&p={product_id}")
            thumb = page.locator('img[alt="Video slide 1"]')
            if thumb.count() and "flex-active" not in (thumb.first.get_attribute("class") or ""):
                thumb.first.click()
        assert info.value.status == 404
        iframe_sel = f'iframe[src*="/embed/work/{uid}"]'
        assert page.locator(iframe_sel).count() == 1
        assert page.frame_locator(iframe_sel).locator("video").count() == 0
    finally:
        page.close()
