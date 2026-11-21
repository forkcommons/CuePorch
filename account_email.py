"""Verify the current account address with a short-lived, single-use mail code."""

import hmac
import re
import secrets
from time import time
from uuid import uuid4

from flask import abort, request

from browser_account_security import private
import mail_relay


SCHEMA = """
CREATE TABLE IF NOT EXISTS account_verified_email(
 scope TEXT NOT NULL, account TEXT NOT NULL, address TEXT NOT NULL, verified REAL NOT NULL,
 PRIMARY KEY(scope,account));
CREATE TABLE IF NOT EXISTS account_email_verification(
 scope TEXT NOT NULL, account TEXT NOT NULL, address TEXT NOT NULL, revision TEXT NOT NULL,
 code_hash BLOB NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, delivered INTEGER NOT NULL,
 PRIMARY KEY(scope,account));
"""
CODE = re.compile(r"[0-9]{8}")
VERIFY_SECONDS = 10 * 60
SEND_INTERVAL = 45


class EmailVerification:
    def __init__(self, scope, signing_key, origin):
        self.scope = scope
        self.config = mail_relay.configured(origin)
        self.key = hmac.digest(signing_key.encode(), b"CuePorch email verification " + scope.encode(), "sha256")

    def verified(self, con, account):
        return con.execute("""SELECT 1 FROM account_verified_email WHERE scope=? AND account=? AND address=?""",
                           (self.scope, account["id"], account["email"])).fetchone() is not None

    def pending(self, con, account):
        return con.execute("""SELECT revision,code_hash FROM account_email_verification
                              WHERE scope=? AND account=? AND address=? AND expires>? AND delivered=1""",
                           (self.scope, account["id"], account["email"], time())).fetchone()

    def forget(self, con, account):
        con.execute("DELETE FROM account_verified_email WHERE scope=? AND account=?", (self.scope, account))
        con.execute("DELETE FROM account_email_verification WHERE scope=? AND account=?", (self.scope, account))
        con.execute("DELETE FROM account_password_reset WHERE scope=? AND account=?", (self.scope, account))

    def proof(self, revision, code):
        return hmac.digest(self.key, (revision + ":" + code).encode(), "sha256")

    def prepare(self, con, account):
        now = time()
        con.execute("DELETE FROM account_email_verification WHERE expires<=?", (now,))
        uid, address = account["id"], account["email"]
        previous = con.execute("""SELECT created FROM account_email_verification
                               WHERE scope=? AND account=?""", (self.scope, uid)).fetchone()
        if previous and now - previous["created"] < SEND_INTERVAL:
            abort(429, "Wait a moment before sending another verification code.")
        revision, code = uuid4().hex, f"{secrets.randbelow(100000000):08d}"
        con.execute("""INSERT OR REPLACE INTO account_email_verification VALUES(?,?,?,?,?,?,?,0)""",
                    (self.scope, uid, address, revision, self.proof(revision, code), now, now + VERIFY_SECONDS))
        return revision, code

    def deliver(self, tx, actor, revision, code):
        accepted = mail_relay.deliver(
            self.config, actor["email"], "Verify your CuePorch email",
            f"Enter this code in CuePorch account security to verify your email.\n\n{code}\n\n"
            "It expires in ten minutes and works once. If you did not request it, you can ignore this message.\n",
        )
        with tx() as con:
            parameters = (self.scope, actor["id"], revision)
            if accepted:
                con.execute("""UPDATE account_email_verification SET delivered=1
                               WHERE scope=? AND account=? AND revision=?""", parameters)
            else:
                # Keep the resend limit after a relay failure without leaving a usable code.
                con.execute("""UPDATE account_email_verification SET code_hash=?
                               WHERE scope=? AND account=? AND revision=?""", (bytes(32), *parameters))
        if not accepted:
            abort(503, "CuePorch could not send the verification email. Try again later.")

    def confirm(self, con, account, code):
        if not isinstance(code, str) or not CODE.fullmatch(code):
            abort(422, "Enter the eight-digit verification code.")
        uid, address = account["id"], account["email"]
        pending = self.pending(con, account)
        if not pending or not hmac.compare_digest(pending["code_hash"], self.proof(pending["revision"], code)):
            abort(422, "The verification code did not match or expired.")
        con.execute("DELETE FROM account_email_verification WHERE scope=? AND account=?", (self.scope, uid))
        con.execute("INSERT OR REPLACE INTO account_verified_email VALUES(?,?,?,?)", (self.scope, uid, address, time()))


def register(app, tx, scope, key, origin, scheme, mutation, account_for, throttle, audit=None):
    email = EmailVerification(scope, key, origin)

    @app.route(f"/api/{scope}/email-verification", methods=("GET", "POST", "PUT"),
               endpoint=scope + "_email_verification")
    def verify_address():
        secure = scheme()
        if request.method in ("GET", "HEAD"):
            with tx() as con:
                account = account_for(con, secure)
                return private(dict(address=account["email"], verified=email.verified(con, account),
                                    available=bool(email.config), pending=email.pending(con, account) is not None))
        body = mutation(secure)
        with tx() as con:
            actor = account_for(con, secure)
            throttle(con, "verify-email-" + request.method, actor["id"])
        if request.method == "POST":
            if not email.config:
                abort(503, "The operator has not configured account email delivery.")
            with tx() as con:
                actor = account_for(con, secure)
                if email.verified(con, actor):
                    return private(dict(address=actor["email"], verified=True))
                revision, code = email.prepare(con, actor)
            email.deliver(tx, actor, revision, code)
            return private(dict(address=actor["email"], sent=True), 202)
        with tx() as con:
            actor = account_for(con, secure)
            email.confirm(con, actor, body.get("code"))
            if audit:
                audit(con, actor)
            return private(dict(address=actor["email"], verified=True))

    return email
