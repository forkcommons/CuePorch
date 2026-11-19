"""Deliver review notifications through the browser's Web Push service."""

import base64
import binascii
import json
import os
import re
import time
from threading import Thread
from urllib.parse import urlsplit

import http_ece
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from filelock import FileLock, Timeout
from flask import abort
from py_vapid import Vapid

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_push_key (
 id INTEGER PRIMARY KEY CHECK(id=1), private TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_push_subscription (
 link TEXT NOT NULL, actor TEXT NOT NULL, endpoint TEXT NOT NULL,
 p256dh TEXT NOT NULL, auth TEXT NOT NULL, PRIMARY KEY(link,actor));
CREATE TABLE IF NOT EXISTS review_push_job (
 link TEXT NOT NULL, version TEXT NOT NULL, actor TEXT NOT NULL,
 comment TEXT NOT NULL, due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(link,version,actor,comment));
"""


def contact():
    return os.environ.get("CUEPORCH_PUSH_CONTACT", "").strip() or None


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decoded(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise ValueError("Use a browser-generated push subscription.")
    bare = value.rstrip("=")
    return base64.urlsafe_b64decode(bare + "=" * (-len(bare) % 4))


def key(con):
    row = con.execute("SELECT private FROM review_push_key WHERE id=1").fetchone()
    vapid = Vapid.from_string(row[0]) if row else Vapid()
    if not row:
        vapid.generate_keys()
        secret = vapid.private_key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        con.execute("INSERT INTO review_push_key VALUES (1,?)", (encoded(secret),))
    return vapid


def endpoint_origin(endpoint):
    if (
        not isinstance(endpoint, str)
        or not 12 <= len(endpoint) <= 2048
        or any(ord(char) <= 32 for char in endpoint)
    ):
        raise ValueError("Use a browser-generated push endpoint.")
    parts = urlsplit(endpoint)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.fragment
    ):
        raise ValueError("Review notifications require an HTTPS push service.")
    origin = "https://" + parts.netloc.lower()
    configured = os.environ.get("CUEPORCH_PUSH_SERVICE_ORIGINS", "").split()
    if origin in configured:
        return origin
    if parts.port in (None, 443) and (
        parts.hostname in ("fcm.googleapis.com", "updates.push.services.mozilla.com")
        or parts.hostname.endswith((".push.apple.com", ".notify.windows.com"))
    ):
        return origin
    raise ValueError(
        "The browser's push service has not been configured for this site."
    )


def settings(con, uid, actor):
    if not actor or not contact():
        return {"enabled": False}
    public = key(con).public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    subscribed = con.execute(
        "SELECT 1 FROM review_push_subscription WHERE link=? AND actor=?", (uid, actor)
    ).fetchone()
    return {"enabled": True, "key": encoded(public), "subscribed": bool(subscribed)}


def subscribe(con, uid, actor, body):
    if not actor or not contact():
        abort(403)
    endpoint = body.get("endpoint") if isinstance(body, dict) else None
    keys = body.get("keys") if isinstance(body, dict) else None
    if not isinstance(keys, dict):
        abort(400, "Use a browser-generated push subscription.")
    try:
        endpoint_origin(endpoint)
        p256dh = decoded(keys.get("p256dh"))
        auth = decoded(keys.get("auth"))
        if len(p256dh) != 65 or len(auth) != 16:
            raise ValueError("Use a browser-generated push subscription.")
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    except (ValueError, binascii.Error):
        abort(400, "Use a valid subscription from a configured browser push service.")
    con.execute(
        """INSERT INTO review_push_subscription VALUES (?,?,?,?,?)
           ON CONFLICT(link,actor) DO UPDATE SET endpoint=excluded.endpoint,
           p256dh=excluded.p256dh,auth=excluded.auth""",
        (uid, actor, endpoint, encoded(p256dh), encoded(auth)),
    )


def unsubscribe(con, uid, actor):
    con.execute(
        "DELETE FROM review_push_subscription WHERE link=? AND actor=?", (uid, actor)
    )
    con.execute("DELETE FROM review_push_job WHERE link=? AND actor=?", (uid, actor))


def forget(con, uid):
    con.execute("DELETE FROM review_push_subscription WHERE link=?", (uid,))
    con.execute("DELETE FROM review_push_job WHERE link=?", (uid,))


def enqueue(con, uid, version, comment):
    con.execute(
        """INSERT OR IGNORE INTO review_push_job (link,version,actor,comment,due)
           SELECT n.link,n.version,n.actor,n.comment,? FROM review_notice n
           JOIN review_push_subscription s ON s.link=n.link AND s.actor=n.actor
           WHERE n.link=? AND n.version=? AND n.comment=?""",
        (time.time(), uid, version, comment),
    )


def deliver(item, vapid, sender):
    origin = endpoint_origin(item["endpoint"])
    payload = json.dumps(
        {
            "path": f"/review/{item['link']}?version={item['version']}#comment-{item['comment']}"
        }
    ).encode("utf8")
    encrypted = http_ece.encrypt(
        payload,
        private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=decoded(item["p256dh"]),
        auth_secret=decoded(item["auth"]),
        version="aes128gcm",
    )
    headers = vapid.sign({"aud": origin, "sub": sender, "exp": int(time.time()) + 3600})
    response = requests.post(
        item["endpoint"],
        data=encrypted,
        timeout=7,
        allow_redirects=False,
        headers={**headers, "Content-Encoding": "aes128gcm", "TTL": "900"},
    )
    return response.status_code


def start(app, tx, database):
    sender = contact()
    if not sender:
        return

    def process_job():
        now = time.time()
        with tx() as con:
            job = con.execute(
                """SELECT j.*,s.endpoint,s.p256dh,s.auth,r.enabled,r.expires,
                          p.mode,h.active AS held,c.resolved,t.resolved AS thread_resolved,
                          n.read, c.created
                   FROM review_push_job j
                   LEFT JOIN review_push_subscription s ON s.link=j.link AND s.actor=j.actor
                   LEFT JOIN review_link r ON r.id=j.link
                   LEFT JOIN review_notice_preferences p ON p.link=j.link AND p.actor=j.actor
                   LEFT JOIN review_notice n ON n.link=j.link AND n.version=j.version AND n.actor=j.actor AND n.comment=j.comment
                   LEFT JOIN review_comment c ON c.id=j.comment
                   LEFT JOIN review_comment t ON t.id=c.parent
                   LEFT JOIN case_hold h ON h.work=r.work AND h.active=1
                   WHERE j.due<=? ORDER BY j.due LIMIT 1""",
                (now,),
            ).fetchone()
            if not job:
                return False
            identity = (job["link"], job["version"], job["actor"], job["comment"])
            target = "link=? AND version=? AND actor=? AND comment=?"
            skip = (
                not job["endpoint"]
                or not job["enabled"]
                or job["expires"] <= now
                or job["mode"] == "off"
                or job["held"]
                or job["read"]
                or job["resolved"]
                or job["thread_resolved"]
                or now - (job["created"] or 0) > 900
            )
            if skip:
                con.execute(f"DELETE FROM review_push_job WHERE {target}", identity)
                return True
            vapid = key(con)
        try:
            code = deliver(job, vapid, sender)
        except requests.RequestException:
            code = 503
        except (ValueError, binascii.Error):
            code = 400
        with tx() as con:
            if code in (404, 410):
                unsubscribe(con, job["link"], job["actor"])
            elif (code == 429 or code >= 500) and job["attempts"] < 4:
                con.execute(
                    f"UPDATE review_push_job SET attempts=attempts+1,due=? WHERE {target}",
                    (now + 15 * (2 ** job["attempts"]), *identity),
                )
            else:
                con.execute(f"DELETE FROM review_push_job WHERE {target}", identity)
        return True

    def run():
        worker_lock = FileLock(str(database) + ".push-worker.lock", timeout=0)
        while database.is_file():
            try:
                with worker_lock:
                    if process_job():
                        continue
            except Timeout:
                pass
            except Exception:
                app.logger.warning("Review push worker could not process its next job")
            time.sleep(2)

    Thread(target=run, daemon=True, name="cueporch-review-push").start()
