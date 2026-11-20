"""Workspace SAML policy and service-provider-initiated team sign-in."""

import hmac
import json
import re
import secrets
from datetime import UTC, datetime
from time import time
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from flask import abort, current_app, redirect, render_template, request
from lxml.etree import XMLSyntaxError
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.constants import OneLogin_Saml2_Constants as SAML
from onelogin.saml2.response import OneLogin_Saml2_Response
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from onelogin.saml2.utils import OneLogin_Saml2_Error, OneLogin_Saml2_Utils

from browser_account_security import TOKEN, email_key, private, same_origin, secure_site, token_hash
from library_analytics import source_origin
from team_group_roles import ROLES


SCHEMA = """
CREATE TABLE IF NOT EXISTS team_saml_settings(
 slot INTEGER PRIMARY KEY CHECK(slot=1), revision TEXT NOT NULL, settings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS team_saml_request(
 state TEXT PRIMARY KEY, request TEXT NOT NULL, revision TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS team_saml_assertion(
 issuer TEXT NOT NULL, assertion TEXT NOT NULL, expires REAL NOT NULL, PRIMARY KEY(issuer,assertion));
CREATE TABLE IF NOT EXISTS team_saml_identity(
 issuer TEXT NOT NULL, subject TEXT NOT NULL, member TEXT NOT NULL, PRIMARY KEY(issuer,subject),
 UNIQUE(issuer,member));
CREATE TABLE IF NOT EXISTS team_saml_session(token_hash TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS team_saml_group(
 team_group TEXT PRIMARY KEY REFERENCES team_group(id), issuer TEXT NOT NULL, source_name TEXT NOT NULL COLLATE NOCASE,
 UNIQUE(issuer,source_name));
"""
CALLBACK = "/team/sso/acs"
METADATA = "/team/sso/metadata"
STATE_SECONDS = 300
MAX_ASSERTION_AGE = 300
DEFAULT = dict(enabled=False, entity_id="", login_url="", signing_certificate="", domains=[], email_attribute="email",
               jit=False, force=False, idp_initiated=False, sync_groups=False, groups_attribute="groups",
               default_role="viewer", scim_adopt_members=False)
DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def configured(con):
    row = con.execute("SELECT revision, settings FROM team_saml_settings WHERE slot=1").fetchone()
    return (row["revision"], DEFAULT | json.loads(row["settings"])) if row else ("", DEFAULT.copy())


def forces_saml(settings, member):
    return bool(settings["enabled"] and settings["force"] and member["role"] != "owner"
                and member["email"].rsplit("@", 1)[-1] in settings["domains"])


def allows_password(con, member):
    return bool(member and member["password_hash"] and not forces_saml(configured(con)[1], member))


def mark_session(con, token, expiry):
    digest = token_hash(token)
    if expiry is not None:
        con.execute("UPDATE team_session SET expires=min(expires,?) WHERE token_hash=?", (expiry, digest))
    con.execute("DELETE FROM team_saml_session WHERE token_hash NOT IN (SELECT token_hash FROM team_session)")
    con.execute("INSERT INTO team_saml_session VALUES(?)", (digest,))


def canonical_https(origin):
    return origin.startswith("https://") and urlsplit(origin).netloc.casefold() == request.host.casefold()


def require_owner(con, operator, ownership):
    actor = operator(con)
    if not actor or not ownership(con, actor)["can_transfer"]:
        abort(403, "Only the workspace Owner or installation credential can configure identity.")
    return actor


def current_certificate(value):
    cert = x509.load_pem_x509_certificate(value.encode("ascii"))
    if not cert.not_valid_before_utc <= datetime.now(UTC) < cert.not_valid_after_utc:
        raise ValueError("The identity provider certificate is outside its validity period.")
    return cert


