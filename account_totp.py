"""Optional authenticator-based sign-in for viewer and team accounts."""

from base64 import b32decode, b32encode, urlsafe_b64encode
import hmac
import json
import re
import secrets
from time import time
from urllib.parse import quote, urlencode

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.twofactor.totp import TOTP
from flask import abort, render_template, request
from werkzeug.security import check_password_hash

from browser_account_security import password, private, workspace_session


SCHEMA = """
CREATE TABLE IF NOT EXISTS account_authenticator(
 scope TEXT NOT NULL, account TEXT NOT NULL, seed BLOB NOT NULL, last_step INTEGER NOT NULL,
 PRIMARY KEY(scope,account));
CREATE TABLE IF NOT EXISTS account_recovery_code(
 scope TEXT NOT NULL, account TEXT NOT NULL, code_hash BLOB NOT NULL,
 PRIMARY KEY(scope,account,code_hash));
"""
OTP = re.compile(r"[0-9]{6}")
RECOVERY = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{8}")
PERIOD = 30
SETUP_SECONDS = 10 * 60


class Authenticator:
    def __init__(self, scope, key):
        self.scope = scope
        self.key = hmac.digest(key.encode(), b"CuePorch authenticator " + scope.encode(), "sha256")
        self.cipher = Fernet(urlsafe_b64encode(self.key))

    def state(self, con, account):
        return con.execute("SELECT seed,last_step FROM account_authenticator WHERE scope=? AND account=?",
                           (self.scope, account)).fetchone()

    def forget(self, con, account):
        con.execute("DELETE FROM account_authenticator WHERE scope=? AND account=?", (self.scope, account))
        con.execute("DELETE FROM account_recovery_code WHERE scope=? AND account=?", (self.scope, account))

    def recovery_hash(self, account, value):
        return hmac.digest(self.key, (account + ":" + value.replace("-", "").lower()).encode(), "sha256")

    def binding(self, account, secure):
        return [account["id"], account["password_hash"], workspace_session(self.scope, secure)]

    def prepare(self, account, secure):
        seed = b32encode(secrets.token_bytes(20)).decode("ascii")
        ticket = self.cipher.encrypt(json.dumps([self.binding(account, secure), seed]).encode()).decode()
        role = "Team" if self.scope == "team" else "Viewer"
        issuer = f"CuePorch {role} ({request.host.replace(':', ' ')})"
        label = quote(issuer, safe="") + ":" + quote(account["email"], safe="")
        uri = "otpauth://totp/" + label + "?" + urlencode(dict(secret=seed, issuer=issuer))
        return private(dict(setup_key=seed, setup_ticket=ticket, app_link=uri))

    @staticmethod
    def matching_step(seed, value, minimum=-1):
        if not OTP.fullmatch(value):
            return None
        generator = TOTP(b32decode(seed), 6, hashes.SHA1(), PERIOD)
        clock = int(time()) // PERIOD
        for step in (clock, clock - 1, clock + 1):
            if step > minimum and hmac.compare_digest(generator.generate(step * PERIOD), value.encode()):
                return step
        return None

    def enable(self, con, account, secure, body, code):
        token = body.get("setup_ticket")
        if not isinstance(token, str) or len(token) > 1500:
            abort(409, "Start authenticator setup again.")
        try:
            binding, seed = json.loads(self.cipher.decrypt(token.encode(), ttl=SETUP_SECONDS))
        except (InvalidToken, ValueError):
            abort(409, "Setup expired. Start authenticator setup again.")
        if binding != self.binding(account, secure) or self.state(con, account["id"]):
            abort(409, "Your account or session changed. Start authenticator setup again.")
        first = self.matching_step(seed, code)
        if first is None:
            abort(422, "That authenticator code did not match.")
        uid = account["id"]
        con.execute("INSERT INTO account_authenticator VALUES(?,?,?,?)",
                    (self.scope, uid, self.cipher.encrypt(seed.encode()), first))
        codes = [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(10)]
        con.executemany("INSERT INTO account_recovery_code VALUES(?,?,?)",
                        [(self.scope, uid, self.recovery_hash(uid, value)) for value in codes])
        return {"enabled": True, "recovery_codes": codes}

    def check(self, con, uid, value):
        registered = self.state(con, uid)
        if registered is None:
            return
        if value is None or value == "":
            abort(private({"authenticator_required": True}, 428))
        if not isinstance(value, str) or len(value) > 32:
            abort(422, "Use a current authenticator or unused recovery code.")
        code = value.strip().lower()
        if RECOVERY.fullmatch(code):
            used = con.execute("DELETE FROM account_recovery_code WHERE scope=? AND account=? AND code_hash=?",
                               (self.scope, uid, self.recovery_hash(uid, code)))
            if used.rowcount:
                return
        elif OTP.fullmatch(code):
            try:
                seed = self.cipher.decrypt(registered["seed"]).decode()
            except InvalidToken:
                abort(503, "This installation could not open the authenticator settings.")
            step = self.matching_step(seed, code, registered["last_step"])
            if step is not None:
                con.execute("UPDATE account_authenticator SET last_step=? WHERE scope=? AND account=?",
                            (step, self.scope, uid))
                return
        abort(422, "That code did not match or has already been used.")


def register(app, tx, scope, signing_key, scheme, mutation, account_for, throttle, rotate, cookie, audit=None):
    authenticator = Authenticator(scope, signing_key)
    account_page = "/team" if scope == "team" else "/account"

    @app.get(account_page + "/security", endpoint=scope + "_security_page")
    def security_page():
        scheme()
        return private(render_template("account_security.html", scope=scope, account_page=account_page))

    @app.route(f"/api/{scope}/authenticator", methods=("GET", "POST", "PUT", "DELETE"),
               endpoint=scope + "_authenticator_settings")
    def security_settings():
        secure = scheme()
        if request.method in ("GET", "HEAD"):
            with tx() as con:
                actor = account_for(con, secure)
                return private({"enabled": bool(authenticator.state(con, actor["id"]))})
        body = mutation(secure)
        with tx() as con:
            actor = account_for(con, secure)
            throttle(con, "authenticator-" + request.method, actor["id"])
        if request.method in ("POST", "DELETE"):
            if not check_password_hash(actor["password_hash"], password(body)):
                abort(401, "The current password did not match.")
        with tx() as con:
            current = account_for(con, secure)
            if current["id"] != actor["id"] or current["password_hash"] != actor["password_hash"]:
                abort(409, "Your account changed. Try again.")
            state = authenticator.state(con, current["id"])
            if request.method == "POST":
                if state:
                    abort(409, "The authenticator is already enabled.")
                return authenticator.prepare(current, secure)
            code = body.get("code", "")
            if not isinstance(code, str) or not 1 <= len(code) <= 32:
                abort(422, "Enter an authenticator or recovery code.")
            if request.method == "PUT":
                result = authenticator.enable(con, current, secure, body, code.strip())
            else:
                if not state:
                    abort(409, "The authenticator is already disabled.")
                authenticator.check(con, current["id"], code)
                authenticator.forget(con, current["id"])
                result = {"enabled": False}
            token = rotate(con, current["id"], secure)
            if audit:
                audit(con, current, result["enabled"])
        return cookie(private(result), secure, token)

    return authenticator
