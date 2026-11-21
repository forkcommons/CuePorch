"""Acceptance tests for Shopify publication."""

import sys
from contextlib import ExitStack
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from flask import Flask, request, redirect
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from test_drive_import import QuietHandler, service_worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app
import workspace_oauth
from shopify_product_media import StoreConnection


class StoreFixture:
    SHOP = "fixture.myshopify.com"
    PRODUCT = "gid://shopify/Product/42"
    VIDEO = "gid://shopify/Video/99"
    UPLOAD = "https://shopify-video-production-core-originals.storage.googleapis.com"
    RESOURCE = UPLOAD + "?external_video_id=99"

    def __init__(self, test, callback, sample):
        self.app = Flask(__name__)
        self.t = test
        self.callback = callback
        self.sample = sample
        self.gallery = ["gid://shopify/Image/1"]
        self.ready = False
        self.uploads = []
        self.creates = self.attaches = self.refreshes = 0
        self.refresh = "fixture-refresh"
        self.app.add_url_rule(
            "/admin/oauth/authorize", view_func=self.authorize, methods=["GET"]
        )
        self.app.add_url_rule(
            "/admin/oauth/access_token", view_func=self.token, methods=["POST"]
        )
        self.app.add_url_rule(
            "/admin/api/2026-10/graphql.json",
            endpoint="graphql",
            view_func=lambda: self.graphql(),
            methods=["POST"],
        )
        self.app.add_url_rule(
            "/signed-upload",
            endpoint="upload",
            view_func=lambda: self.upload(),
            methods=["POST"],
        )

    def authorize(self):
        a = request.args
        s = a.get("scope", "")
        if (
            a.get("redirect_uri") != self.callback
            or "write_products" not in s
            or "write_files" not in s
        ):
            return "bad request", 400
        p = {
            "shop": a.get("shop", self.SHOP),
            "state": a.get("state", "state"),
            "code": "fixture-code",
        }
        p["hmac"] = hmac.new(
            b"fixture-secret", urlencode(sorted(p.items())).encode(), hashlib.sha256
        ).hexdigest()
        return redirect(self.callback + "?" + urlencode(p))

    def token(self):
        f = request.form
        if (
            f.get("client_id") != "cueporch"
            or f.get("client_secret") != "fixture-secret"
        ):
            return {"error": "invalid_client"}, 401
        if f.get("grant_type") == "refresh_token":
            if f.get("refresh_token") != self.refresh:
                return {"error": "invalid_grant"}, 400
            self.refreshes += 1
            self.refresh = f"fixture-refresh-{self.refreshes + 1}"
            return {
                "access_token": "fixture-renewed",
                "refresh_token": self.refresh,
                "expires_in": 3600,
                "refresh_token_expires_in": 3600,
                "scope": "write_products,write_files",
            }
        if f.get("code") != "fixture-code" or f.get("expiring") != "1":
            return {"error": "invalid_grant"}, 400
        return {
            "access_token": "fixture-access",
            "refresh_token": self.refresh,
            "expires_in": 60,
            "refresh_token_expires_in": 3600,
            "scope": "write_products,write_files",
        }

    def graphql(self):
        if request.headers.get("X-Shopify-Access-Token") != "fixture-renewed":
            return {"errors": [{"message": "Invalid API key or access token"}]}, 401

        body = request.get_json() or {}
        query = body.get("query", "")
        variables = body.get("variables") or {}

        if "stagedUploadsCreate(" in query:
            inputs = variables.get("input")
            self.t.assertIsInstance(inputs, list)
            self.t.assertEqual(len(inputs), 1)
            item = inputs[0]
            self.t.assertEqual(item.get("resource"), "VIDEO")
            self.t.assertEqual(item.get("httpMethod"), "POST")
            self.t.assertEqual(str(item.get("fileSize")), str(len(self.sample)))
            self.t.assertEqual(item.get("mimeType"), "video/mp4")
            self.t.assertEqual(item.get("filename"), "clip.mp4")
            return {
                "data": {
                    "stagedUploadsCreate": {
                        "stagedTargets": [
                            {
                                "url": self.UPLOAD,
                                "resourceUrl": self.RESOURCE,
                                "parameters": [{"name": "key", "value": "fixture-key"}],
                            }
                        ],
                        "userErrors": [],
                    }
                }
            }

        if "fileCreate(" in query:
            files = variables.get("files") or variables.get("input")
            self.t.assertIsInstance(files, list)
            self.t.assertEqual(len(files), 1)
            item = files[0]
            self.t.assertEqual(self.uploads, [self.sample])
            self.t.assertEqual(item.get("originalSource"), self.RESOURCE)
            self.t.assertEqual(item.get("contentType"), "VIDEO")
            self.t.assertEqual(self.creates, 0)
            self.creates += 1
            return {
                "data": {
                    "fileCreate": {
                        "files": [{"id": self.VIDEO, "fileStatus": "PROCESSING"}],
                        "userErrors": [],
                    }
                }
            }

        if "node(" in query:
            self.t.assertEqual(variables.get("id"), self.VIDEO)
            status = "READY" if self.ready else "PROCESSING"
            return {"data": {"node": {"id": self.VIDEO, "fileStatus": status}}}

        if "fileUpdate(" in query:
            files = variables.get("files") or variables.get("input")
            self.t.assertIsInstance(files, list)
            self.t.assertEqual(len(files), 1)
            item = files[0]
            self.t.assertEqual(item.get("id"), self.VIDEO)
            self.t.assertEqual(item.get("referencesToAdd"), [self.PRODUCT])
            self.t.assertNotIn(self.VIDEO, self.gallery)
            self.gallery.append(self.VIDEO)
            self.attaches += 1
            return {
                "data": {
                    "fileUpdate": {
                        "files": [{"id": self.VIDEO, "fileStatus": "READY"}],
                        "userErrors": [],
                    }
                }
            }

        if "products(" in query:
            q = variables.get("query") or ""
            cursor = variables.get("cursor")

            if q == "Print":
                self.t.assertIsNone(cursor)
                nodes = [{"id": self.PRODUCT, "title": "Print Edition"}]
                page_info = {"hasNextPage": False, "endCursor": None}
            elif cursor == "more":
                self.t.assertIn(q, ("",))
                nodes = [{"id": "gid://shopify/Product/43", "title": "Giclée"}]
                page_info = {"hasNextPage": False, "endCursor": None}
            else:
                self.t.assertIn(q, ("",))
                self.t.assertIsNone(cursor)
                nodes = [{"id": self.PRODUCT, "title": "Print Edition"}]
                page_info = {"hasNextPage": True, "endCursor": "more"}

            return {"data": {"products": {"nodes": nodes, "pageInfo": page_info}}}

        if "product(" in query:
            self.t.assertIn("id", variables)
            if variables.get("id") == self.PRODUCT:
                product = {"id": self.PRODUCT, "title": "Print Edition"}
            else:
                product = None
            return {"data": {"product": product}}

        return {"errors": [{"message": "Unsupported operation"}]}, 400

    def upload(self):
        self.t.assertIsNone(request.headers.get("Authorization"))
        self.t.assertIsNone(request.headers.get("X-Shopify-Access-Token"))
        self.t.assertEqual(request.form.get("key"), "fixture-key")

        uploaded = request.files["file"]
        self.t.assertEqual(uploaded.filename, "clip.mp4")
        data = uploaded.read()
        self.t.assertEqual(data, self.sample)

        self.uploads.append(data)
        return "", 201