def validated_settings(body, origin):
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        abort(400, "Choose whether to enable team single sign-on.")
    if not enabled:
        return DEFAULT.copy()
    if not origin.startswith("https://"):
        abort(409, "Configure the workspace's public HTTPS address first.")
    settings = {"enabled": True}
    for key, limit in (("entity_id", 512), ("login_url", 2048), ("signing_certificate", 8192), ("email_attribute", 256)):
        value = body.get(key)
        if not isinstance(value, str) or not (1 <= len(value.strip()) <= limit):
            abort(400, "Fill in the identity provider, its login URL, certificate and email attribute.")
        settings[key] = value.strip()
    try:
        login = urlsplit(settings["login_url"])
        entity = urlsplit(settings["entity_id"])
        if (login.scheme != "https" or not login.hostname or login.username is not None or login.fragment
                or any(ch.isspace() for ch in settings["login_url"])
                or entity.scheme not in ("http", "https", "urn") or any(ch.isspace() for ch in settings["entity_id"])):
            raise ValueError
        cert = current_certificate(settings["signing_certificate"])
        key = cert.public_key()
        if not ((isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048)
                or (isinstance(key, ec.EllipticCurvePublicKey) and key.key_size >= 256)):
            abort(400, "Use an identity provider certificate with a supported signing key.")
        settings["signing_certificate"] = cert.public_bytes(serialization.Encoding.PEM).decode().strip()
    except (ValueError, UnicodeError):
        abort(400, "Use an HTTPS login URL, a valid issuer ID and a PEM signing certificate.")
    domains = body.get("domains")
    if not isinstance(domains, list) or not 1 <= len(domains) <= 50:
        abort(400, "Enter at least one authorized email domain.")
    if any(not isinstance(value, str) or not DOMAIN.fullmatch(value.strip().lower()) for value in domains):
        abort(400, "Enter exact email domains such as example.org.")
    settings["domains"] = sorted({value.strip().lower() for value in domains})
    for key in ("jit", "force", "idp_initiated", "sync_groups", "scim_adopt_members"):
        value = body.get(key, DEFAULT[key])
        if not isinstance(value, bool):
            abort(400, "Choose which single sign-on policies to enable.")
        settings[key] = value
    role = body.get("default_role", DEFAULT["default_role"])
    if role not in ROLES:
        abort(400, "Choose a default role for new members.")
    settings["default_role"] = role
    attribute = body.get("groups_attribute", DEFAULT["groups_attribute"])
    if not isinstance(attribute, str) or not 1 <= len(attribute.strip()) <= 256:
        abort(400, "Enter the signed attribute that contains group names.")
    settings["groups_attribute"] = attribute.strip()
    try:
        saml_settings(settings, origin)
    except OneLogin_Saml2_Error:
        abort(400, "Check the identity provider configuration.")
    return settings


def saml_settings(settings, origin):
    return OneLogin_Saml2_Settings({
        "strict": True,
        "sp": {
            "entityId": origin + METADATA,
            "assertionConsumerService": {"url": origin + CALLBACK, "binding": SAML.BINDING_HTTP_POST},
            "NameIDFormat": SAML.NAMEID_UNSPECIFIED,
        },
        "idp": {
            "entityId": settings["entity_id"],
            "singleSignOnService": {"url": settings["login_url"], "binding": SAML.BINDING_HTTP_REDIRECT},
            "x509cert": settings["signing_certificate"],
        },
        "security": {
            "wantAssertionsSigned": True,
            "wantNameId": True,
            "requestedAuthnContext": False,
            "rejectDeprecatedAlgorithm": True,
        },
    })


