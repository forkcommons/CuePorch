"""Optional email second-factor sign-in for a verified browser account."""

import hmac
import secrets
from time import time

from flask import abort, request
from werkzeug.security import check_password_hash

from browser_account_security import TOKEN, password, private, token_hash
from account_email import CODE, SEND_INTERVAL, VERIFY_SECONDS
import mail_relay


SCHEMA = """
CREATE TABLE IF NOT EXISTS account_email_factor(
 scope TEXT NOT NULL, account TEXT NOT NULL,
 PRIMARY KEY(scope,account));
CREATE TABLE IF NOT EXISTS account_email_factor_challenge(
 scope TEXT NOT NULL, account TEXT NOT NULL, address TEXT NOT NULL, revision TEXT NOT NULL,
 purpose TEXT NOT NULL, password_version TEXT NOT NULL, proof BLOB NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
 delivered INTEGER NOT NULL, PRIMARY KEY(scope,account));
"""


def is_enabled(con, scope, uid):
    return con.execute("SELECT 1 FROM account_email_factor WHERE scope=? AND account=?",
                       (scope, uid)).fetchone() is not None


class EmailFactor:
    def __init__(self, email, authenticator):
        self.scope = email.scope
        self.email = email
        self.authenticator = authenticator
        self.key = hmac.digest(email.key, b"CuePorch email sign-in", "sha256")

    def enabled(self, con, uid):
        return is_enabled(con, self.scope, uid)

    def forget(self, con, uid):
        con.execute("DELETE FROM account_email_factor WHERE scope=? AND account=?", (self.scope, uid))
        con.execute("DELETE FROM account_email_factor_challenge WHERE scope=? AND account=?", (self.scope, uid))

    def challenge_proof(self, ticket, code):
        return hmac.digest(self.key, (ticket + ":" + code).encode(), "sha256")

    def prove(self, con, actor, body, purpose="login"):
        uid = actor["id"]
        value = body.get("code")
        if self.authenticator.use_recovery_code(con, uid, value):
            con.execute("DELETE FROM account_email_factor_challenge WHERE scope=? AND account=?", (self.scope, uid))
            return
        ticket = body.get("email_ticket")
        if not isinstance(ticket, str) or not TOKEN.fullmatch(ticket) or not isinstance(value, str) or not CODE.fullmatch(value):
            abort(422, "Use the security code from this sign-in email or an unused recovery code.")
        pending = con.execute("""SELECT * FROM account_email_factor_challenge WHERE scope=? AND account=?
                                AND address=? AND revision=? AND purpose=? AND password_version=? AND expires>? AND delivered=1""",
                              (self.scope, uid, actor["email"], token_hash(ticket), purpose,
                               token_hash(actor["password_hash"]), time())).fetchone()
        if not pending or not hmac.compare_digest(pending["proof"], self.challenge_proof(ticket, value)):
            abort(422, "The security code did not match or has expired.")
        con.execute("DELETE FROM account_email_factor_challenge WHERE scope=? AND account=?", (self.scope, uid))

    def prepare(self, con, actor, purpose="login"):
        if not self.email.config or not self.email.verified(con, actor):
            return None
        now = time()
        con.execute("DELETE FROM account_email_factor_challenge WHERE expires<=?", (now,))
        previous = con.execute("""SELECT created FROM account_email_factor_challenge
                                  WHERE scope=? AND account=?""", (self.scope, actor["id"])).fetchone()
        if previous and now - previous["created"] < SEND_INTERVAL:
            return None
        ticket, code = secrets.token_urlsafe(32), f"{secrets.randbelow(100000000):08d}"
        con.execute("INSERT OR REPLACE INTO account_email_factor_challenge VALUES(?,?,?,?,?,?,?,?,?,0)",
                    (self.scope, actor["id"], actor["email"], token_hash(ticket), purpose, token_hash(actor["password_hash"]),
                     self.challenge_proof(ticket, code), now, now + VERIFY_SECONDS))
        return ticket, code, purpose

    def deliver(self, tx, actor, prepared):
        ticket = None
        sent = False
        if prepared:
            ticket, code, purpose = prepared
            action = "finish signing in" if purpose == "login" else "turn off email sign-in"
            sent = mail_relay.deliver(self.email.config, actor["email"], "Your CuePorch security code",
                                      f"Enter this code to {action}.\n\n{code}\n\n"
                                      "It expires in ten minutes and works once. Do not share it.\n")
            if sent:
                with tx() as con:
                    sent = bool(con.execute("""UPDATE account_email_factor_challenge SET delivered=1
                        WHERE scope=? AND account=? AND revision=?""",
                        (self.scope, actor["id"], token_hash(ticket))).rowcount)
        return private(dict(authenticator_required=True, method="email", email_ticket=ticket if sent else None,
                            sent=sent), 428)

    def check(self, con, actor, body):
        """Return a delivery request only if sign-in still needs an email challenge."""
        if not self.enabled(con, actor["id"]):
            self.authenticator.check(con, actor["id"], body.get("code"))
            return False, None
        if body.get("resend") is True or not body.get("code"):
            return True, self.prepare(con, actor)
        self.prove(con, actor, body)
        return False, None


