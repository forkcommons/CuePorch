"""Verify Event Series translation policies with a deterministic local runtime double."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from institution import create_app
from test_drive_import import QuietHandler, service_worker


class EventSeriesTranslation(unittest.TestCase):
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
            {"CUEPORCH_PUBLIC_URL": "", "CUEPORCH_PEERTUBE_ORIGIN": ""},
        ))
        self.enterContext(patch('caption_adapter.translation_runtime', return_value=True))
        self.model = self.enterContext(patch(
            'caption_adapter.translate_cues',
            side_effect=lambda cues, language, stopping: [
                dict(text=f'{language}: ' + cue['text']) for cue in cues
            ],
        ))
        with patch('institution.library_worker.start', return_value=lambda: None):
            app = create_app(home, root / 'workspace.sqlite', self.operator, 'station')
        for name, stop in app.extensions.items():
            if name.startswith('stop_') and callable(stop):
                self.addCleanup(stop)
        self.app = app
        self.client = app.test_client()

    def wait_for_job(self, uid, job_id, state, path=None):
        deadline = time.monotonic() + 6
        last_state = None
        while time.monotonic() < deadline:
            response = self.client.get(path or f'/api/event-series/{uid}/translation-jobs', headers=self.auth)
            self.assertEqual(response.status_code, 200)
            for job in response.get_json()['jobs']:
                if job['id'] == job_id:
                    last_state = job['state']
                    if job['state'] == state:
                        return job
            time.sleep(0.05)
        self.fail(f"Job {job_id} did not reach state {state}; last state was {last_state}")

    def create_translation_event(self):
        opens = datetime.now(timezone.utc) + timedelta(hours=1)
        response = self.client.post(
            "/api/live-events",
            headers=self.auth,
            json={
                "title": "Localized gathering",
                "description": "A community program.",
                "schedule": {
                    "start": opens.strftime("%Y-%m-%dT%H:%M"),
                    "end": (opens + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
                    "repeat": "none",
                    "timezone": "UTC",
                },
                "visibility": "public",
                "type": "live",
                "latency": "standard",
                "save_replay": True,
                "slug": "localized-gathering",
            },
        )
        self.assertEqual(response.status_code, 201)
        event = response.get_json()

        response = self.client.post(
            "/api/live-event-speaker-profiles",
            headers=self.auth,
            json={
                "name": "Mina Reed",
                "role": "Curator",
                "bio": "Introduces the films.",
            },
        )
        self.assertEqual(response.status_code, 201)
        speaker = response.get_json()

        response = self.client.put(
            f"/api/live-events/{event['id']}/landing",
            headers=self.auth,
            json={
                "revision": None,
                "settings": {
                    "enabled": True,
                    "theme": "light",
                    "layout": "stacked",
                    "accent": "#3266aa",
                    "show_logo": True,
                    "show_poster": False,
                    "speaker_ids": [speaker["id"]],
                },
            },
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.get(
            f"/api/live-events/{event['id']}/landing",
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200)
        landing = response.get_json()

        return event, landing, speaker

    def test_landing_translation_job_flow(self):
        event, landing, speaker = self.create_translation_event()
        uid = event["id"]
        path = f'/api/live-events/{uid}/landing/translation-jobs'
        response = self.client.post(
            path,
            headers=self.auth,
            json=dict(revision=landing["revision"], language='es'),
        )
        assert response.status_code == 202
        jobid = response.get_json()['job']['id']
        response = self.client.get(path)
        assert response.status_code == 403
        self.wait_for_job(uid, jobid, 'saved', path=path)
        response = self.client.get(event['watch'] + '?lang=es')
        html = response.get_data(as_text=True)
        assert "es: Localized gathering" in html
        assert "es: Curator" in html
        assert "Mina Reed" in html

        resp = self.client.put(
            "/api/policy/translation",
            headers=self.auth,
            json={"required": True},
        )
        assert resp.status_code == 200

        resp = self.client.get(f"/api/live-events/{uid}/landing", headers=self.auth)
        current_revision = resp.get_json()["revision"]

        resp = self.client.post(
            path,
            headers=self.auth,
            json={"revision": current_revision, "language": "fr"},
        )
        assert resp.status_code == 202
        job = resp.get_json()["job"]
        assert job["state"] == "waiting"

        resp = self.client.put(
            f"/api/live-event-speaker-profiles/{speaker['id']}",
            headers=self.auth,
            json=dict(
                revision=speaker["revision"],
                name=speaker["name"],
                role="New host",
                bio=speaker["bio"],
            ),
        )
        assert resp.status_code == 200

        calls_before = self.model.call_count

        resp = self.client.post(
            f"/api/actions/{job['id']}/decision",
            headers=self.auth,
            json={"decision": "approve"},
        )
        assert resp.status_code == 204

        self.wait_for_job(uid, job["id"], "superseded", path=path)
        assert self.model.call_count == calls_before

    def test_landing_generated_draft_in_browser(self):
        event, _, _ = self.create_translation_event()
        uid = event["id"]
        path = f"/api/live-events/{uid}/landing/translation-jobs"
        server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=QuietHandler)
        self.enterContext(service_worker(server))
        base = f"http://127.0.0.1:{server.server_port}"
        with sync_playwright() as playwright, chromium(playwright) as browser:
            page = browser.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(base + "/operator/live-events")
            page.get_by_role("button", name="Open production for Localized gathering").first.click()
            page.locator("#eventLanding").get_by_text("Event landing page", exact=True).click()
            page.select_option("#eventLandingLanguage", "es")
            page.uncheck("#eventLandingAiAuto")
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/translation-jobs")) as info:
                page.click("#eventLandingAiGenerate")
            assert info.value.status == 202
            job_id = info.value.json()["job"]["id"]
            self.wait_for_job(uid, job_id, "draft", path=path)
            page.click("#eventLandingAiRefresh")
            page.click("#eventLandingAiDraft")
            assert page.input_value("#eventLandingTitle") == "es: Localized gathering"
            assert page.locator("#eventLandingTranslatedSpeakers").get_by_role("group", name="Mina Reed").locator("input").first.input_value() == "es: Curator"
            page.fill("#eventLandingTitle", "Programa local")
            page.check("#eventLandingTranslationPublished")
            with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/landing")) as resp:
                page.get_by_role("button", name="Save landing page", exact=True).click()
            assert resp.value.status == 200
            viewer = browser.new_page()
            viewer.goto(base + event["watch"] + "?lang=es")
            assert viewer.locator("h1").inner_text() == "Programa local"
            assert viewer.get_by_text("es: Curator", exact=True).first.is_visible()

    def test_series_translation_waits_and_then_publishes(self):
        create = self.client.post(
            '/api/event-series',
            json={},
            headers=self.auth,
        )
        assert create.status_code == 201
        created = create.get_json()
        uid = created['id']
        assert created['revision']

        updated = self.client.put(
            f'/api/event-series/{uid}',
            json=dict(
                revision=created['revision'],
                slug='local-film',
                settings=dict(
                    title='Local film program',
                    visibility='public',
                ),
            ),
            headers=self.auth,
        )
        assert updated.status_code == 200
        revision = updated.get_json()['revision']
        assert isinstance(revision, str)

        policy = self.client.put(
            '/api/policy/translation',
            json={'required': True},
            headers=self.auth,
        )
        assert policy.status_code == 200

        job_resp = self.client.post(
            f'/api/event-series/{uid}/translation-jobs',
            json={'revision': revision, 'language': 'fr'},
            headers=self.auth,
        )
        assert job_resp.status_code == 202
        job = job_resp.get_json()['job']
        assert job['state'] == 'waiting'
        job_id = job['id']

        duplicate = self.client.post(
            f'/api/event-series/{uid}/translation-jobs',
            json={'revision': revision, 'language': 'fr'},
            headers=self.auth,
        )
        assert duplicate.status_code == 202
        assert duplicate.get_json()['job']['id'] == job_id

        unauth = self.client.post(
            f'/api/event-series/{uid}/translation-jobs',
            json={'revision': revision, 'language': 'fr'},
        )
        assert unauth.status_code == 403

        decision = self.client.post(
            f'/api/actions/{job_id}/decision',
            json={'decision': 'approve'},
            headers=self.auth,
        )
        assert decision.status_code == 204

        saved = self.wait_for_job(uid, job_id, 'saved')
        assert saved['state'] == 'saved'

        page = self.client.get('/event-series/local-film?lang=fr')
        assert page.status_code == 200
        assert 'fr: Local film program' in page.get_data(as_text=True)

        current = self.client.get(
            f'/api/event-series/{uid}',
            headers=self.auth,
        )
        assert current.status_code == 200
        current_revision = current.get_json()['revision']

        draft_job_resp = self.client.post(
            f'/api/event-series/{uid}/translation-jobs',
            json={
                'revision': current_revision,
                'language': 'es',
                'apply_now': False,
            },
            headers=self.auth,
        )
        assert draft_job_resp.status_code == 202
        draft_job = draft_job_resp.get_json()['job']
        assert draft_job['state'] == 'waiting'
        draft_job_id = draft_job['id']

        draft_decision = self.client.post(
            f'/api/actions/{draft_job_id}/decision',
            json={'decision': 'approve'},
            headers=self.auth,
        )
        assert draft_decision.status_code == 204

        draft = self.wait_for_job(uid, draft_job_id, 'draft')
        assert draft['result']['published'] is False
        assert draft['result']['title'] == 'es: Local film program'

        draft_page = self.client.get('/event-series/local-film?lang=es')
        assert draft_page.status_code == 200
        draft_text = draft_page.get_data(as_text=True)
        assert 'Local film program' in draft_text
        assert 'es: Local film program' not in draft_text

    def test_operator_loads_generated_draft(self):
        create = self.client.post(
            '/api/event-series',
            json={},
            headers=self.auth,
        )
        self.assertEqual(create.status_code, 201)
        entry = create.get_json()
        uid = entry['id']
        update = self.client.put(
            f'/api/event-series/{uid}',
            json=dict(
                revision=entry['revision'],
                slug='gallery-program',
                settings=dict(
                    title='Gallery program',
                    visibility='public',
                    presentation=dict(
                        blocks=["navigation", "hero", "faq"],
                        navigation=[
                            dict(
                                label='Schedule',
                                href='https://venue.example.test/schedule',
                            ),
                        ],
                        faq=[
                            dict(question='Where?', answer='The gallery'),
                        ],
                    ),
                ),
            ),
            headers=self.auth,
        )
        self.assertEqual(update.status_code, 200)
        server = make_server('127.0.0.1', 0, self.app, threaded=True, request_handler=QuietHandler)
        self.enterContext(service_worker(server))
        base = f'http://127.0.0.1:{server.server_port}'
        with sync_playwright() as playwright, chromium(playwright) as browser:
            context = browser.new_context()
            page = context.new_page()
            page.add_init_script(f"sessionStorage.setItem('cueporchOperator', {json.dumps(self.operator)})")
            page.goto(base + "/operator/event-series")
            page.get_by_role("button", name="Gallery program", exact=True).click()
            page.select_option("#seriesTranslationLanguage", "es")
            page.uncheck("#seriesAiAuto")
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/translation-jobs")) as resp_info:
                page.click("#seriesAiGenerate")
            response = resp_info.value
            assert response.status == 202
            job_id = response.json()["job"]["id"]
            self.wait_for_job(uid, job_id, "draft")
            page.click("#seriesAiRefresh")
            page.click("#seriesAiDraft")
            assert page.locator("#translatedTitle").input_value() == "es: Gallery program"
            assert page.get_by_label("Navigation translation: Schedule", exact=True).input_value() == "es: Schedule"
            assert page.get_by_label("FAQ question translation: Where?", exact=True).input_value() == "es: Where?"
            assert page.get_by_label("FAQ answer translation: The gallery", exact=True).input_value() == "es: The gallery"
            page.get_by_label("FAQ answer translation: The gallery", exact=True).fill("La galería")
            assert not page.is_checked("#translationPublished")
            page.check("#translationPublished")
            with page.expect_response(lambda r: r.request.method == "PUT") as put_info:
                page.get_by_role("button", name="Save Event Series", exact=True).click()
            assert put_info.value.status == 200
            fresh_page = browser.new_page()
            fresh_page.goto(base + "/event-series/gallery-program?lang=es")
            assert fresh_page.get_by_role("heading", level=1).inner_text() == "es: Gallery program"
            schedule_link = fresh_page.get_by_role("link", name="es: Schedule", exact=True)
            assert schedule_link.get_attribute("href") == "https://venue.example.test/schedule"
            fresh_page.get_by_text("es: Where?", exact=True).click()
            assert fresh_page.get_by_text("es: Where?", exact=True).is_visible()
            assert fresh_page.get_by_text("La galería", exact=True).is_visible()

            saved = self.client.get(f'/api/event-series/{uid}', headers=self.auth).get_json()
            settings = saved["settings"]
            settings["presentation"]["navigation"][0]["label"] = "Tickets"
            settings["presentation"]["navigation"][0]["href"] = "https://venue.example.test/schedule"
            edit = self.client.put(
                f'/api/event-series/{uid}',
                json=dict(
                    revision=saved["revision"],
                    slug=saved["slug"],
                    settings=settings,
                ),
                headers=self.auth,
            )
            self.assertEqual(edit.status_code, 200)
            fresh_page.reload()
            assert fresh_page.get_by_role("link", name="Tickets", exact=True).get_attribute("href") == "https://venue.example.test/schedule"
            assert fresh_page.get_by_role("link", name="es: Schedule", exact=True).count() == 0
            fresh_page.get_by_text("es: Where?", exact=True).click()
            assert fresh_page.get_by_text("es: Where?", exact=True).is_visible()
            assert fresh_page.get_by_text("La galería", exact=True).is_visible()

    def test_translation_job_superseded_by_concurrent_edit(self):
        created = self.client.post(
            '/api/event-series', json={}, headers=self.auth
        ).get_json()
        uid = created['id']
        response = self.client.put(
            f'/api/event-series/{uid}',
            headers=self.auth,
            json=dict(
                revision=created['revision'],
                slug='edited-film',
                settings=dict(title='First title', visibility='public'),
            ),
        )
        self.assertEqual(response.status_code, 200)
        revision = response.get_json()['revision']

        started = Event()
        release = Event()

        def fake_model(cues, language, stopping):
            started.set()
            release.wait(5)
            return [dict(text='STALE ' + cue['text']) for cue in cues]

        self.model.side_effect = fake_model

        response = self.client.post(
            f'/api/event-series/{uid}/translation-jobs',
            headers=self.auth,
            json=dict(revision=revision, language='fr'),
        )
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()['job']['id']

        self.assertTrue(started.wait(5))
        try:
            response = self.client.put(
                f'/api/event-series/{uid}',
                headers=self.auth,
                json=dict(
                    revision=revision,
                    slug='edited-film',
                    settings=dict(title='Revised title', visibility='public'),
                ),
            )
            self.assertEqual(response.status_code, 200)
        finally:
            release.set()

        self.wait_for_job(uid, job_id, 'superseded')

        response = self.client.get('/event-series/edited-film?lang=fr')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Revised title', body)
        self.assertNotIn('STALE', body)
