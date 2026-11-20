import {checked, teamRequest} from "./team_api.js";

type Settings = {
  enabled: boolean;
  entity_id: string;
  login_url: string;
  signing_certificate: string;
  email_attribute: string;
  domains: string[];
};

const access = document.querySelector<HTMLFormElement>("#teamIdentityAccess")!;
const credential = access.querySelector<HTMLInputElement>("input")!;
const settings = document.querySelector<HTMLFormElement>("#teamSamlSettings")!;
const feedback = document.querySelector<HTMLElement>("#teamIdentityStatus")!;
let authorization = sessionStorage.getItem("cueporchOperator") || "";
let revision = "";
let busy = false;

function field(name: keyof Settings) {
  return settings.elements.namedItem(name) as HTMLInputElement | HTMLTextAreaElement;
}

function render(result: {revision: string; settings: Settings}) {
  revision = result.revision;
  if (field("enabled")) {
    (field("enabled") as HTMLInputElement).checked = result.settings.enabled;
    for (const key of ["entity_id", "login_url", "signing_certificate", "email_attribute"] as const) {
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
  } catch (error) {
    feedback.textContent = error instanceof Error ? error.message : String(error);
  } finally {
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
    enabled: (field("enabled") as HTMLInputElement).checked,
    entity_id: field("entity_id").value,
    login_url: field("login_url").value,
    signing_certificate: field("signing_certificate").value,
    email_attribute: field("email_attribute").value,
    domains: field("domains").value.split(/\r?\n/).map(line => line.trim()).filter(Boolean),
  });
};

if (authorization) void update("GET");