def register(app, tx, email, authenticator, scheme, mutation, account_for, throttle, rotate, cookie, audit=None):
    factor = EmailFactor(email, authenticator)

    @app.route(f"/api/{email.scope}/email-factor", methods=("GET", "PUT", "DELETE"),
               endpoint=email.scope + "_email_factor")
    def email_factor_settings():
        secure = scheme()
        if request.method in ("GET", "HEAD"):
            with tx() as con:
                account = account_for(con, secure)
                return private(dict(enabled=factor.enabled(con, account["id"]), available=bool(email.config),
                                    verified=email.verified(con, account)))
        body = mutation(secure)
        with tx() as con:
            actor = account_for(con, secure)
            throttle(con, "email-factor-" + request.method, actor["id"])
        if not check_password_hash(actor["password_hash"], password(body)):
            abort(401, "The current password did not match.")
        with tx() as con:
            current = account_for(con, secure)
            if current["id"] != actor["id"] or current["password_hash"] != actor["password_hash"]:
                abort(409, "Your account changed. Try again.")
            uid = current["id"]
            enabled = factor.enabled(con, uid)
            if request.method == "PUT":
                if enabled or authenticator.state(con, uid):
                    abort(409, "Turn off the current sign-in method before enabling email codes.")
                if not email.config or not email.verified(con, current):
                    abort(409, "Verify your email before switching on email sign-in.")
                con.execute("INSERT INTO account_email_factor VALUES(?,?)", (email.scope, uid))
                result = dict(enabled=True, recovery_codes=authenticator.issue_recovery_codes(con, uid))
            else:
                if not enabled:
                    abort(409, "Email sign-in is already off.")
                factor.prove(con, current, body, "disable")
                factor.forget(con, uid)
                authenticator.forget(con, uid)
                result = dict(enabled=False)
            token = rotate(con, uid, secure)
            if audit:
                audit(con, current, result["enabled"])
        return cookie(private(result), secure, token)

    @app.post(f"/api/{email.scope}/email-factor/code", endpoint=email.scope + "_email_factor_code")
    def send_email_factor_code():
        secure = scheme()
        body = mutation(secure)
        with tx() as con:
            account = account_for(con, secure)
            throttle(con, "email-factor-send", account["id"])
        if not check_password_hash(account["password_hash"], password(body)):
            abort(401, "The current password did not match.")
        with tx() as con:
            current = account_for(con, secure)
            if (current["id"] != account["id"] or current["password_hash"] != account["password_hash"]
                    or not factor.enabled(con, current["id"])):
                abort(409, "Email sign-in settings changed. Try again.")
            prepared = factor.prepare(con, current, "disable")
        delivery = factor.deliver(tx, current, prepared)
        delivery.status_code = 202
        return delivery

    return factor
