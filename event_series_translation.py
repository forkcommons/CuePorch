"""Generate Event Series translations with the configured local TextFlowKit runtime."""

import json
import time
import uuid

from flask import abort, request
from werkzeug.exceptions import HTTPException

import caption_adapter
import event_landing_translation
import event_series_locale
import local_model
import team_ai_policy
from browser_account_security import private
from library_interactivity import text_field

LIMITS = {"title": 128, "description": 5000, "date_label": 100, "timezone_label": 100}


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_series_translation (
    id        TEXT PRIMARY KEY,
    series    TEXT NOT NULL,
    revision  TEXT NOT NULL,
    language  TEXT NOT NULL,
    member    TEXT,
    apply_now INTEGER NOT NULL DEFAULT 1,
    state     TEXT NOT NULL,
    result    TEXT,
    error     TEXT,
    created   REAL NOT NULL,
    UNIQUE (series, revision, language)
);
"""


def public_job(row):
    state = (
        "waiting"
        if row["state"] == "queued" and row["gate"] == "waiting"
        else row["state"]
    )
    result = json.loads(row["result"]) if row["result"] else None
    return {
        "id": row["id"],
        "language": row["language"],
        "revision": row["revision"],
        "state": state,
        "apply_now": bool(row["apply_now"]),
        "result": result,
        "error": row["error"],
    }


def update_job(con, job, state, result=None, error=None):
    if result is not None:
        result = json.dumps(result)
    con.execute(
        "UPDATE live_series_translation SET state=?, result=?, error=? WHERE id=?",
        (state, result, error, job['id']),
    )


def checked_series(con, job):
    reason = team_ai_policy.restriction(con, 'translation', job['member'])
    if reason:
        update_job(con, job, 'blocked', error=reason)
        return None
    page = con.execute(
        "SELECT * FROM live_series_page WHERE id=? AND revision=?",
        (job['series'], job['revision']),
    ).fetchone()
    if page is None:
        update_job(con, job, 'superseded', error='series revision is not current')
        return None
    return page


def translation_fields(settings, language, stopping):
    presentation = settings.get('presentation', {})
    source = dict(
        title=settings['title'],
        description=settings['description'],
        date_label=presentation.get('date_label', ''),
        timezone_label=presentation.get('timezone_label', ''),
    )
    result = dict(published=False, **dict.fromkeys(source, ''))
    records = [(None, key, text, LIMITS[key]) for key, text in source.items() if text]
    for group, (collection_key, field, limit) in event_series_locale.CONTENT_FIELDS.items():
        records.extend(
            (group, field, text, limit)
            for text in dict.fromkeys(item[field] for item in presentation.get(collection_key, []))
        )
    items = caption_adapter.translate_cues(
        [dict(text=text) for _, _, text, _ in records], language, stopping
    )
    content_entries = {}
    for (group, key, source_text, limit), item in zip(records, items, strict=True):
        if not isinstance(item, dict) or not isinstance(item.get('text'), str) or not item['text'].strip():
            raise caption_adapter.CaptionError('The translation was incomplete.')
        text = text_field(item['text'], 'a translated ' + key, limit, required=True)
        if group is None:
            result[key] = text
        else:
            content_entries.setdefault(group, []).append(dict(source=source_text, text=text))
    if content_entries:
        result['content'] = content_entries
    return result


def checked_landing(con, job):
    reason = team_ai_policy.restriction(con, 'translation', job['member'])
    if reason:
        update_job(con, job, 'blocked', error=reason)
        return None
    source = event_landing_translation.read_source(
        con, job['series'].removeprefix('event:')
    )
    if source is None or source['revision'] != job['revision']:
        update_job(con, job, 'superseded', error='landing source changed')
        return None
    return source


def process(tx, save_translation, stopping):
    with tx() as con:
        job = con.execute(
            """SELECT j.*, a.status AS gate FROM live_series_translation j
               JOIN action a ON a.id=j.id WHERE j.state IN ('queued','running')
               AND a.status IN ('eligible','rejected') ORDER BY j.created LIMIT 1"""
        ).fetchone()
        if job is None:
            return
        if job['gate'] == 'rejected':
            update_job(con, job, 'rejected')
            return
        is_landing = job['series'].startswith('event:')
        checker = checked_landing if is_landing else checked_series
        source = checker(con, job)
        if source is None:
            return
        update_job(con, job, 'running')

    try:
        if is_landing:
            translated = event_landing_translation.translation_fields(
                source, job['language'], stopping
            )
        else:
            settings = json.loads(source['settings'])
            translated = translation_fields(settings, job['language'], stopping)
    except (HTTPException, OSError, ValueError):
        if not stopping.is_set():
            with tx() as con:
                update_job(con, job, 'failed', error='Translation could not be generated.')
        return

    if stopping.is_set():
        return

    with tx() as con:
        source = checker(con, job)
        if not source:
            return
        if job['apply_now']:
            if is_landing:
                event_landing_translation.save_translation(
                    con, source, job['language'], translated
                )
            else:
                save_translation(con, source, settings, job['language'], translated)
            update_job(con, job, 'saved')
        else:
            update_job(con, job, 'draft', result=translated)


def register(app, tx, ai_actor, action):
    @app.route('/api/event-series/<uid>/translation-jobs', methods=['GET', 'POST'])
    @app.route('/api/live-events/<uid>/landing/translation-jobs',
               methods=['GET', 'POST'], defaults={'landing': True})
    def event_series_translation_jobs(uid, landing=False):
        with tx() as con:
            _, member = ai_actor(con)
            scope = 'event:' + uid if landing else uid
            if landing:
                row = event_landing_translation.read_source(con, uid)
            else:
                row = con.execute('SELECT * FROM live_series_page WHERE id=?', (uid,)).fetchone()
            if row is None:
                abort(404)
            expected_revision = row['landing_revision'] if landing else row['revision']

            def serialize(item):
                job = public_job(item)
                if landing:
                    current = item['revision'] == row['revision']
                    job['revision'] = expected_revision if current else None
                    if not current and job['state'] == 'draft':
                        job.update(state='superseded', result=None, error='landing source changed')
                return job

            if request.method in ("GET", "HEAD"):
                rows = con.execute(
                    "SELECT t.*, a.status AS gate FROM live_series_translation t "
                    "JOIN action a ON a.id = t.id "
                    "WHERE t.series=? "
                    "ORDER BY t.created DESC LIMIT 10",
                    (scope,),
                ).fetchall()
                return private(dict(
                    jobs=[serialize(item) for item in rows],
                    available=bool(caption_adapter.translation_runtime()) and not team_ai_policy.restriction(
                        con, "translation", member
                    ),
                ))

            request.max_content_length = 4096
            body = request.get_json(silent=True)
            if not isinstance(body, dict):
                abort(400)

            if body.get('revision') != expected_revision:
                abort(409)

            settings = row['settings'] if landing else json.loads(row['settings'])
            loc = settings.get('localization', event_series_locale.DEFAULTS)
            base = loc['base']

            language = body.get('language')
            if not isinstance(language, str) or language not in event_series_locale.LANGUAGES:
                abort(400)
            if language == base:
                abort(400)

            apply_now = body.get('apply_now', True)
            if not isinstance(apply_now, bool):
                abort(400)

            denial = team_ai_policy.restriction(con, 'translation', member)
            if denial:
                abort(403, description=denial)

            if not caption_adapter.translation_runtime():
                abort(409, "Configure TextFlowKit and a local translation model first.")

            existing = con.execute(
                "SELECT t.*, a.status AS gate FROM live_series_translation t "
                "JOIN action a ON a.id = t.id "
                "WHERE t.series=? AND t.revision=? AND t.language=?",
                (scope, row['revision'], language),
            ).fetchone()
            if existing is not None:
                return private(dict(job=serialize(existing)), 202)

            job_id = uuid.uuid4().hex
            identity = {'event': uid} if landing else {'series': uid}
            title = row['title'] if landing else settings['title']
            action(con, job_id, 'translation', dict(title=title, language=language) | identity)
            con.execute(
                "INSERT INTO live_series_translation "
                "(id, series, revision, language, member, apply_now, state, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    scope,
                    row['revision'],
                    language,
                    member,
                    apply_now,
                    'queued',
                    time.time(),
                ),
            )
            new_row = con.execute(
                "SELECT t.*, a.status AS gate FROM live_series_translation t "
                "JOIN action a ON a.id = t.id "
                "WHERE t.id=?",
                (job_id,),
            ).fetchone()
            return private(dict(job=serialize(new_row)), 202)


def start(app, root, tx, save_translation):
    lock_path = root / ".event-series-translation.lock"
    return local_model.start_worker(
        app,
        lock_path,
        "event_series_translation",
        lambda stopping: process(tx, save_translation, stopping),
        ready=caption_adapter.translation_runtime,
    )
