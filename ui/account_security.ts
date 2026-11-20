import {viewerRequest} from "./audience_api.js";
import {teamRequest} from "./team_api.js";

type Method = "GET" | "POST" | "PUT" | "DELETE";

const security = document.querySelector<HTMLElement>("#accountSecurity")!;
const status = document.querySelector<HTMLElement>("#authenticatorStatus")!;
const begin = document.querySelector<HTMLFormElement>("#authenticatorStart")!;
const confirmation = document.querySelector<HTMLFormElement>("#authenticatorConfirm")!;
const disable = document.querySelector<HTMLFormElement>("#authenticatorDisable")!;
const recovery = document.querySelector<HTMLElement>("#authenticatorRecovery")!;
const seed = document.querySelector<HTMLInputElement>("#authenticatorSeed")!;
const openApp = document.querySelector<HTMLAnchorElement>("#authenticatorOpen")!;
let setupTicket = "";
let busy = false;

async function request(method: Method, body: object = {}) {
  const send = security.dataset.scope === "team" ? teamRequest : viewerRequest;
  const response = await send("authenticator", method, body);
  if (response.ok) return response.json();
  const detail = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p")?.textContent;
  if (method === "GET" && response.status === 401) throw new Error("Sign in to manage your account security.");
  throw new Error(detail || "CuePorch could not change the authenticator.");
}

async function perform(action: () => Promise<void>) {
  if (busy) return;
  busy = true;
  security.setAttribute("aria-busy", "true");
  try { await action(); } catch (error) { status.textContent = error instanceof Error ? error.message : String(error); }
  finally { busy = false; security.removeAttribute("aria-busy"); }
}

function disposeSetup() {
  setupTicket = "";
  confirmation.reset();
  seed.value = "";
  openApp.removeAttribute("href");
  confirmation.hidden = true;
}

function state(enabled: boolean) {
  disposeSetup();
  begin.hidden = enabled;
  disable.hidden = !enabled;
  status.textContent = enabled ? "Authenticator sign-in is on." : "Authenticator sign-in is off.";
}

function bind(form: HTMLFormElement, action: (values: object) => Promise<void>) {
  form.addEventListener("submit", event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    form.reset();
    void perform(() => action(values));
  });
}

bind(begin, async values => {
  const prepared = await request("POST", values);
  setupTicket = prepared.setup_ticket;
  seed.value = prepared.setup_key;
  openApp.href = prepared.app_link;
  begin.hidden = true;
  confirmation.hidden = false;
  status.textContent = "Enter a code from the app to confirm setup.";
  seed.focus();
});

bind(confirmation, async values => {
  const enabled = await request("PUT", {...values, setup_ticket: setupTicket});
  state(true);
  const list = recovery.querySelector("ul")!;
  list.replaceChildren(...enabled.recovery_codes.map((code: string) => {
    const item = document.createElement("li");
    item.textContent = code;
    return item;
  }));
  recovery.hidden = false;
});

bind(disable, async values => {
  await request("DELETE", values);
  recovery.hidden = true;
  recovery.querySelector("ul")!.replaceChildren();
  state(false);
});

document.querySelector<HTMLButtonElement>("#authenticatorCancel")!.onclick = () => {
  if (!busy) state(false);
};
void perform(async () => state((await request("GET")).enabled));
