import { checked, teamRequest } from "./team_api.js";
const access = document.querySelector("#teamIdentityAccess");
const credential = access.querySelector("input");
const settings = document.querySelector("#teamSamlSettings");
const feedback = document.querySelector("#teamIdentityStatus");
const provisioning = document.querySelector("#teamProvisioning");
const provisioningStatus = document.querySelector("#teamProvisioningStatus");
const endpoint = document.querySelector("#teamProvisioningEndpoint");
const createToken = document.querySelector("#createProvisioningToken");
const revokeToken = document.querySelector("#revokeProvisioningToken");
const tokenLabel = document.querySelector("#newProvisioningToken");
const tokenValue = tokenLabel.querySelector("input");
let authorization = sessionStorage.getItem("cueporchOperator") || "";
let revision = "";
let busy = false;
function field(name) {
    return settings.elements.namedItem(name);
}
function render(result) {
    revision = result.revision;
    if (field("enabled")) {
        for (const key of ["enabled", "jit", "force", "idp_initiated", "sync_groups"]) {
            field(key).checked = result.settings[key];
        }
        for (const key of ["entity_id", "login_url", "signing_certificate", "email_attribute", "groups_attribute", "default_role"]) {
            field(key).value = result.settings[key];
        }
        field("domains").value = result.settings.domains.join("\n");
    }
    access.hidden = true;
    settings.hidden = false;
    feedback.textContent = result.settings.enabled ? "Single sign-on is enabled." : "Single sign-on is off.";
}
async function update(method, body = {}) {
    if (busy)
        return;
    busy = true;
    settings.setAttribute("aria-busy", "true");
    try {
        const response = await checked(await teamRequest("identity", method, body, authorization));
        render(await response.json());
        await provisioningRequest("GET");
    }
    catch (error) {
        feedback.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        busy = false;
        settings.removeAttribute("aria-busy");
    }
}
async function provisioningRequest(method) {
    const response = await checked(await teamRequest("identity/provisioning", method, {}, authorization));
    const result = await response.json();
    provisioning.hidden = !result.available;
    endpoint.value = result.endpoint;
    createToken.textContent = result.active ? "Rotate provisioning token" : "Create provisioning token";
    revokeToken.hidden = !result.active;
    tokenLabel.hidden = !result.token;
    tokenValue.value = result.token || "";
    provisioningStatus.textContent = result.active ? "Provisioning is enabled." : "Provisioning is off.";
    if (result.token)
        tokenValue.select();
}
async function changeProvisioning(method) {
    if (busy)
        return;
    busy = true;
    try {
        await provisioningRequest(method);
    }
    catch (error) {
        provisioningStatus.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        busy = false;
    }
}
createToken.onclick = () => void changeProvisioning("POST");
revokeToken.onclick = () => void changeProvisioning("DELETE");
access.onsubmit = event => {
    event.preventDefault();
    authorization = credential.value;
    access.reset();
    void update("GET");
};
settings.onsubmit = event => {
    event.preventDefault();
    void update("PUT", {
        revision,
        enabled: field("enabled").checked,
        entity_id: field("entity_id").value,
        login_url: field("login_url").value,
        signing_certificate: field("signing_certificate").value,
        email_attribute: field("email_attribute").value,
        jit: field("jit").checked,
        force: field("force").checked,
        idp_initiated: field("idp_initiated").checked,
        sync_groups: field("sync_groups").checked,
        groups_attribute: field("groups_attribute").value,
        default_role: field("default_role").value,
        domains: field("domains").value.split(/\r?\n/).map(line => line.trim()).filter(Boolean),
    });
};
if (authorization)
    void update("GET");
