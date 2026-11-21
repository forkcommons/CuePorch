import { checked, sessionMarker, teamRequest } from "./team_api.js";
const access = document.querySelector("#sessionPolicyAccess");
const credentialField = access.querySelector("input");
const status = document.querySelector("#sessionPolicyStatus");
const form = document.querySelector("#sessionPolicy");
const duration = form.querySelector("input[name=duration]");
const mode = form.querySelector("select[name=mode]");
const unit = form.querySelector("select[name=unit]");
const logout = document.querySelector("#endTeamSessions");
let credential = sessionStorage.getItem("cueporchOperator") || sessionMarker;
let revision = "";
async function change(method, body = {}) {
    form.inert = true;
    try {
        const response = await checked(await teamRequest("session-policy", method, body, credential));
        const policy = await response.json();
        revision = policy.revision;
        duration.valueAsNumber = policy.duration;
        mode.value = policy.mode;
        unit.value = policy.unit;
        access.hidden = true;
        form.hidden = Boolean(policy.signed_out && credential === sessionMarker);
        status.textContent = policy.signed_out ? "Team sessions ended. Members must sign in again."
            : method === "PUT" ? "Session settings are already up to date." : "";
    }
    catch (error) {
        if (method === "GET")
            access.hidden = false;
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
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
    void change("PUT", { revision, mode: mode.value, duration: duration.valueAsNumber, unit: unit.value });
};
logout.onclick = () => void change("POST");
void change("GET");
