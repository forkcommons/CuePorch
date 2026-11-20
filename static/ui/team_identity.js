import { checked, teamRequest } from "./team_api.js";
const access = document.querySelector("#teamIdentityAccess");
const credential = access.querySelector("input");
const settings = document.querySelector("#teamSamlSettings");
const feedback = document.querySelector("#teamIdentityStatus");
let authorization = sessionStorage.getItem("cueporchOperator") || "";
let revision = "";
let busy = false;
function field(name) {
    return settings.elements.namedItem(name);
}
function render(result) {
    revision = result.revision;
    if (field("enabled")) {
        for (const key of ["enabled", "jit", "force"]) {
            field(key).checked = result.settings[key];
        }
        for (const key of ["entity_id", "login_url", "signing_certificate", "email_attribute", "default_role"]) {
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
    }
    catch (error) {
        feedback.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        busy = false;
        settings.removeAttribute("aria-busy");
    }
}
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
        default_role: field("default_role").value,
        domains: field("domains").value.split(/\r?\n/).map(line => line.trim()).filter(Boolean),
    });
};
if (authorization)
    void update("GET");