def assertion_for(settings, origin, encoded, expected):
    now = time()
    request_data = {"https": "on", "http_host": urlsplit(origin).netloc, "script_name": CALLBACK}
    try:
        current_certificate(settings["signing_certificate"])
        response = OneLogin_Saml2_Response(saml_settings(settings, origin), encoded)
        document = response.get_xml_document()
        if (document.getroottree().docinfo.doctype or document.get("Destination") != origin + CALLBACK
                or response.get_in_response_to() != expected or not response.is_valid(request_data, expected)):
            return None
        assertion = document.find("saml:Assertion", SAML.NSMAP)
        issued = response.get_assertion_issue_instant()
        if assertion is None or not now - MAX_ASSERTION_AGE < issued <= now + 60:
            return None
        expiry = response.get_assertion_not_on_or_after()
        if not expiry or not now < expiry:
            return None
        confirmations = assertion.findall("saml:Subject/saml:SubjectConfirmation", SAML.NSMAP)
        for confirmation in confirmations:
            data = confirmation.find("saml:SubjectConfirmationData", SAML.NSMAP)
            if (data is not None and confirmation.get("Method") == SAML.CM_BEARER
                    and data.get("Recipient") == origin + CALLBACK and data.get("InResponseTo") == expected
                    and data.get("NotOnOrAfter") and OneLogin_Saml2_Utils.parse_SAML_to_time(data.get("NotOnOrAfter")) == expiry):
                break
        else:
            return None
        restrictions = assertion.findall("saml:Conditions/saml:AudienceRestriction", SAML.NSMAP)
        if not restrictions or any(origin + METADATA not in [node.text for node in item] for item in restrictions):
            return None
        subject = response.get_nameid()
        assertion_id = response.get_assertion_id()
        attributes = response.get_attributes()
        address = attributes.get(settings["email_attribute"], [])
        if not subject or len(subject) > 512 or not assertion_id or len(assertion_id) > 256 or len(address) != 1:
            return None
        session_expiry = response.get_session_not_on_or_after()
        name = " ".join(value for key in ("firstName", "lastName") for value in attributes.get(key, [])[:1]).strip()
        if not 1 <= len(name) <= 120 or any(ord(char) < 32 for char in name):
            name = address[0][:120]
        groups = [group.strip() for value in attributes.get(settings["groups_attribute"], [])
                  for group in value.split(",") if group.strip()] if settings["sync_groups"] else []
        if len(groups) > 100 or any(len(group) > 64 or any(ord(char) < 32 for char in group) for group in groups):
            return None
        return subject, address[0], name, groups, assertion_id, min(expiry, issued + MAX_ASSERTION_AGE), session_expiry
    except (OneLogin_Saml2_Error, XMLSyntaxError, ValueError, TypeError, AttributeError, OverflowError):
        return None


def team_for_assertion(con, settings, subject, address, name, note):
    if address.rsplit("@", 1)[-1] not in settings["domains"]:
        return None
    issuer = settings["entity_id"]
    identity = con.execute("SELECT member FROM team_saml_identity WHERE issuer=? AND subject=?", (issuer, subject)).fetchone()
    if identity:
        return con.execute("""SELECT id FROM team_member m
            WHERE id=? AND email=? AND role<>'owner' AND removed IS NULL
            AND NOT EXISTS(SELECT 1 FROM team_scim_user u WHERE u.member=m.id AND u.issuer<>?)""",
            (identity["member"], address, issuer)).fetchone()
    member = con.execute("SELECT id,role FROM team_member WHERE email=? AND removed IS NULL", (address,)).fetchone()
    if member is None:
        if not settings["jit"] or con.execute("SELECT 1 FROM team_member WHERE email=?", (address,)).fetchone():
            return None
        invitation = con.execute("SELECT role FROM team_invitation WHERE email=? AND expires>?", (address, time())).fetchone()
        uid, role = str(uuid4()), invitation["role"] if invitation else settings["default_role"]
        con.execute("INSERT INTO team_member VALUES(?,?,?,?,?,?,NULL)", (uid, address, name, role, "", time()))
        con.execute("DELETE FROM team_invitation WHERE email=?", (address,))
        note(con, "saml", "saml_provisioned", uid, role)
        member = dict(id=uid, role=role)
    if member["role"] == "owner":
        return None
    managed = con.execute("SELECT issuer FROM team_scim_user WHERE member=?", (member["id"],)).fetchone()
    if managed and managed["issuer"] != issuer:
        return None
    result = con.execute("INSERT OR IGNORE INTO team_saml_identity VALUES(?,?,?)", (issuer, subject, member["id"]))
    return member if result.rowcount else None


