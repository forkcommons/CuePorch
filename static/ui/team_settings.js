import { checked, teamRequest } from "./team_api.js";
const access = document.querySelector("#teamSettingsAccess");
const key = access.querySelector("input");
const status = document.querySelector("#teamSettingsStatus");
const form = document.querySelector("#teamBranding");
const submit = form.querySelector("button");
const name = form.querySelector("[name=name]");
const accent = form.querySelector("[name=accent]");
const logo = document.querySelector("#workspaceLogoPreview");
const removeLogo = document.querySelector("#removeWorkspaceLogo");
let revision = "";
function show(brand) {
    form.reset();
    name.value = brand.label;
    accent.value = brand.accent;
    logo.hidden = removeLogo.hidden = !brand.logo;
    if (brand.logo)
        logo.src = brand.logo;
    else
        logo.removeAttribute("src");
    revision = brand.revision;
    access.hidden = true;
    form.hidden = false;
}
async function load() {
    try {
        const response = await checked(await teamRequest("settings", "GET", {}, key.value));
        show(await response.json());
        sessionStorage.setItem("cueporchOperator", key.value);
        status.textContent = "";
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
}
access.onsubmit = event => {
    event.preventDefault();
    void load();
};
form.onsubmit = async (event) => {
    event.preventDefault();
    submit.disabled = true;
    status.textContent = "";
    const body = new FormData(form);
    body.set("revision", revision);
    try {
        const response = await checked(await fetch("/api/team/settings", {
            method: "PUT", headers: { Authorization: `Bearer ${key.value}`, "X-CuePorch-Team": "1" }, body,
        }));
        show(await response.json());
        status.textContent = "Workspace branding saved.";
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        submit.disabled = false;
    }
};
key.value = sessionStorage.getItem("cueporchOperator") || "";
if (key.value)
    void load();
