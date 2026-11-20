"""Return to an existing registration by proving control of its mailbox."""

import hashlib
import hmac
import json
import re
import secrets
import time

from flask import abort, request

import mail_relay
from library_registration import EMAIL, meets_current_requirements

SCHEMA = """
CREATE TABLE IF NOT EXISTS registration_sign_on(
 id TEXT PRIMARY KEY, scope TEXT NOT NULL, address_key TEXT NOT NULL,
 proof TEXT NOT NULL, expires REAL NOT NULL, attempts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS registration_sign_on_expiry ON registration_sign_on(expires);
CREATE TABLE IF NOT EXISTS registration_sign_on_limit(
 bucket TEXT PRIMARY KEY, expires REAL NOT NULL, count INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS registration_sign_on_limit_expiry ON registration_sign_on_limit(expires);
"""
CODE_LIFETIME = 600
SEND_INTERVAL = 60
MAX_TRIES = 6


class SignOn:
    def __init__(self, tx, secret, origin, delivery):
        self.tx = tx
        self.key = secret.encode("utf-8")
        self.relay = mail_relay.configured(origin)
        self.delivery = delivery

    def digest(self, *values):
        encoded = json.dumps(values, separators=(",", ":")).encode("utf-8")
        return hmac.new(self.key, encoded, hashlib.sha256).hexdigest()

    def recipient(self, con, kind, subject, occurrence, address_key, form):
        if kind == "event":
            row = con.execute(
                "SELECT email,answers FROM live_event_registrant WHERE event=? AND opens=? AND email_key=?",
                (subject, occurrence, address_key),
            ).fetchone()
        else:
            row = con.execute("SELECT email,answers FROM viewer_registrant WHERE kind=? AND subject=? AND email_key=?",
                              (kind, subject, address_key)).fetchone()
        if row and meets_current_requirements(form, json.loads(row["answers"])):
            return row["email"]
        return None

    def send(self, kind, subject, occurrence, form, scope, challenge, address_key):
        with self.tx() as con:
            address = self.recipient(con, kind, subject, occurrence, address_key, form)
            if not address:
                return
            code = f"{secrets.randbelow(100_000_000):08}"
            con.execute("INSERT INTO registration_sign_on VALUES(?,?,?,?,?,0)",
                        (challenge, scope, address_key, self.digest("registration code", challenge, code),
                         time.time() + CODE_LIFETIME))
        mail_relay.deliver(self.relay, address, "Your CuePorch registration code",
                           f"Enter this code to open your CuePorch registration: {code}\n\n"
                           "The code works once and expires in 10 minutes. "
                           "If you did not ask for it, you can ignore this message.\n")

    def limit(self, con, bucket, now, interval, maximum):
        count = con.execute("""INSERT INTO registration_sign_on_limit VALUES(?,?,1)
            ON CONFLICT(bucket) DO UPDATE SET count=count+1 RETURNING count""", (bucket, now + interval)).fetchone()[0]
        return count <= maximum

    def attempt(self, con, kind, subject, form, scope, occurrence=None):
        if not self.relay:
            abort(404)
        action = request.form.get("registration_action")
        if action == "open":
            return False, {"step": "email"}, None
        if action == "back":
            return False, None, None
        now = time.time()
        scope = self.digest("registration scope", kind, subject, scope)
        con.execute("DELETE FROM registration_sign_on WHERE expires<=?", (now,))
        con.execute("DELETE FROM registration_sign_on_limit WHERE expires<=?", (now,))
        if action == "send":
            address = request.form.get("return_email", "").strip()
            if len(address) > 254 or not EMAIL.fullmatch(address):
                return False, {"step": "email"}, "Enter the email you used to register."
            address_key = address.casefold()
            source = self.digest("registration sender", request.remote_addr or "")
            target = self.digest("registration recipient", address_key)
            if not self.limit(con, source, now, CODE_LIFETIME, 120):
                return False, {"step": "email"}, "Too many requests. Try again in a few minutes."
            challenge = secrets.token_urlsafe(32)
            if self.limit(con, target, now, SEND_INTERVAL, 1):
                self.delivery.submit(
                    lambda email: self.send(kind, subject, occurrence, form, scope, challenge, email), address_key,
                )
            return False, {"step": "code", "challenge": challenge}, None
        if action != "verify":
            abort(400)
        challenge = request.form.get("return_challenge", "")
        code = request.form.get("return_code", "")
        row = None
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge) and re.fullmatch(r"[0-9]{8}", code):
            row = con.execute("""UPDATE registration_sign_on SET attempts=attempts+1
                WHERE id=? AND scope=? AND attempts<? RETURNING address_key,proof""",
                (challenge, scope, MAX_TRIES)).fetchone()
        if (row and hmac.compare_digest(row["proof"], self.digest("registration code", challenge, code))
                and self.recipient(con, kind, subject, occurrence, row["address_key"], form)):
            con.execute("DELETE FROM registration_sign_on WHERE id=?", (challenge,))
            return True, None, None
        return False, {"step": "code", "challenge": challenge[:43]}, "The code was invalid or expired."