def sync_group_claims(con, issuer, member, names, note):
    existing = {row["team_group"] for row in con.execute("""SELECT g.team_group FROM team_saml_group g
        JOIN team_group_member m ON m.team_group=g.team_group WHERE g.issuer=? AND m.member=?""", (issuer, member))}
    incoming = set()
    for name in dict.fromkeys(names):
        group = con.execute("SELECT team_group FROM team_saml_group WHERE issuer=? AND source_name=?",
                            (issuer, name)).fetchone()
        if group:
            incoming.add(group["team_group"])
        elif not con.execute("SELECT 1 FROM team_group WHERE name=? COLLATE NOCASE", (name,)).fetchone():
            uid = str(uuid4())
            con.execute("INSERT INTO team_group VALUES(?,?,?)", (uid, name, secrets.token_hex(16)))
            con.execute("INSERT INTO team_saml_group VALUES(?,?,?)", (uid, issuer, name))
            incoming.add(uid)
    con.executemany("DELETE FROM team_group_member WHERE team_group=? AND member=?",
                    [(uid, member) for uid in existing - incoming])
    con.executemany("INSERT OR IGNORE INTO team_group_member VALUES(?,?)",
                    [(uid, member) for uid in incoming - existing])
    changed = existing ^ incoming
    if changed:
        con.executemany("UPDATE team_group SET revision=lower(hex(randomblob(16))) WHERE id=?", [(uid,) for uid in changed])
        note(con, "saml", "saml_groups_synchronized", member, str(len(incoming)))