class ShopifyPublication(unittest.TestCase):
    def test_shopify_product_media(self):
        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(TemporaryDirectory()))
            (root / "objects").mkdir()
            (root / "manifest.json").write_text(
                json.dumps(dict(institution="station", schema=1, works=[]))
            )
            clip = root / "Shopify product.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=24",
                    "-t",
                    "3",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(clip),
                ],
                check=True,
                capture_output=True,
            )
            video = clip.read_bytes()

            remote = Flask("shopify-service")
            service = make_server(
                "127.0.0.1", 0, remote, request_handler=QuietHandler, threaded=True
            )
            base = f"http://127.0.0.1:{service.server_port}"
            callback = base + "/integration/oauth/shopify/callback"
            fixture = StoreFixture(self, callback, video)
            provider = make_server(
                "127.0.0.1", 0, fixture.app, request_handler=QuietHandler, threaded=True
            )
            provider_origin = f"http://127.0.0.1:{provider.server_port}"
            database = root / "workspace.sqlite"
            operator = secrets.token_urlsafe(32)

            cleanup.enter_context(
                patch.dict(
                    "os.environ",
                    {
                        "CUEPORCH_PUBLIC_URL": "",
                        "CUEPORCH_SHOPIFY_CLIENT_ID": "cueporch",
                        "CUEPORCH_SHOPIFY_CLIENT_SECRET": "fixture-secret",
                    },
                )
            )

            def store_origin(shop):
                self.assertEqual(fixture.SHOP, shop)
                return provider_origin

            cleanup.enter_context(
                patch.object(StoreConnection, "store_origin", side_effect=store_origin)
            )

            original = workspace_oauth.provider_request

            def provider_request(method, url, **kwargs):
                if url == fixture.UPLOAD:
                    url = provider_origin + "/signed-upload"
                return original(method, url, **kwargs)

            cleanup.enter_context(
                patch.object(workspace_oauth, "provider_request", provider_request)
            )

            def start_app():
                with patch(
                    "institution.library_worker.start", return_value=lambda: None
                ):
                    app = create_app(root, database, operator, "station")
                stops = [
                    stop
                    for name, stop in app.extensions.items()
                    if name.startswith("stop_")
                ]

                def stop_app():
                    while stops:
                        stops.pop()()

                cleanup.callback(stop_app)
                service.app = app
                return stop_app

            stop = start_app()
            cleanup.enter_context(service_worker(service))
            cleanup.enter_context(service_worker(provider))
            with sync_playwright() as playwright, chromium(playwright) as browser:
                self.exercise(
                    browser, base, operator, clip, video, fixture, stop, start_app
                )

    def exercise(self, browser, base, operator, clip, video, fixture, stop, start_app):
        from time import monotonic, sleep
        from uuid import uuid4

        page = browser.new_page()
        api = page.context.request
        auth = {"Authorization": "Bearer " + operator}

        page.goto(base + "/operator/integrations")
        page.get_by_label("Operator credential").fill(operator)
        page.get_by_role("button", name="Open integrations").click()
        page.get_by_label("Shopify store domain").fill("fixture")
        page.get_by_role("button", name="Connect Shopify").click()
        expect(page.locator("#integrationStatus")).to_have_text("Shopify is connected.")
        upload_id = str(uuid4())
        resp = api.post(
            base + "/api/library/uploads",
            headers=auth,
            multipart={
                "upload_id": upload_id,
                "file": {"name": clip.name, "mimeType": "video/mp4", "buffer": video},
            },
        )
        assert resp.status == 201, resp.text()
        uid = resp.json()["id"]
        page.goto(f"{base}/operator/videos/{uid}")
        page.get_by_role("link", name="Add to Shopify product").click()
        expect(page.locator("#socialConnectionScope")).to_have_text(fixture.SHOP)
        selector = page.locator("#socialPage")
        selector.select_option(index=1)
        expect(selector.locator("option")).to_have_text(
            ["Choose a product", "Print Edition"]
        )
        page.get_by_role("button", name="More products").click()
        expect(selector.locator("option")).to_have_text(
            ["Choose a product", "Print Edition", "Giclée"]
        )
        page.get_by_label("Product name").fill("Print")
        page.get_by_role("button", name="Find products").click()
        expect(selector.locator("option")).to_have_text(
            ["Choose a product", "Print Edition"]
        )
        assert fixture.refreshes == 1

        endpoint = f"{base}/api/library/work/{uid}/social/shopify"
        body = api.get(endpoint, headers=auth).text()
        assert "fixture-secret" not in body and "fixture-renewed" not in body
        selector.select_option(fixture.PRODUCT)
        page.get_by_role(
            "checkbox", name="Add this video to the selected product"
        ).check()
        page.get_by_role("button", name="Add to Shopify product").click()
        deadline = monotonic() + 12
        while fixture.creates == 0 and monotonic() < deadline:
            sleep(0.1)
        assert fixture.creates == 1
        assert (fixture.uploads, fixture.attaches, fixture.gallery) == (
            [video],
            0,
            ["gid://shopify/Image/1"],
        )

        stop()
        fixture.ready = True
        start_app()
        page.reload()
        jobs = page.locator("#socialJobs")
        link = jobs.locator(
            f'a[href="https://{fixture.SHOP}/admin/products/42"]',
            has_text="Open on Shopify",
        )
        link.wait_for()
        assert link.inner_text() == "Open on Shopify"
        assert "delivered" in jobs.inner_text().lower()
        assert fixture.VIDEO in jobs.inner_text()
        assert fixture.creates == fixture.attaches == 1
        assert fixture.gallery == ["gid://shopify/Image/1", fixture.VIDEO]
        assert fixture.uploads == [video]
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            input=fixture.uploads[0],
            check=True,
        )
        response = api.put(
            base + "/api/policy/publication", headers=auth, data={"required": True}
        )
        assert response.ok
        page.reload()
        selector.select_option(fixture.PRODUCT)
        page.get_by_role(
            "checkbox", name="Add this video to the selected product"
        ).check()
        page.get_by_role("button", name="Add to Shopify product").click()
        jobs.get_by_text("waiting for publication approval").wait_for()
        assert fixture.uploads == [video]
        page.get_by_role("button", name="Cancel upload").click()
        jobs.get_by_text("canceled").wait_for()
