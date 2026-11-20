"""Folder-scoped Slack delivery without passing video access to the channel."""

import base64
import hmac
import json
import re
from threading import Event, Thread
from time import time
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock, Timeout
from flask import abort, request
import requests

from browser_account_security import json_mutation, private, same_origin, secure_site


SCHEMA = """
CREATE TABLE IF NOT EXISTS folder_slack(
 folder TEXT PRIMARY KEY, revision TEXT NOT NULL, address BLOB NOT NULL, topics TEXT NOT NULL);
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

    @app.route("/api/library/folders/<folder>/slack", methods=("GET", "PUT", "DELETE"))
    def slack_settings(folder):
        staff()
        secure = secure_site(origin)
        same_origin(secure)
        body = json_mutation(secure, "X-CuePorch-Slack") if request.method in ("PUT", "DELETE") else None
        with tx() as con:
            if not con.execute("""SELECT 1 FROM library_folder WHERE id=? AND NOT EXISTS(
                SELECT 1 FROM library_deleted_item WHERE kind='folder' AND item=?)""", (folder, folder)).fetchone():
                abort(404, "The selected Library folder is unavailable.")
            row = con.execute("SELECT * FROM folder_slack WHERE folder=?", (folder,)).fetchone()
            if body is not None:
                if not configured or not secure:
                    abort(409, "Configure the workspace HTTPS address to connect Slack.")
                if body.get("revision") != (row["revision"] if row else None):
                    abort(409, "The Slack settings changed. Reopen this folder's settings.")
                if request.method == "DELETE":
                    con.execute("DELETE FROM folder_slack_notice WHERE folder=?", (folder,))
                    con.execute("DELETE FROM folder_slack WHERE folder=?", (folder,))
                    return private(dict(configured=True, revision=None, topics=[], deliveries=[]))
                topics, address = body.get("topics"), body.get("webhook", "")
                if not (isinstance(topics, list) and 1 <= len(topics) <= len(TOPICS)
                        and all(isinstance(topic, str) and topic in TOPICS for topic in topics)):
                    abort(400, "Choose the Slack notices you want to receive.")
                if not isinstance(address, str) or (address and not WEBHOOK.fullmatch(address)):
                    abort(400, "Enter the incoming webhook for the selected Slack channel.")
                if not address and not row:
                    abort(400, "Enter the incoming webhook for the selected Slack channel.")
                revision = uuid4().hex
                encrypted = cipher.encrypt(address.encode("ascii")) if address else row["address"]
                con.execute("INSERT OR REPLACE INTO folder_slack VALUES(?,?,?,?)",
                            (folder, revision, encrypted, json.dumps(sorted(set(topics)))))
                con.execute("UPDATE folder_slack_notice SET state='canceled' WHERE folder=? AND state='queued'", (folder,))
                row = con.execute("SELECT * FROM folder_slack WHERE folder=?", (folder,)).fetchone()
            receipts = con.execute("""SELECT topic,state,attempts FROM folder_slack_notice WHERE folder=?
                                    ORDER BY id DESC LIMIT 10""", (folder,)).fetchall()
        return private(dict(configured=configured, revision=row["revision"] if row else None,
                            topics=json.loads(row["topics"]) if row else [], deliveries=[dict(item) for item in receipts]))

    return close
