import {checked, teamRequest} from "./team_api.js";

interface Brand {label: string; accent: string; logo: string | null; revision: string}

const access = document.querySelector<HTMLFormElement>("#teamSettingsAccess")!;
const key = access.querySelector<HTMLInputElement>("input")!;
const status = document.querySelector<HTMLElement>("#teamSettingsStatus")!;
const form = document.querySelector<HTMLFormElement>("#teamBranding")!;
const submit = form.querySelector<HTMLButtonElement>("button")!;
const name = form.querySelector<HTMLInputElement>("[name=name]")!;
const accent = form.querySelector<HTMLInputElement>("[name=accent]")!;
const logo = document.querySelector<HTMLImageElement>("#workspaceLogoPreview")!;
const removeLogo = document.querySelector<HTMLElement>("#removeWorkspaceLogo")!;
let revision = "";

function show(brand: Brand) {
  form.reset();
  name.value = brand.label;
  accent.value = brand.accent;
  logo.hidden = removeLogo.hidden = !brand.logo;
  if (brand.logo) logo.src = brand.logo;
  else logo.removeAttribute("src");
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
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  }
}

access.onsubmit = event => {
  event.preventDefault();
  void load();
};
form.onsubmit = async event => {
  event.preventDefault();
  submit.disabled = true;
  status.textContent = "";
  const body = new FormData(form);
  body.set("revision", revision);
  try {
    const response = await checked(await fetch("/api/team/settings", {
      method: "PUT", headers: {Authorization: `Bearer ${key.value}`, "X-CuePorch-Team": "1"}, body,
    }));
    show(await response.json());
    status.textContent = "Workspace branding saved.";
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    submit.disabled = false;
  }
};
key.value = sessionStorage.getItem("cueporchOperator") || "";
if (key.value) void load();
