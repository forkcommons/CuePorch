"""Verify Event Series domain routing through synthetic HTTPS origins."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from institution import create_app


class EventSeriesDomains(TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        home = root / 'collection'
        (home / 'objects').mkdir(parents=True)
        (home / 'manifest.json').write_text(
            json.dumps(dict(institution='station', schema=1, works=[])),
            encoding='utf8',
        )
        self.operator = secrets.token_urlsafe(32)
        self.auth = {'Authorization': 'Bearer ' + self.operator}
        self.enterContext(patch.dict(
            os.environ,
            {
                'CUEPORCH_PUBLIC_URL': 'https://watch.example.test',
                'CUEPORCH_PEERTUBE_ORIGIN': '',
                'CUEPORCH_EVENT_SERIES_HOSTS': (
                    'festival.example.test,locked.example.test,spare.example.test,festival.example.invalid'
                ),
            },
        ))
        with patch('institution.library_worker.start', return_value=lambda: None):
            self.app = create_app(home, root / 'workspace.sqlite', self.operator, 'station')
        for name, stop in self.app.extensions.items():
            if name.startswith('stop_') and callable(stop):
                self.addCleanup(stop)
        self.client = self.app.test_client()

    def create_series(self, slug, title, visibility="public", host="", view_password="", events=()):
        base_url = "https://watch.example.test"

        response = self.client.post(
            "/api/event-series",
            json={},
            headers=self.auth,
            base_url=base_url,
        )
        self.assertEqual(response.status_code, 201)
        draft = response.get_json()

        response = self.client.put(
            f"/api/event-series/{draft['id']}",
            json={
                "revision": draft["revision"],
                "slug": slug,
                "settings": {
                    "title": title,
                    "visibility": visibility,
                    "custom_host": host,
                    "events": list(events),
                },
                "view_password": view_password,
            },
            headers=self.auth,
            base_url=base_url,
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_public_domain_absolute_alias(self):
        start = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
        end = start + timedelta(hours=1)
        create_event_resp = self.client.post(
            '/api/live-events',
            headers=self.auth,
            base_url='https://watch.example.test',
            json={
                "title": "Screening",
                "slug": "screening",
                "visibility": "public",
                "type": "live",
                "latency": "standard",
                "save_replay": False,
                "schedule": {
                    "timezone": "UTC",
                    "repeat": "none",
                    "start": start.strftime('%Y-%m-%dT%H:%M'),
                    "end": end.strftime('%Y-%m-%dT%H:%M'),
                },
            },
        )
        self.assertEqual(create_event_resp.status_code, 201)
        event_id = create_event_resp.get_json()['id']
        entry = self.create_series(
            "festival", "Festival program", host="festival.example.test", events=[event_id]
        )
        self.assertEqual(entry['watch'], 'https://festival.example.test/')

        response = self.client.get('/', base_url='https://festival.example.test')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Festival program', response.data)
        self.assertIn(b'https://watch.example.test/events/screening?occurrence=', response.data)

        response = self.client.get('/event-series/festival', base_url='https://watch.example.test')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Festival program', response.data)

        for base_url in ('https://watch.example.test', 'https://unused.example.test'):
            response = self.client.get('/', base_url=base_url)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b'Festival program', response.data)
        response = self.client.get('/', base_url='https://spare.example.test')
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b'Festival program', response.data)

        headers = dict(self.auth)
        headers['X-Forwarded-Host'] = 'watch.example.test'
        response = self.client.get(
            '/api/event-series',
            headers=headers,
            base_url='https://festival.example.test:444',
        )
        self.assertEqual(response.status_code, 404)

        response = self.client.get(
            '/api/event-series',
            headers=self.auth,
            base_url='https://watch.example.test',
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.get('/?preview=invalid', base_url='https://festival.example.test')
        self.assertEqual(response.status_code, 404)

        response = self.client.get('/static/cueporch.css', base_url='https://festival.example.test')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/css')

    def test_password_protected_domain(self):
        self.create_series(
            'locked',
            'Private program',
            'password',
            'locked.example.test',
            'fixture-password',
        )

        response = self.client.get('/', base_url='https://locked.example.test')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'Private program', response.data)

        response = self.client.post(
            '/',
            base_url='https://locked.example.test',
            data={'password': 'wrong'},
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            '/',
            base_url='https://locked.example.test',
            data={'password': 'fixture-password'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Private program', response.data)

        cookies = response.headers.getlist('Set-Cookie')
        self.assertTrue(cookies)
        cookie_header = '; '.join(cookies)
        self.assertIn('Secure', cookie_header)
        self.assertIn('HttpOnly', cookie_header)
        self.assertIn('Path=/', cookie_header)
        self.assertNotIn('Domain=', cookie_header)

        response = self.client.get('/', base_url='https://locked.example.test')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Private program', response.data)

        fresh_client = self.app.test_client()
        response = fresh_client.get('/', base_url='https://locked.example.test')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'Private program', response.data)

    def test_team_hostname_uses_workspace_sign_in_and_revocation(self):
        import re

        canonical = 'https://watch.example.test'
        alias = 'https://festival.example.test'
        entry = self.create_series(
            'festival', 'Festival program', visibility='team', host='festival.example.test'
        )
        self.assertEqual(entry['watch'], alias + '/')

        locked = self.client.get('/', base_url=alias)
        self.assertEqual(locked.status_code, 200)
        self.assertNotIn(b'Festival program', locked.data)
        link = re.search(rb'href="([^"]+)">Sign in</a>', locked.data)
        self.assertIsNotNone(link)
        destination = link[1].decode()
        self.assertTrue(destination.startswith(canonical + '/team/event-series/connect?challenge='))
        connect = self.client.get(destination)
        self.assertEqual(connect.status_code, 303)
        self.assertEqual(connect.headers['Location'], '/team')

        marker = {'X-CuePorch-Team': '1'}
        invitation = self.client.post(
            '/api/team/invitations', base_url=canonical, headers={**self.auth, **marker},
            json=dict(email='host@example.test', role='admin'),
        )
        self.assertEqual(invitation.status_code, 201, invitation.get_data(as_text=True))
        invite_code = invitation.get_json()['path'].split('#', 1)[1]
        joined = self.client.post(
            '/api/team/join', base_url=canonical, headers=marker,
            json=dict(invitation=invite_code, name='Host', password='long-password'),
        )
        self.assertEqual(joined.status_code, 201, joined.get_data(as_text=True))
        self.assertIn(b'Return to Event Series', self.client.get('/team', base_url=canonical).data)

        handoff = self.client.get(destination)
        self.assertEqual(handoff.status_code, 200)
        self.assertIn(b'action="https://festival.example.test/"', handoff.data)
        code = re.search(rb'name="team_code" value="([^"]+)"', handoff.data)
        self.assertIsNotNone(code)
        fields = dict(team_code=code[1].decode())
        wrong_origin = self.client.post(
            '/', base_url=alias, data=fields, headers={'Origin': 'https://other.example.test'},
        )
        self.assertEqual(wrong_origin.status_code, 403)
        fresh = self.app.test_client()
        self.assertEqual(fresh.post(
            '/', base_url=alias, data=fields, headers={'Origin': canonical},
        ).status_code, 404)

        redeemed = self.client.post('/', base_url=alias, data=fields, headers={'Origin': canonical})
        self.assertEqual(redeemed.status_code, 303)
        self.assertEqual(redeemed.headers['Location'], '/')
        self.assertEqual(self.client.post(
            '/', base_url=alias, data=fields, headers={'Origin': canonical},
        ).status_code, 404)
        self.assertIn(b'Festival program', self.client.get('/', base_url=alias).data)
        self.assertEqual(self.client.get('/api/team/account', base_url=alias).status_code, 404)

        signed_out = self.client.delete('/api/team/session', base_url=canonical, headers=marker, json={})
        self.assertEqual(signed_out.status_code, 204)
        relocked = self.client.get('/', base_url=alias)
        self.assertEqual(relocked.status_code, 200)
        self.assertIn(b'Sign in', relocked.data)
        self.assertNotIn(b'Festival program', relocked.data)

    def test_festival_alias_team_join_origin(self):
        from urllib.parse import urlsplit
        from browser import chromium
        from playwright.sync_api import expect, sync_playwright

        canonical = "https://watch.example.test"
        alias = "https://festival.example.invalid"
        allowed_origins = {canonical, alias}

        self.create_series("festival", "Festival program", visibility="team", host="festival.example.invalid")
        inv = self.client.post(
            "/api/team/invitations",
            base_url=canonical,
            headers={**self.auth, "X-CuePorch-Team": "1"},
            json=dict(email="host@example.test", role="admin"),
        )
        self.assertEqual(inv.status_code, 201)
        token = inv.get_json()["path"].split("#", 1)[1]

        with sync_playwright() as p, chromium(p) as browser:
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(8000)
            document_post_origins = []

            def intercept(route):
                req = route.request
                split = urlsplit(req.url)
                origin = f"{split.scheme}://{split.netloc}"
                if origin not in allowed_origins:
                    route.abort()
                    return
                req_headers = req.all_headers()
                path = (split.path or "/") + (("?" + split.query) if split.query else "")
                headers = {
                    k: v
                    for k, v in req_headers.items()
                    if k.lower() not in ("host", "content-length")
                }
                if req.method == "POST" and req.resource_type == "document":
                    document_post_origins.append(req_headers.get("origin"))
                res = self.app.test_client(use_cookies=False).open(
                    path,
                    base_url=f"{split.scheme}://{split.netloc}",
                    method=req.method,
                    headers=headers,
                    data=req.post_data_buffer,
                    buffered=True,
                )
                out = {
                    k: v
                    for k, v in res.headers.items()
                    if k.lower() not in ("content-length", "set-cookie")
                }
                cookies = [v for k, v in res.headers.items() if k.lower() == "set-cookie"]
                if cookies:
                    out["Set-Cookie"] = cookies[0]
                route.fulfill(status=res.status_code, headers=out, body=res.get_data())

            context.route("**/*", intercept)
            page.goto(alias + "/")
            page.get_by_role("link", name="Sign in").click()
            page.wait_for_url(lambda url: url.startswith(canonical + "/team"))
            page.evaluate(
                "async token => { const r = await fetch('/api/team/join', "
                "{method:'POST', headers:{'Content-Type':'application/json','X-CuePorch-Team':'1'}, "
                "body: JSON.stringify({invitation: token, name:'Festival host', password:'long-password'})}); "
                "if (!r.ok) throw new Error(String(r.status)); }",
                token,
            )
            page.reload()
            page.get_by_role("link", name="Return to Event Series").click()
            page.get_by_role("button", name="Continue").click()
            page.wait_for_url(alias + "/")
            expect(page.locator("h1")).to_have_text("Festival program")
            self.assertIn(canonical, document_post_origins)

    def test_event_series_custom_host_rules(self):
        self.create_series('existing', 'Existing program', host='festival.example.test')

        response = self.client.post(
            '/api/event-series',
            headers=self.auth,
            base_url='https://watch.example.test',
            json={},
        )
        self.assertEqual(response.status_code, 201)
        draft = response.get_json()

        payload = dict(
            revision=draft['revision'],
            slug='next',
            settings=dict(
                title='Next program',
                visibility='public',
                custom_host='festival.example.test',
            ),
        )

        response = self.client.put(
            f"/api/event-series/{draft['id']}",
            headers=self.auth,
            base_url='https://watch.example.test',
            json=payload,
        )
        self.assertEqual(response.status_code, 409)

        payload['settings']['custom_host'] = 'unapproved.example.test'
        response = self.client.put(
            f"/api/event-series/{draft['id']}",
            headers=self.auth,
            base_url='https://watch.example.test',
            json=payload,
        )
        self.assertEqual(response.status_code, 422)

        payload['settings']['visibility'] = 'team'
        payload['settings']['custom_host'] = ''
        response = self.client.put(
            f"/api/event-series/{draft['id']}",
            headers=self.auth,
            base_url='https://watch.example.test',
            json=payload,
        )
        self.assertEqual(response.status_code, 200)
