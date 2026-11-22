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
                'CUEPORCH_EVENT_SERIES_HOSTS': 'festival.example.test,locked.example.test,spare.example.test',
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
