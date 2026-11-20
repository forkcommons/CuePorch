import {checked, teamRequest} from "./team_api.js";

type Settings = {
  enabled: boolean;
  entity_id: string;
  login_url: string;
  signing_certificate: string;
  email_attribute: string;
  domains: string[];
  jit: boolean;
  force: boolean;
  default_role: string;
};

const access = document.querySelector<HTMLFormElement>("#teamIdentityAccess")!;
const credential = access.querySelector<HTMLInputElement>("input")!;
const settings = document.querySelector<HTMLFormElement>("#teamSamlSettings")!;
const feedback = document.querySelector<HTMLElement>("#teamIdentityStatus")!;
const provisioning = document.querySelector<HTMLElement>("#teamProvisioning")!;
const provisioningStatus = document.querySelector<HTMLElement>("#teamProvisioningStatus")!;
const endpoint = document.querySelector<HTMLInputElement>("#teamProvisioningEndpoint")!;
const createToken = document.querySelector<HTMLButtonElement>("#createProvisioningToken")!;
const revokeToken = document.querySelector<HTMLButtonElement>("#revokeProvisioningToken")!;
const tokenLabel = document.querySelector<HTMLLabelElement>("#newProvisioningToken")!;
const tokenValue = tokenLabel.querySelector("input")!;
let authorization = sessionStorage.getItem("cueporchOperator") || "";
let revision = "";
let busy = false;

function field(name: keyof Settings) {
  return settings.elements.namedItem(name) as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
}

function render(result: {revision: string; settings: Settings}) {
  revision = result.revision;
  if (field("enabled")) {
    for (const key of ["enabled", "jit", "force"] as const) {
      (field(key) as HTMLInputElement).checked = result.settings[key];
    }
    for (const key of ["entity_id", "login_url", "signing_certificate", "email_attribute", "default_role"] as const) {
      field(key).value = result.settings[key];
    }
    field("domains").value = result.settings.domains.join("\n");
  }
  access.hidden = true;
  settings.hidden = false;
  feedback.textContent = result.settings.enabled ? "Single sign-on is enabled." : "Single sign-on is off.";
}

async function update(method: "GET" | "PUT", body: object = {}) {
  if (busy) return;
  busy = true;
  settings.setAttribute("aria-busy", "true");
  try {
    const response = await checked(await teamRequest("identity", method, body, authorization));
    render(await response.json());
    await provisioningRequest("GET");
  } catch (error) {
    feedback.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    busy = false;
    settings.removeAttribute("aria-busy");
  }
}

async function provisioningRequest(method: "GET" | "POST" | "DELETE") {
  const response = await checked(await teamRequest("identity/provisioning", method, {}, authorization));
  const result = await response.json();
  provisioning.hidden = !result.available;
  endpoint.value = result.endpoint;
  createToken.textContent = result.active ? "Rotate provisioning token" : "Create provisioning token";
  revokeToken.hidden = !result.active;
  tokenLabel.hidden = !result.token;
  tokenValue.value = result.token || "";
  provisioningStatus.textContent = result.active ? "Provisioning is enabled." : "Provisioning is off.";
  if (result.token) tokenValue.select();
}

async function changeProvisioning(method: "POST" | "DELETE") {
  if (busy) return;
  busy = true;
  try {
    await provisioningRequest(method);
  } catch (error) {
    provisioningStatus.textContent = error instanceof Error ? error.message : String(error);
  } finally {
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
    enabled: (field("enabled") as HTMLInputElement).checked,
    entity_id: field("entity_id").value,
    login_url: field("login_url").value,
    signing_certificate: field("signing_certificate").value,
    email_attribute: field("email_attribute").value,
    jit: (field("jit") as HTMLInputElement).checked,
    force: (field("force") as HTMLInputElement).checked,
    default_role: field("default_role").value,
    domains: field("domains").value.split(/\r?\n/).map(line => line.trim()).filter(Boolean),
  });
};

if (authorization) void update("GET");
