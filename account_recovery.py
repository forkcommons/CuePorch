"""Password reset using the verified mailbox for a viewer or team account."""

import logging
from queue import Empty, Full, Queue
from secrets import token_urlsafe
from threading import Event, Lock, Thread
from time import time

from flask import abort, render_template, request
from werkzeug.security import generate_password_hash

from browser_account_security import TOKEN, email_key, password, private, token_hash
import mail_relay


SCHEMA = """
CREATE TABLE IF NOT EXISTS account_password_reset(
 scope TEXT NOT NULL, account TEXT NOT NULL, address TEXT NOT NULL, token_hash TEXT NOT NULL,
 password_version TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, delivered INTEGER NOT NULL,
 PRIMARY KEY(scope,account));
CREATE UNIQUE INDEX IF NOT EXISTS account_reset_token ON account_password_reset(scope,token_hash);
"""
RESET_SECONDS = 30 * 60
RESET_INTERVAL = 60


class ResetDelivery:
    def __init__(self):
        self.queue = Queue(maxsize=24)
        self.closed = Event()
        self.lock = Lock()
        self.worker = None

    def submit(self, send, address):
        with self.lock:
            if self.closed.is_set():
                abort(503, "Account email is temporarily unavailable.")
            try:
                self.queue.put_nowait((send, address))
            except Full:
                abort(503, "Too many reset requests. Try again later.")
            if self.worker is None:
                self.worker = Thread(target=self.run, name="CuePorch password reset email", daemon=True)
                self.worker.start()

    def run(self):
        while not self.closed.is_set():
            try:
                send, address = self.queue.get(timeout=0.2)
            except Empty:
                continue
            if self.closed.is_set():
                return
            try:
                send(address)
            except Exception:
                logging.getLogger(__name__).exception("Password reset delivery failed.")

    def stop(self):
        self.closed.set()
        with self.lock:
            worker = self.worker
        if worker:
            worker.join(timeout=7)


def register(app, tx, scope, origin, verified_mail, scheme, mutation, find_account, throttle, authenticator,
             replace_password):
    prefix = "/team" if scope == "team" else "/account"
    page_path = prefix + "/reset"
    api_path = f"/api/{scope}/password-recovery"
    jobs = app.extensions["account_reset_delivery"]

    def send_reset(address):
        token = token_urlsafe(32)
        digest = token_hash(token)
        now = time()
        with tx() as con:
            con.execute("DELETE FROM account_password_reset WHERE expires<=?", (now,))
            account = find_account(con, address)
            if not account or not verified_mail.verified(con, account):
                return
            uid = account["id"]
            active = con.execute("SELECT created FROM account_password_reset WHERE scope=? AND account=?",
                                 (scope, uid)).fetchone()
            if active and now - active["created"] < RESET_INTERVAL:
                return
            con.execute("INSERT OR REPLACE INTO account_password_reset VALUES(?,?,?,?,?,?,?,0)",
                        (scope, uid, address, digest, token_hash(account["password_hash"]), now, now + RESET_SECONDS))
        link = origin + page_path + "#" + token
        accepted = mail_relay.deliver(
            verified_mail.config, address, "Reset your CuePorch password",
            "Open this link to choose a new password. It expires in 30 minutes and works once.\n\n" +
            link + "\n\nIf you did not request a reset, you can ignore this message.\n",
        )
        with tx() as con:
            if accepted:
                con.execute("UPDATE account_password_reset SET delivered=1 WHERE scope=? AND account=? AND token_hash=?",
                            (scope, uid, digest))

    def checked_reset(con, token):
        pending = con.execute("""SELECT * FROM account_password_reset WHERE scope=? AND token_hash=?
                                 AND expires>? AND delivered=1""", (scope, token, time())).fetchone()
        account = find_account(con, pending["address"]) if pending else None
        if (not account or account["id"] != pending["account"]
                or token_hash(account["password_hash"]) != pending["password_version"]
                or not verified_mail.verified(con, account)):
            abort(404, "This reset link is expired or was already used. Ask for a new one.")
        return account

    @app.get(page_path, endpoint=scope + "_password_reset")
    def password_reset_page():
        scheme()
        response = private(render_template("password_recovery.html", scope=scope, account_page=prefix,
                                           available=bool(verified_mail.config)))
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.route(api_path, methods=("POST", "PUT"), endpoint=scope + "_password_recovery")
    def password_recovery():
        secure = scheme()
        body = mutation(secure)
        if request.method == "POST":
            address = email_key(body.get("email"))
            if not verified_mail.config:
                abort(503, "The operator has not configured account email delivery.")
            with tx() as con:
                throttle(con, "password-reset-request", address)
            jobs.submit(send_reset, address)
            return private({"accepted": True}, 202)
        token = body.get("reset_token")
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            abort(404, "Open the complete password reset link.")
        digest = token_hash(token)
        with tx() as con:
            throttle(con, "password-reset-confirm", digest)
        with tx() as con:
            checked_reset(con, digest)
        new = generate_password_hash(password(body, new=True))
        with tx() as con:
            account = checked_reset(con, digest)
            authenticator.check(con, account["id"], body.get("code"))
            replace_password(con, account["id"], new)
            con.execute("DELETE FROM account_password_reset WHERE scope=? AND account=?", (scope, account["id"]))
        return private("", 204)
