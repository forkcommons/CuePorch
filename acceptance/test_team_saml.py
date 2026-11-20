"""Rehearse workspace single sign-on against a local, signed SAML identity provider."""

from base64 import b64encode
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
from threading import Thread
from time import time
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
from cueporch import import_collection
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


def saml_document(origin, request_id, email, subject, private_key, certificate, *,
                  audience=None, expired=False, groups=(), issuer=ISSUER):
    now = datetime.now(UTC) - (timedelta(minutes=10) if expired else timedelta())
    issued = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = (now + timedelta(minutes=4 if expired else 60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (now - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    acs = origin + "/team/sso/acs"
    metadata = audience or origin + "/team/sso/metadata"
    response_id, assertion_id = "_" + secrets.token_hex(16), "_" + secrets.token_hex(16)
    response_to = f' InResponseTo="{request_id}"' if request_id else ""
    xml = etree.fromstring(f"""<samlp:Response xmlns:samlp="{SAML.NS_SAMLP}" xmlns:saml="{SAML.NS_SAML}"
        ID="{response_id}" Version="2.0" IssueInstant="{issued}" Destination="{acs}"{response_to}>
      <saml:Issuer>{issuer}</saml:Issuer>
      <samlp:Status><samlp:StatusCode Value="{SAML.STATUS_SUCCESS}"/></samlp:Status>
      <saml:Assertion ID="{assertion_id}" Version="2.0" IssueInstant="{issued}">
        <saml:Issuer>{issuer}</saml:Issuer>
        <saml:Subject>
          <saml:NameID>{subject}</saml:NameID>
          <saml:SubjectConfirmation Method="{SAML.CM_BEARER}">
            <saml:SubjectConfirmationData Recipient="{acs}" NotOnOrAfter="{expires}"{response_to}/>
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
    if groups:
        statement = assertion.find("saml:AttributeStatement", SAML.NSMAP)
        attribute = etree.SubElement(statement, "{" + SAML.NS_SAML + "}Attribute", Name="groups")
        for group in groups:
            etree.SubElement(attribute, "{" + SAML.NS_SAML + "}AttributeValue").text = group
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
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=12:d=2",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(root / "clip.mp4")], check=True)
            intake, collection = root / "intake.json", root / "collection"
            intake.write_text(json.dumps(dict(institution="station", works=[dict(
                title="Private station film", provider="fixture", source_id="film", source_url="",
                files=[dict(role="playback", format="mp4", path="clip.mp4")],
            )])))
            import_collection(intake, collection)
            work = json.loads((collection / "manifest.json").read_text())["works"][0]["id"]
            app = create_app(collection, root / "institution.sqlite", operator, "station")
            for name in ("stop_event_capture", "stop_editor", "stop_file_links"):
                cleanup.callback(app.extensions[name])
            self.server(cleanup, app, port, tls)
            idp = Flask("fixture-identity-provider")
            policy = dict(subject="fixture-user", email=EMAIL, expired=False, audience=None, foreign=False, groups=(),
                          issuer=ISSUER)
            pending, provider_assertion = {}, {}

            def show_assertion(response, state):
                return render_template_string("""
                  <form method="post" action="{{ callback }}">
                    <input type="hidden" name="SAMLResponse" value="{{ response }}">
                    <input type="hidden" name="RelayState" value="{{ state }}">
                    <button>Continue as station crew</button>
                  </form>""", callback=origin + "/team/sso/acs", response=response, state=state)

            @idp.get("/authorize")
            def authorize():
                document = etree.fromstring(Toolkit.decode_base64_and_inflate(request.args["SAMLRequest"]))
                self.assertEqual(document.get("AssertionConsumerServiceURL"), origin + "/team/sso/acs")
                self.assertEqual(document.find("saml:Issuer", SAML.NSMAP).text, origin + "/team/sso/metadata")
                state = request.args["RelayState"]
                self.assertRegex(state, re.compile(r"^[A-Za-z0-9_-]{43}$"))
                sign_key, sign_cert = (foreign_key, foreign_cert) if policy["foreign"] else (private_key, certificate)
                response = saml_document(origin, document.get("ID"), policy["email"], policy["subject"], sign_key, sign_cert,
                                         audience=policy["audience"], expired=policy["expired"], groups=policy["groups"],
                                         issuer=policy["issuer"])
                pending.clear()
                pending.update(RelayState=state, SAMLResponse=response)
                return show_assertion(response, state)

            @idp.get("/portal")
            def portal():
                if not provider_assertion:
                    provider_assertion.update(RelayState="https://unrelated.example/ignored", SAMLResponse=saml_document(
                        origin, None, EMAIL, "fixture-user", private_key, certificate,
                    ))
                return show_assertion(provider_assertion["SAMLResponse"], provider_assertion["RelayState"])

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
                            self.verify_scim(page, admin, federated, start, origin, policy, operator_headers, work)

                            with browser.new_context(ignore_https_errors=True) as from_portal:
                                portal_page = from_portal.new_page()

                                def show_portal():
                                    portal_page.goto(idp_origin + "/portal")
                                    return portal_page.get_by_role("button", name="Continue as station crew")

                                show_portal().click()
                                expect(portal_page.get_by_role("heading", name="Forbidden")).to_be_visible()
                                page.get_by_label("Allow members to start sign-in from the identity provider").check()
                                with page.expect_response(lambda result: result.url == origin + "/api/team/identity"
                                                          and result.request.method == "PUT") as saved:
                                    save.click()
                                self.assertTrue(saved.value.ok)
                                continue_sso = show_portal()
                                foreign = from_portal.request.post(origin + "/team/sso/acs", form=provider_assertion,
                                                                   headers={"Origin": "https://unrelated.example"})
                                self.assertEqual(foreign.status, 403)
                                continue_sso.click()
                                expect(portal_page.get_by_text("Station crew · Contributor")).to_be_visible()
                                expect(portal_page).to_have_url(origin + "/team")
                                show_portal().click()
                                expect(portal_page.get_by_text("This sign-in has already been used.")).to_be_visible()
                            self.verify_group_claims(page, admin, federated, unrelated, start, origin, policy,
                                                     operator_headers, work)
                            existing = join(local, "existing@station.example", "contributor", "Existing producer")
                            self.verify_scim_adoption(page, admin, local, federated, start, origin, policy,
                                                      operator_headers, work, existing, owner_email, local_password)
                            self.assertEqual(owner.request.get(origin + "/api/team/account").json()["role"], "owner")
                            self.verify_session_controls(admin, owner, local, federated, start, origin, policy,
                                                         operator_headers, work, owner_email, local_password)

                    audit = admin.request.get(origin + "/api/team/audit", headers=operator_headers).json()["events"]
                    self.assertTrue({"saml_enabled", "saml_signed_in", "saml_disabled", "saml_provisioned", "removed"} <=
                                    {row["action"] for row in audit})
                    self.assertTrue(any(row["action"] == "scim_adopted" and row["subject"] == existing for row in audit))
                browser.close()

    def verify_scim(self, page, admin, federated, start, origin, policy, operator, work):
        api = admin.request
        page.get_by_role("button", name="Create provisioning token").click()
        status = page.locator("#teamProvisioningStatus")
        expect(status).to_have_text("Provisioning is enabled.")
        token = page.locator("#newProvisioningToken input").input_value()
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        base = page.get_by_label("Provisioning base URL").input_value()
        self.assertEqual(base, origin + "/scim/v2")
        auth = {"Authorization": "Bearer " + token, "Content-Type": "application/scim+json"}
        self.assertNotIn("token", api.get(origin + "/api/team/identity/provisioning", headers=operator).json())
        self.assertEqual(api.get(base + "/Users", headers=operator).status, 401)
        self.assertTrue(api.get(base + "/ServiceProviderConfig", headers=auth).json()["patch"]["supported"])

        original = "scim@station.example"
        new_address = "editor@station.example"
        response = api.post(base + "/Users", headers=auth, data=dict(
            userName=original, externalId="directory-person-001", name=dict(givenName="Private", familyName="Editor"),
        ))
        self.assertEqual((response.status, response.headers["content-type"]), (201, "application/scim+json"))
        user = response.json()
        self.assertEqual(user["displayName"], "Private Editor")
        uri = response.headers["location"]

        def update(path, *operations):
            return api.patch(path, headers=auth, data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"], "Operations": list(operations),
            })

        filtered = api.get(base + "/Users", headers=auth, params={"filter": f'userName eq "{original}"'}).json()
        self.assertEqual((filtered["totalResults"], filtered["Resources"][0]["id"]), (1, user["id"]))
        empty = api.get(base + "/Users", headers=auth, params={"startIndex": 2, "count": 1}).json()
        self.assertEqual((empty["totalResults"], empty["itemsPerPage"]), (1, 0))
        self.assertEqual(api.post(base + "/Users", headers=auth, data=dict(userName=original)).status, 409)
        policy.update(subject="scim-directory-001", email=original)
        screen = start(federated)
        screen.get_by_role("button", name="Continue as station crew").click()
        expect(screen.get_by_text("Private Editor · Contributor Plus")).to_be_visible()
        media = origin + f"/team/video/{work}/media"
        self.assertEqual(federated.request.get(media).status, 404)

        group_response = api.post(base + "/Groups", headers=auth, data={
            "displayName": "Station editors", "externalId": "directory-group-001",
        })
        self.assertEqual(group_response.status, 201)
        group = group_response.json()
        group_uri = group_response.headers["location"]
        listed = api.get(base + "/Groups", headers=auth, params={
            "filter": 'displayName eq "Station editors"', "excludedAttributes": "members",
        }).json()
        self.assertNotIn("members", listed["Resources"][0])
        folder = api.post(origin + "/api/library/folders", headers=operator, data={"name": "Station work", "color": "#22555a"})
        self.assertTrue(folder.ok)
        self.assertTrue(api.put(origin + "/api/library/placement", headers=operator,
                                data={"works": [work], "folder": folder.json()["id"]}).ok)
        local_group = api.get(origin + "/api/team/groups", headers=operator).json()["groups"][0]
        self.assertTrue(local_group["managed"])
        local_uri = origin + "/api/team/groups/" + group["id"]
        self.assertTrue(api.put(local_uri, headers=operator, data=dict(local_group, folders=[folder.json()["id"]])).ok)
        add = dict(op="Add", path="members", value=[{"value": user["id"]}])
        self.assertEqual(update(group_uri, add).status, 204)
        screen.goto(origin + "/team")
        screen.get_by_role("link", name="Private station film").click()
        width = screen.locator("video").evaluate("async video => {video.muted=true; await video.play(); return video.videoWidth;}")
        self.assertEqual(width, 160)
        remove = dict(op="Remove", path=f'members[value eq "{user["id"]}"]')
        self.assertEqual(update(group_uri, remove).status, 204)
        self.assertEqual(federated.request.get(media).status, 404)
        self.assertEqual(update(group_uri, add).status, 204)
        self.assertTrue(federated.request.get(media).ok)
        self.assertFalse(update(uri, dict(op="Replace", path="active", value=False)).json()["active"])
        self.assertEqual(federated.request.get(media).status, 401)
        gone = start(federated)
        gone.get_by_role("button", name="Continue as station crew").click()
        expect(gone.get_by_role("heading", name="Forbidden")).to_be_visible()
        activated = update(uri, dict(op="Replace", value=dict(active=True, userName=new_address,
                                                              name=dict(givenName="Renamed", familyName="Editor")))).json()
        self.assertTrue(activated["active"])
        self.assertEqual((activated["id"], activated["displayName"]), (user["id"], "Renamed Editor"))
        self.assertEqual(api.get(group_uri, headers=auth).json()["members"], [])
        policy["email"] = new_address
        renewed = start(federated)
        renewed.get_by_role("button", name="Continue as station crew").click()
        expect(renewed.get_by_text("Renamed Editor · Contributor Plus")).to_be_visible()
        self.assertEqual(federated.request.get(media).status, 404)
        self.assertEqual(api.delete(uri, headers=auth).status, 204)
        self.assertEqual(api.get(uri, headers=auth).status, 404)
        self.assertEqual(federated.request.get(media).status, 401)
        self.assertEqual(api.delete(group_uri, headers=auth).status, 204)
        self.assertEqual(api.get(group_uri, headers=auth).status, 404)
        page.get_by_role("button", name="Rotate provisioning token").click()
        expect(page.locator("#newProvisioningToken input")).not_to_have_value(token)
        self.assertEqual(api.get(base + "/Users", headers=auth).status, 401)
        rotated = page.locator("#newProvisioningToken input").input_value()
        rotated_auth = auth | {"Authorization": "Bearer " + rotated}
        self.assertTrue(api.get(base + "/Users", headers=rotated_auth).ok)
        page.get_by_role("button", name="Revoke provisioning token").click()
        expect(status).to_have_text("Provisioning is off.")
        self.assertEqual(api.get(base + "/Users", headers=rotated_auth).status, 401)

    def verify_group_claims(self, page, admin, federated, refreshed, start, origin, policy, operator, work):
        api = admin.request
        checkbox = page.get_by_label("Synchronize groups from signed SAML assertions")
        save = page.get_by_role("button", name="Save single sign-on settings")

        def enable(enabled):
            checkbox.set_checked(enabled)
            with page.expect_response(lambda response: response.url == origin + "/api/team/identity"
                                      and response.request.method == "PUT") as settings:
                save.click()
            self.assertTrue(settings.value.ok)

        def sign_in(context):
            workspace = start(context)
            workspace.get_by_role("button", name="Continue as station crew").click()
            expect(workspace).to_have_url(origin + "/team")
            return workspace

        enable(True)
        policy.update(subject="fixture-user", email=EMAIL, groups=("Programming, Broadcast",))
        workspace = sign_in(federated)
        expect(workspace.get_by_text("Station crew · Contributor")).to_be_visible()
        account = origin + "/api/team/account"
        uid = federated.request.get(account).json()["id"]
        team_groups = origin + "/api/team/groups"
        groups = api.get(team_groups, headers=operator).json()
        managed = {group["name"]: group for group in groups["groups"]}
        folder = next(row["id"] for row in groups["folders"] if row["title"] == "Station work")
        for name, role, folders in (("Programming", "contributor_plus", [folder]), ("Broadcast", "viewer", [])):
            group = managed[name]
            self.assertTrue(group["managed"])
            self.assertEqual(group["members"], [uid])
            self.assertTrue(api.put(team_groups + "/" + group["id"], headers=operator,
                                    data=dict(group, default_role=role, folders=folders)).ok)
        media = origin + f"/team/video/{work}/media"
        self.assertEqual(federated.request.get(account).json()["role"], "contributor_plus")
        self.assertTrue(federated.request.get(media).ok)
        member = origin + "/api/team/members/" + uid
        self.assertTrue(api.put(member, headers=operator, data=dict(role="contributor", role_override=True)).ok)
        self.assertEqual(federated.request.get(account).json()["role"], "contributor")
        self.assertTrue(api.put(member, headers=operator, data=dict(role="contributor", role_override=False)).ok)
        self.assertEqual(federated.request.get(account).json()["role"], "contributor_plus")
        policy["groups"] = ("Broadcast",)
        workspace = sign_in(refreshed)
        expect(workspace.get_by_text("Station crew · Viewer")).to_be_visible()
        self.assertEqual(federated.request.get(account).json()["role"], "viewer")
        self.assertEqual(federated.request.get(media).status, 404)
        self.assertEqual(refreshed.request.get(media).status, 404)
        enable(False)
        self.assertEqual(federated.request.get(account).status, 401)
        managed = api.get(team_groups, headers=operator).json()["groups"]
        self.assertTrue(all(not group["members"] for group in managed if group["managed"]))

    def verify_scim_adoption(self, page, admin, local, federated, start, origin, policy, operator,
                             work, uid, owner_email, password):
        api = admin.request
        identity = origin + "/api/team/identity"
        member = origin + "/api/team/account"
        media = origin + f"/team/video/{work}/media"
        address = "existing@station.example"
        directory = origin + "/scim/v2/Users"
        alternate_issuer = ISSUER + ":adopting"

        def save_settings():
            with page.expect_response(lambda response: response.url == identity and response.request.method == "PUT") as saved:
                page.get_by_role("button", name="Save single sign-on settings").click()
            self.assertTrue(saved.value.ok)
            expect(page.locator("#teamSamlSettings")).not_to_have_attribute("aria-busy", "true")

        def federate():
            screen = start(federated)
            screen.get_by_role("button", name="Continue as station crew").click()
            return screen

        page.get_by_label("Require SAML for team members").uncheck()
        save_settings()
        credentials = dict(email=address, password=password)
        self.assertEqual(local.request.post(origin + "/api/team/session", headers=TEAM, data=credentials).json()["id"], uid)
        self.assertTrue(api.put(origin + f"/api/team/members/{uid}/works/{work}", headers=operator, data={}).ok)
        self.assertTrue(local.request.get(media).ok)
        policy.update(subject="existing-producer", email=address)
        expect(federate().get_by_text("Existing producer · Contributor")).to_be_visible()

        issuer = page.get_by_label("Identity provider entity ID")
        issuer.fill(alternate_issuer)
        save_settings()
        self.assertTrue(local.request.get(media).ok)
        page.get_by_role("button", name="Create provisioning token").click()
        expect(page.locator("#teamProvisioningStatus")).to_have_text("Provisioning is enabled.")
        token = page.locator("#newProvisioningToken input").input_value()
        auth = {"Authorization": "Bearer " + token, "Content-Type": "application/scim+json"}
        record = dict(userName=address, externalId="previously-local-001", displayName="Directory producer")
        match = page.get_by_label("Match SCIM users to existing team accounts")
        expect(match).not_to_be_checked()
        self.assertEqual(api.post(directory, headers=auth, data=record).status, 409)
        match.check()
        save_settings()
        self.assertEqual(api.post(directory, headers=auth, data=dict(record, externalId="")).status, 409)
        self.assertEqual(api.post(directory, headers=auth, data=dict(record, userName=owner_email)).status, 409)
        adopted = api.post(directory, headers=auth, data=record)
        self.assertEqual(adopted.status, 201)
        self.assertEqual(adopted.json()["id"], uid)
        self.assertEqual(local.request.get(member).status, 401)
        self.assertFalse(local.request.post(origin + "/api/team/session", headers=TEAM, data=credentials).ok)
        policy["issuer"] = alternate_issuer
        expect(federate().get_by_text("Directory producer · Contributor")).to_be_visible()
        self.assertEqual(federated.request.get(member).json()["id"], uid)
        self.assertTrue(federated.request.get(media).ok)

        resource = adopted.headers["location"]
        for active in (False, True):
            updated = api.patch(resource, headers=auth, data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [dict(op="Replace", path="active", value=active)],
            })
            self.assertIs(updated.json()["active"], active)
            self.assertEqual(federated.request.get(media).status, 401)
        expect(federate().get_by_text("Directory producer · Contributor")).to_be_visible()
        self.assertEqual(federated.request.get(media).status, 404)

        issuer.fill(ISSUER)
        save_settings()
        policy["issuer"] = ISSUER
        expect(federate().get_by_text("This identity is not linked to an eligible workspace member.")).to_be_visible()
        self.assertEqual(federated.request.get(member).status, 401)

    def verify_session_controls(self, admin, owner, local, federated, start, origin, policy,
                                operator, work, owner_email, password):
        settings = origin + "/api/team/session-policy"
        account = origin + "/api/team/account"
        media = origin + f"/team/video/{work}/media"
        workspace = owner.new_page()
        authorized_member = {"Authorization": "Bearer team-session"}

        def sign_in(context, address):
            result = context.request.post(origin + "/api/team/session", headers=TEAM,
                                          data=dict(email=address, password=password))
            self.assertTrue(result.ok)
            return result

        def configure(mode, duration, unit):
            workspace.goto(origin + "/team/session-policy")
            workspace.get_by_label("Require sign-in again").select_option(mode)
            workspace.get_by_label("Duration").fill(str(duration))
            workspace.get_by_label("Unit").select_option(unit)
            workspace.get_by_role("button", name="Save and sign out team").click()
            expect(workspace.get_by_role("status")).to_have_text("Team sessions ended. Members must sign in again.")

        def from_provider():
            policy.update(subject="fixture-user", email=EMAIL, issuer=ISSUER)
            screen = start(federated)
            screen.get_by_role("button", name="Continue as station crew").click()
            expect(screen.get_by_text("Station crew · Contributor")).to_be_visible()

        contributor = sign_in(local, EMAIL).json()["id"]
        self.assertEqual(local.request.get(settings, headers=authorized_member).status, 403)
        self.assertTrue(admin.request.put(origin + f"/api/team/members/{contributor}/works/{work}",
                                          headers=operator, data={}).ok)
        self.assertTrue(local.request.get(media).ok)
        initial = admin.request.get(settings, headers=operator).json()
        self.assertEqual((initial["mode"], initial["duration"], initial["unit"]), ("fixed", 12, "hours"))

        now = time()
        elapsed = 0
        with patch("team_sessions.clock", lambda: now + elapsed):
            configure("fixed", 1, "minutes")
            self.assertEqual(owner.request.get(account).status, 401)
            self.assertEqual(local.request.get(media).status, 401)
            self.assertIn("Max-Age=60", sign_in(local, EMAIL).headers["set-cookie"])
            elapsed = 45
            self.assertTrue(local.request.get(media).ok)
            elapsed = 61
            self.assertEqual(local.request.get(media).status, 401)

            sign_in(owner, owner_email)
            configure("idle", 2, "hours")
            elapsed = 0
            self.assertNotIn("Max-Age=", sign_in(local, EMAIL).headers["set-cookie"])
            from_provider()
            elapsed = 45 * 60
            self.assertTrue(federated.request.get(media).ok)
            self.assertTrue(local.request.get(media).ok)
            elapsed = 65 * 60
            self.assertEqual(federated.request.get(media).status, 401)
            self.assertTrue(local.request.get(media).ok)
            elapsed = 160 * 60
            self.assertTrue(local.request.get(media).ok)
            elapsed = 281 * 60
            self.assertEqual(local.request.get(media).status, 401)

        sign_in(owner, owner_email)
        sign_in(local, EMAIL)
        federated.request.delete(origin + "/api/team/session", headers=TEAM, data={})
        from_provider()
        workspace.goto(origin + "/team/session-policy")
        workspace.get_by_role("button", name="Sign out all team members now").click()
        expect(workspace.get_by_role("status")).to_have_text("Team sessions ended. Members must sign in again.")
        for context in (owner, local, federated):
            self.assertEqual(context.request.get(account).status, 401)
            self.assertEqual(context.request.get(media).status, 401)
        self.assertEqual(sign_in(owner, owner_email).json()["role"], "owner")
        audit = admin.request.get(origin + "/api/team/audit", headers=operator).json()["events"]
        self.assertTrue({"session_policy_changed", "all_team_sessions_ended"} <= {row["action"] for row in audit})


if __name__ == "__main__":
    unittest.main()
