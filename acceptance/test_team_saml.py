"""Rehearse workspace single sign-on against a local, signed SAML identity provider."""

from base64 import b64encode
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, render_template_string, request
from lxml import etree
from onelogin.saml2.constants import OneLogin_Saml2_Constants as SAML
from onelogin.saml2.utils import OneLogin_Saml2_Utils as Toolkit
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from browser import chromium
from installed_simulcast import free_port, tls_certificate

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import create_app


EMAIL = "crew@station.example"
ISSUER = "urn:cueporch:fixture:identity"
TEAM = {"X-CuePorch-Team": "1"}


def signing_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "CuePorch SAML fixture")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().issuer_name(name).subject_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1)).sign(key, hashes.SHA256()))
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode(),
            cert.public_bytes(serialization.Encoding.PEM).decode())


def saml_document(origin, request_id, email, subject, private_key, certificate, *, audience=None, expired=False):
    now = datetime.now(UTC) - (timedelta(minutes=10) if expired else timedelta())
    issued = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = (now + timedelta(minutes=4 if expired else 60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (now - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    acs = origin + "/team/sso/acs"
    metadata = audience or origin + "/team/sso/metadata"
    response_id, assertion_id = "_" + secrets.token_hex(16), "_" + secrets.token_hex(16)
    xml = etree.fromstring(f"""<samlp:Response xmlns:samlp="{SAML.NS_SAMLP}" xmlns:saml="{SAML.NS_SAML}"
        ID="{response_id}" Version="2.0" IssueInstant="{issued}" Destination="{acs}" InResponseTo="{request_id}">
      <saml:Issuer>{ISSUER}</saml:Issuer>
      <samlp:Status><samlp:StatusCode Value="{SAML.STATUS_SUCCESS}"/></samlp:Status>
      <saml:Assertion ID="{assertion_id}" Version="2.0" IssueInstant="{issued}">
        <saml:Issuer>{ISSUER}</saml:Issuer>
        <saml:Subject>
          <saml:NameID>{subject}</saml:NameID>
          <saml:SubjectConfirmation Method="{SAML.CM_BEARER}">
            <saml:SubjectConfirmationData Recipient="{acs}" InResponseTo="{request_id}" NotOnOrAfter="{expires}"/>
          </saml:SubjectConfirmation>
        </saml:Subject>
        <saml:Conditions NotBefore="{start}" NotOnOrAfter="{expires}">
          <saml:AudienceRestriction><saml:Audience>{metadata}</saml:Audience></saml:AudienceRestriction>
        </saml:Conditions>
        <saml:AuthnStatement AuthnInstant="{issued}" SessionNotOnOrAfter="{expires}">
          <saml:AuthnContext><saml:AuthnContextClassRef>{SAML.AC_PASSWORD_PROTECTED}</saml:AuthnContextClassRef></saml:AuthnContext>
        </saml:AuthnStatement>
        <saml:AttributeStatement><saml:Attribute Name="email"><saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>
      </saml:Assertion>
    </samlp:Response>""".encode())
    assertion = xml.find("saml:Assertion", SAML.NSMAP)
    signed = Toolkit.add_sign(etree.tostring(assertion), private_key, certificate)
    xml.replace(assertion, etree.fromstring(signed))
    return b64encode(etree.tostring(xml)).decode()


class TeamSAML(unittest.TestCase):
    def server(self, stack, app, port, certificate):
        server = stack.enter_context(make_server("127.0.0.1", port, app, ssl_context=certificate))
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        stack.callback(worker.join, 5)
        stack.callback(server.shutdown)
        return server.server_port

    def test_workspace_signed_login(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as cleanup:
            root = Path(temporary)
            tls = tls_certificate(root)
            private_key, certificate = signing_material()
            foreign_key, foreign_cert = signing_material()
            port, idp_port = free_port(), free_port()
            origin = f"https://127.0.0.1:{port}"
            idp_origin = f"https://localhost:{idp_port}"
            operator = secrets.token_urlsafe(32)
            operator_headers = TEAM | {"Authorization": "Bearer " + operator}
            cleanup.enter_context(patch.dict("os.environ", {"CUEPORCH_PUBLIC_URL": origin}))
            for worker in ("library_worker", "library_caption_jobs", "review_push"):
                cleanup.enter_context(patch(f"institution.{worker}.start"))
            (root / "manifest.json").write_text(json.dumps(dict(institution="station", schema=1, works=[])))
            app = create_app(root, root / "institution.sqlite", operator, "station")
            for name in ("stop_event_capture", "stop_editor", "stop_file_links"):
                cleanup.callback(app.extensions[name])
            self.server(cleanup, app, port, tls)
            idp = Flask("fixture-identity-provider")
            policy = dict(subject="fixture-user", email=EMAIL, expired=False, audience=None, foreign=False)
            pending = {}

            @idp.get("/authorize")
            def authorize():
                document = etree.fromstring(Toolkit.decode_base64_and_inflate(request.args["SAMLRequest"]))
                self.assertEqual(document.get("AssertionConsumerServiceURL"), origin + "/team/sso/acs")
                self.assertEqual(document.find("saml:Issuer", SAML.NSMAP).text, origin + "/team/sso/metadata")
                state = request.args["RelayState"]
                self.assertRegex(state, re.compile(r"^[A-Za-z0-9_-]{43}$"))
                sign_key, sign_cert = (foreign_key, foreign_cert) if policy["foreign"] else (private_key, certificate)
                response = saml_document(origin, document.get("ID"), policy["email"], policy["subject"], sign_key, sign_cert,
                                         audience=policy["audience"], expired=policy["expired"])
                pending.clear()
                pending.update(RelayState=state, SAMLResponse=response)
                return render_template_string("""
                  <form method="post" action="{{ callback }}">
                    <input type="hidden" name="SAMLResponse" value="{{ response }}">
                    <input type="hidden" name="RelayState" value="{{ state }}">
                    <button>Continue as station crew</button>
                  </form>""", callback=origin + "/team/sso/acs", response=response, state=state)

            self.server(cleanup, idp, idp_port, tls)
            with sync_playwright() as playwright:
                browser = chromium(playwright)
                with browser.new_context(ignore_https_errors=True) as admin, browser.new_context(ignore_https_errors=True) as local:
                    page = admin.new_page()
                    page.goto(origin + "/team/identity")
                    page.get_by_label("Installation credential").fill(operator)
                    page.get_by_role("button", name="Open single sign-on settings").click()
                    status = page.locator("#teamIdentityStatus")
                    expect(status).to_have_text("Single sign-on is off.")
                    local_password = "Strong local fixture phrase"

                    def join(context, email, role, name):
                        invite = admin.request.post(origin + "/api/team/invitations", headers=operator_headers,
                                                    data={"email": email, "role": role})
                        self.assertTrue(invite.ok)
                        invitation = invite.json()["path"].split("#", 1)[1]
                        joined = context.request.post(origin + "/api/team/join", headers=TEAM, data=dict(
                            invitation=invitation, name=name, password=local_password,
                        ))
                        self.assertEqual(joined.status, 201)
                        return joined.json()["id"]

                    join(local, EMAIL, "contributor", "Station crew")
                    self.assertEqual(local.request.get(origin + "/api/team/identity",
                                     headers={"Authorization": "Bearer team-session"}).status, 403)

                    save = page.get_by_role("button", name="Save single sign-on settings")

                    def set_provider():
                        page.get_by_label("Allow team members to sign in with SAML").check()
                        page.get_by_label("Identity provider entity ID").fill(ISSUER)
                        page.get_by_label("Identity provider sign-in URL").fill(idp_origin + "/authorize")
                        page.get_by_label("Identity provider signing certificate").fill(certificate)
                        page.get_by_label("Authorized email domains, one per line", exact=True).fill("station.example")

                    set_provider()
                    save.click()
                    expect(status).to_have_text("Single sign-on is enabled.")
                    settings = admin.request.get(origin + "/api/team/identity", headers=operator_headers).json()
                    metadata = admin.request.get(settings["metadata"])
                    self.assertTrue(metadata.ok)
                    self.assertEqual(etree.fromstring(metadata.body()).get("entityID"), settings["metadata"])
                    self.assertEqual(admin.request.put(origin + "/api/team/identity", headers=operator_headers,
                                     data=dict(enabled=False, revision="stale")).status, 409)

                    def start(context):
                        sign_in = context.new_page()
                        sign_in.goto(origin + "/team")
                        sign_in.get_by_role("link", name="Sign in with your organization").click()
                        expect(sign_in).to_have_url(re.compile("^" + re.escape(idp_origin + "/authorize?")))
                        return sign_in

                    with browser.new_context(ignore_https_errors=True) as federated, browser.new_context(ignore_https_errors=True) as unrelated:
                        sign_in = start(federated)
                        self.assertEqual(unrelated.request.post(origin + "/team/sso/acs", form=pending).status, 403)
                        sign_in.get_by_role("button", name="Continue as station crew").click()
                        expect(sign_in.get_by_text("Station crew · Contributor")).to_be_visible()
                        self.assertEqual(federated.request.get(origin + "/api/team/account").json()["email"], EMAIL)
                        self.assertEqual(federated.request.post(origin + "/team/sso/acs", form=pending).status, 403)
                        with browser.new_context(ignore_https_errors=True) as invalid:
                            for change in (dict(foreign=True), dict(expired=True),
                                           dict(audience="https://unrelated.example/metadata"),
                                           dict(subject="unrelated-subject")):
                                policy.update(subject="fixture-user", expired=False, audience=None, foreign=False)
                                policy.update(change)
                                invalid_page = start(invalid)
                                invalid_page.get_by_role("button", name="Continue as station crew").click()
                                expect(invalid_page.get_by_role("heading", name="Forbidden")).to_be_visible()
                                self.assertEqual(invalid.request.get(origin + "/api/team/account").status, 401)
                                invalid_page.close()
                        page.get_by_label("Allow team members to sign in with SAML").uncheck()
                        save.click()
                        expect(status).to_have_text("Single sign-on is off.")
                        self.assertEqual(federated.request.get(origin + "/api/team/account").status, 401)
                        self.assertTrue(local.request.get(origin + "/api/team/account").ok)
                        self.assertEqual(admin.request.get(settings["metadata"]).status, 404)

                        with browser.new_context(ignore_https_errors=True) as owner:
                            owner_email = "owner@station.example"
                            owner_id = join(owner, owner_email, "admin", "Station owner")
                            self.assertTrue(admin.request.put(origin + "/api/team/owner/" + owner_id,
                                            headers=operator_headers, data={"previous": None}).ok)
                            set_provider()
                            page.get_by_label("Create accounts for new members").check()
                            page.get_by_label("Default role for new members").select_option("contributor_plus")
                            page.get_by_label("Require SAML for team members").check()
                            save.click()
                            expect(status).to_have_text("Single sign-on is enabled.")
                            self.assertEqual(local.request.get(origin + "/api/team/account").status, 401)
                            self.assertEqual(local.request.post(origin + "/api/team/session", headers=TEAM,
                                             data=dict(email=EMAIL, password=local_password)).status, 403)
                            self.assertTrue(owner.request.post(origin + "/api/team/session", headers=TEAM,
                                            data=dict(email=owner_email, password=local_password)).ok)
                            join(unrelated, "invited@station.example", "viewer", "Invited crew")
                            self.assertEqual(unrelated.request.get(origin + "/api/team/account").status, 401)
                            policy.update(subject="new-fixture-user", email="new@station.example")
                            new = start(federated)
                            new.get_by_role("button", name="Continue as station crew").click()
                            expect(new.get_by_text("new@station.example · Contributor Plus")).to_be_visible()
                            new_id = federated.request.get(origin + "/api/team/account").json()["id"]
                            result = admin.request.put(origin + "/api/team/owner/" + new_id, headers=operator_headers,
                                                       data={"previous": owner_id})
                            self.assertEqual(result.status, 409)
                            self.assertTrue(admin.request.delete(origin + "/api/team/members/" + new_id,
                                                                 headers=operator_headers, data={}).ok)
                            self.assertEqual(federated.request.get(origin + "/api/team/account").status, 401)
                            second_attempt = start(federated)
                            second_attempt.get_by_role("button", name="Continue as station crew").click()
                            expect(second_attempt.get_by_role("heading", name="Forbidden")).to_be_visible()

                    audit = admin.request.get(origin + "/api/team/audit", headers=operator_headers).json()["events"]
                    self.assertTrue({"saml_enabled", "saml_signed_in", "saml_disabled", "saml_provisioned", "removed"} <=
                                    {row["action"] for row in audit})
                browser.close()


if __name__ == "__main__":
    unittest.main()