def register(app, tx, origin, operator, ownership, mutation, issue, cookie, note, limit):
    def available():
        if not canonical_https(origin):
            return False
        with tx() as con:
            return configured(con)[1]["enabled"]

    @app.get("/team/identity")
    def identity_settings_page():
        secure_site(origin)
        return private(render_template("team_identity.html", public_origin=origin if origin.startswith("https://") else ""))

    @app.route("/api/team/identity", methods=("GET", "PUT"))
    def identity_settings():
        secure = secure_site(origin)
        same_origin(secure)
        with tx() as con:
            require_owner(con, operator, ownership)
            revision, configuration = configured(con)
        if request.method in ("GET", "HEAD"):
            return private(dict(revision=revision, settings=configuration,
                                metadata=origin + METADATA if origin.startswith("https://") else "",
                                callback=origin + CALLBACK if origin.startswith("https://") else ""))
        body = mutation(secure)
        configuration = validated_settings(body, origin)
        with tx() as con:
            actor = require_owner(con, operator, ownership)
            previous, old = configured(con)
            if body.get("revision") != previous:
                abort(409, "SSO settings changed. Reload before saving.")
            if configuration == old:
                return private(dict(revision=previous, settings=old))
            revision = secrets.token_hex(16)
            con.execute("INSERT OR REPLACE INTO team_saml_settings VALUES(1,?,?)", (revision, json.dumps(configuration)))
            con.execute("DELETE FROM team_saml_request")
            con.execute("DELETE FROM team_session WHERE token_hash IN (SELECT token_hash FROM team_saml_session)")
            con.execute("DELETE FROM team_saml_session")
            if not configuration["enabled"] or configuration["entity_id"] != old["entity_id"]:
                con.execute("DELETE FROM team_scim_token")
            if (not configuration["enabled"] or not configuration["sync_groups"]
                    or configuration["entity_id"] != old["entity_id"]
                    or configuration["groups_attribute"] != old["groups_attribute"]):
                con.execute("""UPDATE team_group SET revision=lower(hex(randomblob(16)))
                            WHERE id IN (SELECT team_group FROM team_saml_group)""")
                con.execute("DELETE FROM team_group_member WHERE team_group IN (SELECT team_group FROM team_saml_group)")
            if configuration["enabled"] and configuration["force"]:
                members = con.execute("SELECT id,email,role FROM team_member WHERE removed IS NULL AND role<>'owner'")
                con.executemany("DELETE FROM team_session WHERE member=?",
                                [(member["id"],) for member in members if forces_saml(configuration, member)])
            note(con, actor, "saml_enabled" if configuration["enabled"] else "saml_disabled", "workspace")
        return private(dict(revision=revision, settings=configuration))

    @app.get(METADATA)
    def team_saml_metadata():
        if not canonical_https(origin):
            abort(404)
        with tx() as con:
            _, settings = configured(con)
        if not settings["enabled"]:
            abort(404)
        response = private(saml_settings(settings, origin).get_sp_metadata())
        response.mimetype = "application/samlmetadata+xml"
        return response

    def state_cookie():
        return "__Host-cueporch-sso-" + current_app.config["WORKSPACE_COOKIE_SCOPE"]

    @app.get("/team/sso")
    def start_team_saml():
        if not canonical_https(origin):
            abort(404)
        same_origin(True)
        with tx() as con:
            revision, settings = configured(con)
            if not settings["enabled"]:
                abort(404)
            limit(con, "saml-start")
            state = secrets.token_urlsafe(32)
            auth = OneLogin_Saml2_Auth({}, saml_settings(settings, origin))
            target = auth.login(return_to=state, set_nameid_policy=False)
            con.execute("DELETE FROM team_saml_request WHERE expires<=?", (time(),))
            con.execute("INSERT INTO team_saml_request VALUES(?,?,?,?)",
                        (token_hash(state), auth.get_last_request_id(), revision, time() + STATE_SECONDS))
        response = private(redirect(target))
        response.set_cookie(state_cookie(), state, max_age=STATE_SECONDS, secure=True, httponly=True, samesite="None", path="/")
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.post(CALLBACK)
    def finish_team_saml():
        if not canonical_https(origin) or request.mimetype != "application/x-www-form-urlencoded":
            abort(404)
        if not request.content_length or request.content_length > 192 * 1024:
            abort(413)
        state = request.form.get("RelayState", "")
        encoded = request.form.get("SAMLResponse", "")
        browser_state = request.cookies.get(state_cookie(), "")
        if not encoded:
            abort(403, "Start single sign-on from this workspace again.")
        digest = (token_hash(state) if TOKEN.fullmatch(state) and TOKEN.fullmatch(browser_state)
                  and hmac.compare_digest(state, browser_state) else None)
        with tx() as con:
            limit(con, "saml-response")
            revision, settings = configured(con)
            pending = con.execute("SELECT request FROM team_saml_request WHERE state=? AND revision=? AND expires>?",
                                  (digest, revision, time())).fetchone() if digest else None
        provider_launch = (not digest and settings["idp_initiated"]
                           and request.headers.get("Origin") == source_origin(settings["login_url"]))
        if not settings["enabled"] or (not pending and not provider_launch):
            abort(403, "Start single sign-on from this workspace again.")
        verified = assertion_for(settings, origin, encoded, pending["request"] if pending else None)
        if not verified:
            abort(403, "The identity provider's sign-in could not be verified.")
        subject, address, name, groups, assertion, expires, session_expiry = verified
        if session_expiry is not None and session_expiry <= time():
            abort(403, "This single sign-on session has expired.")
        address = email_key(address)
        with tx() as con:
            if configured(con)[0] != revision:
                abort(403, "Start single sign-on from this workspace again.")
            if pending and not con.execute("DELETE FROM team_saml_request WHERE state=? AND expires>?",
                                           (digest, time())).rowcount:
                abort(403, "Start single sign-on from this workspace again.")
            con.execute("DELETE FROM team_saml_assertion WHERE expires<=?", (time(),))
            if not con.execute("INSERT OR IGNORE INTO team_saml_assertion VALUES(?,?,?)",
                               (settings["entity_id"], assertion, expires)).rowcount:
                abort(403, "This sign-in has already been used.")
            member = team_for_assertion(con, settings, subject, address, name, note)
            if not member:
                abort(403, "This identity is not linked to an eligible workspace member.")
            if settings["sync_groups"]:
                sync_group_claims(con, settings["entity_id"], member["id"], groups, note)
            token = issue(con, member["id"], True)
            mark_session(con, token, session_expiry)
            note(con, "team:" + member["id"], "saml_signed_in", member["id"])
        response = cookie(private(redirect("/team", code=303)), True, token)
        response.delete_cookie(state_cookie(), secure=True, httponly=True, samesite="None", path="/")
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return available
