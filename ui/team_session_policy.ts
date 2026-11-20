import {checked, sessionMarker, teamRequest} from "./team_api.js";

interface Policy {revision: string; mode: string; duration: number; unit: string; signed_out?: boolean}

const access = document.querySelector<HTMLFormElement>("#sessionPolicyAccess")!;
const credentialField = access.querySelector<HTMLInputElement>("input")!;
const status = document.querySelector<HTMLElement>("#sessionPolicyStatus")!;
const form = document.querySelector<HTMLFormElement>("#sessionPolicy")!;
const duration = form.querySelector<HTMLInputElement>("input[name=duration]")!;
const mode = form.querySelector<HTMLSelectElement>("select[name=mode]")!;
const unit = form.querySelector<HTMLSelectElement>("select[name=unit]")!;
const logout = document.querySelector<HTMLButtonElement>("#endTeamSessions")!;
let credential = sessionStorage.getItem("cueporchOperator") || sessionMarker;
let revision = "";

async function change(method: "GET" | "PUT" | "POST", body: object = {}) {
  form.inert = true;
  try {
    const response = await checked(await teamRequest("session-policy", method, body, credential));
    const policy: Policy = await response.json();
    revision = policy.revision;
    duration.valueAsNumber = policy.duration;
    mode.value = policy.mode;
    unit.value = policy.unit;
    access.hidden = true;
    form.hidden = Boolean(policy.signed_out && credential === sessionMarker);
    status.textContent = policy.signed_out ? "Team sessions ended. Members must sign in again."
      : method === "PUT" ? "Session settings are already up to date." : "";
  } catch (error) {
    if (method === "GET") access.hidden = false;
    status.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    form.inert = false;
  }
}

access.onsubmit = event => {
  event.preventDefault();
  credential = credentialField.value;
  access.reset();
  void change("GET");
};
form.onsubmit = event => {
  event.preventDefault();
  void change("PUT", {revision, mode: mode.value, duration: duration.valueAsNumber, unit: unit.value});
};
logout.onclick = () => void change("POST");
void change("GET");
