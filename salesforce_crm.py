import os
import re

from flask import abort

from workspace_oauth import Config, Connection, check_provider, response_object

_MY_DOMAIN = re.compile(
    r"https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+my\.salesforce\.com/?",
    re.ASCII | re.IGNORECASE,
)


class SalesforceConnection(Connection):
    def __init__(self, tx, operator):
        candidate = os.environ.get("CUEPORCH_SALESFORCE_ORIGIN", "")
        self.origin = (
            candidate.rstrip("/").lower()
            if _MY_DOMAIN.fullmatch(candidate) and len(candidate[8:].rstrip("/")) <= 253
            else ""
        )
        oauth = self.origin + "/services/oauth2" if self.origin else ""
        super().__init__(Config(
            key="salesforce", name="Salesforce Sales Cloud",
            authorization_url=oauth + "/authorize",
            token_url=oauth + "/token",
            revoke_url=oauth + "/revoke" if oauth else "",
            scope="api refresh_token", nonexpiring_access=True,
        ), tx, operator)
        self.configured = bool(self.origin and self.configured)

    def scope_allowed(self, scope):
        scopes = set(scope.split()) if isinstance(scope, str) else set()
        return "api" in scopes and bool(scopes & {"refresh_token", "offline_access"})

    def exchange(self, fields, previous_refresh=None):
        self.require_configured()
        return super().exchange(fields, previous_refresh) | {"origin": self.origin}

    def tokens(self, row):
        saved = super().tokens(row)
        if not self.origin or not isinstance(saved, dict) or saved.get("origin") != self.origin:
            abort(409, "Reconnect Salesforce using the configured My Domain.")
        return saved


_CURSOR_RE = re.compile(r'[A-Za-z0-9_-]{1,256}')
_NEXT_RE = re.compile(r'/services/data/v64\.0/query/([A-Za-z0-9_-]{1,256})')
_ID_RE = re.compile(r'701(?:[A-Za-z0-9]{12}|[A-Za-z0-9]{15})')
_SOQL = 'SELECT Id, Name, IsActive FROM Campaign WHERE IsActive = true ORDER BY Name, Id'


def campaigns(connection, revision, cursor=None):
    connection.require_configured()
    base = connection.origin + '/services/data/v64.0/query'

    if cursor is None:
        target = base
        api_kwargs = {'params': {'q': _SOQL}}
    else:
        if not isinstance(cursor, str) or _CURSOR_RE.fullmatch(cursor) is None:
            abort(400)
        target = base + '/' + cursor
        api_kwargs = {}

    with connection.api(revision, 'GET', target, **api_kwargs) as response:
        check_provider(response)
        body = response_object(response)

    return _parse_campaigns(body, cursor)


def _parse_campaigns(body, cursor):
    if not isinstance(body, dict):
        abort(502)

    records = body.get('records')
    if not isinstance(records, list) or len(records) > 2000:
        abort(502)

    done = body.get('done')
    if not isinstance(done, bool):
        abort(502)

    next_url = body.get('nextRecordsUrl')

    items = []
    seen = set()
    for rec in records:
        if not isinstance(rec, dict):
            abort(502)

        cid = rec.get('Id')
        name = rec.get('Name')
        active = rec.get('IsActive')

        if not isinstance(cid, str) or _ID_RE.fullmatch(cid) is None:
            abort(502)
        if cid in seen:
            abort(502)
        seen.add(cid)

        if active is not True:
            abort(502)

        if not isinstance(name, str) or not name or len(name) > 255 or not name.isprintable():
            abort(502)

        items.append({'id': cid, 'name': name})

    extracted = None
    if done:
        if next_url is not None:
            abort(502)
    else:
        if not isinstance(next_url, str):
            abort(502)
        match = _NEXT_RE.fullmatch(next_url)
        if match is None:
            abort(502)
        extracted = match.group(1)
        if extracted == cursor:
            abort(502)

    return {'campaigns': items, 'next_cursor': extracted}
