"""Folder-scoped Slack delivery without passing video access to the channel."""

import base64
import hmac
import json
import re
from threading import Event, Thread
from time import time
from urllib.parse import urlencode
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock, Timeout
from flask import abort, request
import requests

from browser_account_security import json_mutation, private, same_origin, secure_site
from library_analytics import source_origin
from workspace_oauth import Config, Connection, provider_request, response_object


SCHEMA = """
CREATE TABLE IF NOT EXISTS folder_slack(
 folder TEXT PRIMARY KEY, revision TEXT NOT NULL, address BLOB NOT NULL, topics TEXT NOT NULL, channel TEXT);
CREATE TABLE IF NOT EXISTS folder_slack_notice(
 id INTEGER PRIMARY KEY, folder TEXT NOT NULL, revision TEXT NOT NULL, work TEXT NOT NULL, topic TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued', created INTEGER NOT NULL, due INTEGER NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS folder_slack_due ON folder_slack_notice(state,due);
CREATE INDEX IF NOT EXISTS folder_slack_folder ON folder_slack_notice(folder);
"""
TOPICS = {
    "upload": "A video upload is ready.",
    "comment": "A reviewer left a comment.",
    "approval": "A video version was approved.",
}
WEBHOOK = re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9]{2,40}/[A-Za-z0-9]{2,40}/[A-Za-z0-9_-]{16,100}")
RETRY_SECONDS = (10, 30, 120, 300)
OAUTH = Config("slack", "Slack", "https://slack.com/oauth/v2/authorize", "https://slack.com/api/oauth.v2.access",
               "", "incoming-webhook", pkce=False)


def migrate(con):
    columns = {row[1] for row in con.execute("PRAGMA table_info(folder_slack)")}
    if "channel" not in columns:
        con.execute("ALTER TABLE folder_slack ADD COLUMN channel TEXT")


def current_settings(con, folder):
    if not con.execute("""SELECT 1 FROM library_folder WHERE id=? AND NOT EXISTS(
        SELECT 1 FROM library_deleted_item WHERE kind='folder' AND item=?)""", (folder, folder)).fetchone():
        abort(404, "The selected Library folder is unavailable.")
    return con.execute("SELECT * FROM folder_slack WHERE folder=?", (folder,)).fetchone()


def check_revision(row, revision):
    if revision != (row["revision"] if row else None):
        abort(409, "The Slack settings changed. Reopen this folder's settings.")


def save(con, folder, address, topics, channel):
    revision = uuid4().hex
    con.execute("INSERT OR REPLACE INTO folder_slack(folder,revision,address,topics,channel) VALUES(?,?,?,?,?)",
                (folder, revision, address, json.dumps(topics), channel))
    con.execute("UPDATE folder_slack_notice SET state='canceled' WHERE folder=? AND state='queued'", (folder,))
    return con.execute("SELECT * FROM folder_slack WHERE folder=?", (folder,)).fetchone()


def selected_topics(body):
    topics = body.get("topics")
    if not (isinstance(topics, list) and 1 <= len(topics) <= len(TOPICS)
            and all(isinstance(topic, str) and topic in TOPICS for topic in topics)):
        abort(400, "Choose the Slack notices you want to receive.")
    return sorted(set(topics))


class ChannelConsent(Connection):
    def __init__(self, tx, operator, webhook_cipher):
        super().__init__(OAUTH, tx, operator)
        self.webhook_cipher = webhook_cipher

    def exchange(self, fields, previous_refresh=None):
        with provider_request("POST", self.config.token_url, data=fields | self.client_fields()) as response:
            receipt = response_object(response)
        incoming = receipt.get("incoming_webhook")
        scopes = receipt.get("scope")
        if (response.status_code != 200 or receipt.get("ok") is not True or receipt.get("token_type") != "bot"
                or not isinstance(scopes, str) or "incoming-webhook" not in scopes.split(",")
                or not isinstance(incoming, dict)):
            abort(502, "Slack did not authorize a channel webhook. Try connecting it again.")
        address, channel = incoming.get("url"), incoming.get("channel")
        if not isinstance(address, str) or not WEBHOOK.fullmatch(address) or not isinstance(channel, str) or not 1 <= len(channel) <= 100:
            abort(502, "Slack returned an invalid channel webhook.")
        return address, channel

    def save_authorization(self, con, tokens, context):
        check_revision(current_settings(con, context["folder"]), context["revision"])
        address, channel = tokens
        save(con, context["folder"], self.webhook_cipher.encrypt(address.encode("ascii")), context["topics"], channel)

    def return_path(self, result, context):
        return "/operator/library?" + urlencode(dict(folder=context["folder"], slack=result))


def enqueue(con, work, topic):
    now = int(time())
    con.execute("""INSERT INTO folder_slack_notice(folder,revision,work,topic,created,due)
                   SELECT s.folder,s.revision,p.work,?,?,? FROM library_placement p
                   JOIN folder_slack s ON s.folder=p.folder
                   WHERE p.work=? AND EXISTS(SELECT 1 FROM json_each(s.topics) WHERE value=?)""",
                (topic, now, now, work, topic))


def send(cipher, job, origin):
    try:
        address = cipher.decrypt(job["address"]).decode("ascii")
    except (InvalidToken, UnicodeError):
        return 400, 0
    if not WEBHOOK.fullmatch(address):
        return 400, 0
    with requests.Session() as session:
        session.trust_env = False
        try:
            with session.post(address, json={
                "text": f"{TOPICS[job['topic']]} <{origin}/team/video/{job['work']}|Open in CuePorch>",
                "unfurl_links": False, "unfurl_media": False,
            }, timeout=(3, 6), allow_redirects=False, stream=True) as response:
                delay = response.headers.get("Retry-After", "")
                return response.status_code, int(delay) if re.fullmatch(r"[0-9]{1,6}", delay) else 0
        except requests.RequestException:
            return 503, 0


def deliver_next(tx, cipher, origin):
    with tx() as con:
        job = con.execute("""SELECT n.*,s.address,p.folder AS current_folder,
                             EXISTS(SELECT 1 FROM library_deleted_item d
                               WHERE (d.kind='work' AND d.item=n.work) OR (d.kind='folder' AND d.item=n.folder)) AS hidden
                             FROM folder_slack_notice n JOIN folder_slack s
                               ON s.folder=n.folder AND s.revision=n.revision
                             LEFT JOIN library_placement p ON p.work=n.work
                             WHERE n.state='queued' AND n.due<=? ORDER BY n.due,n.id LIMIT 1""",
                          (int(time()),)).fetchone()
        if not job:
            return False
        if job["hidden"] or job["current_folder"] != job["folder"]:
            con.execute("UPDATE folder_slack_notice SET state='canceled' WHERE id=?", (job["id"],))
            return True
    result, delay = send(cipher, job, origin)
    attempts = job["attempts"] + 1
    retry = (result == 429 or result >= 500) and attempts <= len(RETRY_SECONDS)
    due = int(time()) + max(delay, RETRY_SECONDS[attempts - 1]) if retry else job["due"]
    with tx() as con:
        con.execute("""UPDATE folder_slack_notice SET state=?,attempts=?,due=? WHERE id=? AND revision=?""",
                    ("delivered" if result == 200 else "queued" if retry else "failed",
                     attempts, due, job["id"], job["revision"]))
        con.execute("""DELETE FROM folder_slack_notice WHERE folder=? AND created<? AND state!='queued'""",
                    (job["folder"], int(time()) - 30 * 86400))
    return True


def register(app, tx, origin, staff, operator, database):
    configured = origin.startswith("https://")
    cipher = Fernet(base64.urlsafe_b64encode(hmac.digest(operator.encode(), b"CuePorch Slack webhooks", "sha256")))
    consent = ChannelConsent(tx, operator, cipher)
    lock = FileLock(str(database.with_suffix(".slack.lock")), timeout=0)
    stop, wake = Event(), Event()

    def worker():
        while not stop.is_set():
            try:
                with lock:
                    if deliver_next(tx, cipher, origin):
                        continue
            except Timeout:
                pass
            except Exception:
                app.logger.exception("Folder Slack delivery failed.")
            wake.wait(1)
            wake.clear()

    thread = None
    if configured:
        thread = Thread(target=worker, name="CuePorch folder Slack", daemon=True)
        thread.start()

    def close():
        stop.set()
        wake.set()
        if thread:
            thread.join(10)

    def clear_consent(con, folder):
        con.execute("""DELETE FROM workspace_integration_attempt WHERE provider='slack'
                       AND json_extract(context,'$.folder')=?""", (folder,))

    @app.post("/api/library/folders/<folder>/slack/connect")
    def connect_channel(folder):
        staff()
        secure = secure_site(origin)
        if not configured or not secure or source_origin("https://" + request.host) != origin:
            abort(409, "Use the configured workspace HTTPS address to connect Slack.")
        body = json_mutation(secure, "X-CuePorch-Slack")
        revision = body.get("revision")
        topics = selected_topics(body)
        with tx() as con:
            check_revision(current_settings(con, folder), revision)
        return consent.prepare(secure, origin, dict(folder=folder, revision=revision, topics=topics))

    @app.get(consent.callback)
    def slack_callback():
        secure = secure_site(origin)
        if not configured or not secure or source_origin("https://" + request.host) != origin:
            abort(403, "Use the configured workspace HTTPS address to connect Slack.")
        return consent.complete(secure, origin)

    @app.route("/api/library/folders/<folder>/slack", methods=("GET", "PUT", "DELETE"))
    def slack_settings(folder):
        staff()
        secure = secure_site(origin)
        same_origin(secure)
        body = json_mutation(secure, "X-CuePorch-Slack") if request.method in ("PUT", "DELETE") else None
        with tx() as con:
            row = current_settings(con, folder)
            if body is not None:
                if not configured or not secure:
                    abort(409, "Configure the workspace HTTPS address to connect Slack.")
                check_revision(row, body.get("revision"))
                if request.method == "DELETE":
                    con.execute("DELETE FROM folder_slack_notice WHERE folder=?", (folder,))
                    con.execute("DELETE FROM folder_slack WHERE folder=?", (folder,))
                    row = None
                else:
                    topics, address = selected_topics(body), body.get("webhook", "")
                    if (not isinstance(address, str) or (address and not WEBHOOK.fullmatch(address))
                            or (not address and not row)):
                        abort(400, "Enter the incoming webhook for the selected Slack channel.")
                    encrypted = cipher.encrypt(address.encode("ascii")) if address else row["address"]
                    row = save(con, folder, encrypted, topics, None if address else row["channel"])
                clear_consent(con, folder)
            receipts = con.execute("""SELECT topic,state,attempts FROM folder_slack_notice WHERE folder=?
                                    ORDER BY id DESC LIMIT 10""", (folder,)).fetchall()
        return private(dict(configured=configured, oauth_available=configured and consent.configured,
                            revision=row["revision"] if row else None, channel=row["channel"] if row else None,
                            topics=json.loads(row["topics"]) if row else [], deliveries=[dict(item) for item in receipts]))

    return close
