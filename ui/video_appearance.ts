export {};

type Value = string | boolean | null;
type Settings = Record<string, Value>;
interface Appearance {
  title: string;
  settings: Settings;
  is_default: boolean;
  presets: {name: string; settings: Settings}[];
  logos: Record<string, string>;
}

const identify = document.querySelector<HTMLFormElement>("#identify")!;
const credential = document.querySelector<HTMLInputElement>("#appearanceKey")!;
const editor = document.querySelector<HTMLElement>("#appearanceEditor")!;
const surface = document.querySelector<HTMLSelectElement>("#appearanceSurface")!;
const form = document.querySelector<HTMLFormElement>("#appearanceForm")!;
const submit = form.querySelector<HTMLButtonElement>("[type=submit]")!;
const choices = document.querySelector<HTMLSelectElement>("#selectedPreset")!;
const removePreset = document.querySelector<HTMLButtonElement>("#removePreset")!;
const reset = document.querySelector<HTMLButtonElement>("#resetAppearance")!;
const message = document.querySelector<HTMLElement>("#appearanceMessage")!;
const sample = document.querySelector<HTMLElement>(".appearance-preview")!;
const label = document.querySelector<HTMLElement>("#previewTitle")!;
const catalog = document.querySelector<HTMLElement>("#previewCatalog")!;
const image = document.querySelector<HTMLImageElement>("#previewLogo")!;
const video = document.querySelector<HTMLVideoElement>("#appearanceVideo")!;
const clear = document.querySelector<HTMLButtonElement>("#clearLogo")!;
const input = (key: string) => form.elements.namedItem(key) as HTMLInputElement;
const work = "/api/library/work/" + encodeURIComponent(document.body.dataset.work || "");
let stored: Appearance;
let logo: string | null = null;
let temporaryLogo: string | null = null;
let loading = 0;

credential.value = sessionStorage.getItem("cueporchOperator") || "";

async function request<T>(path: string, method = "GET", body?: object | FormData): Promise<T> {
  const headers: Record<string, string> = {Authorization: "Bearer " + credential.value};
  if (body && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method, headers, body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const error = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
    throw new Error(error?.textContent || `Request failed (${response.status})`);
  }
  return response.status === 204 ? undefined as T : response.json();
}

function endpoint() {
  return work + "/appearance/" + surface.value;
}

function discardLocalLogo() {
  if (temporaryLogo) URL.revokeObjectURL(temporaryLogo);
  temporaryLogo = null;
  input("logo_file").value = "";
}

function live() {
  for (const name of ["primary", "accent", "icon", "text", "background"]) {
    sample.style.setProperty("--viewer-" + name, input(name).value);
  }
  sample.dataset.theme = input("theme").value;
  sample.dataset.font = input("font").value;
  sample.dataset.crop = input("crop").checked ? "yes" : "no";
  sample.dataset.ambient = input("ambient").checked ? "yes" : "no";
  label.hidden = !input("show_title").checked;
  catalog.hidden = surface.value !== "watch" || !input("show_chrome").checked;
  const logoUrl = temporaryLogo || (logo && stored.logos[logo]);
  image.hidden = clear.hidden = !logoUrl;
  if (logoUrl) image.src = logoUrl;
  else image.removeAttribute("src");
}

function fill(settings: Settings) {
  discardLocalLogo();
  logo = typeof settings.logo === "string" ? settings.logo : null;
  for (const [key, value] of Object.entries(settings)) {
    const field = input(key);
    if (field) {
      if (typeof value === "boolean") field.checked = value;
      else field.value = value || "";
    }
  }
  input("preset_name").value = "";
  input("apply_page_to_library").checked = false;
  live();
}

function render(value: Appearance) {
  stored = value;
  label.textContent = value.title;
  document.querySelector("#appearanceTitle")!.textContent = "Appearance for " + value.title;
  choices.replaceChildren(new Option("Current settings", ""),
                          ...value.presets.map((preset) => new Option(preset.name, preset.name)));
  removePreset.disabled = true;
  for (const field of form.querySelectorAll<HTMLElement>("[data-watch-only]")) {
    field.hidden = surface.value !== "watch";
  }
  fill(value.settings);
  reset.disabled = value.is_default;
}

async function load() {
  const current = ++loading;
  const value = await request<Appearance>(endpoint());
  if (current !== loading) return;
  render(value);
}

function error(value: unknown) {
  message.textContent = value instanceof Error ? value.message : String(value);
}

async function signIn() {
  try {
    await load();
    sessionStorage.setItem("cueporchOperator", credential.value);
    identify.hidden = true;
    editor.hidden = false;
    const info = await request<{preview: string | null}>(work);
    if (info.preview && !video.getAttribute("src")) {
      const ticket = await request<{preview: string}>(work + "/ticket", "POST", {sha256: info.preview});
      video.src = ticket.preview;
      video.hidden = false;
    }
  } catch (value) {
    error(value);
  }
}

async function change(action: () => Promise<void>, success: string) {
  surface.disabled = submit.disabled = choices.disabled = true;
  try {
    await action();
    message.textContent = success;
  } catch (value) {
    error(value);
  } finally {
    surface.disabled = submit.disabled = choices.disabled = false;
  }
}

identify.onsubmit = (event) => { event.preventDefault(); void signIn(); };
surface.onchange = () => { void load().catch(error); };
choices.onchange = () => {
  const preset = stored.presets.find((entry) => entry.name === choices.value);
  removePreset.disabled = !preset;
  fill(preset?.settings || stored.settings);
};
removePreset.onclick = () => void change(async () => {
  const path = "/api/library/appearance-preset/" + surface.value + "/" + encodeURIComponent(choices.value);
  await request<void>(path, "DELETE");
  await load();
}, "Preset removed.");
reset.onclick = () => void change(async () => {
  render(await request<Appearance>(endpoint(), "DELETE"));
}, "Default appearance restored.");

input("logo_file").onchange = () => {
  const file = input("logo_file").files?.[0];
  if (temporaryLogo) URL.revokeObjectURL(temporaryLogo);
  temporaryLogo = file ? URL.createObjectURL(file) : null;
  live();
};
clear.onclick = () => { discardLocalLogo(); logo = null; live(); };
input("theme").addEventListener("change", () => {
  const dark = input("theme").value === "dark";
  if (["#112024", "#f5f5ee"].includes(input("background").value)) input("background").value = dark ? "#112024" : "#f5f5ee";
  if (["#e5efef", "#152e33"].includes(input("text").value)) input("text").value = dark ? "#e5efef" : "#152e33";
  live();
});
form.addEventListener("input", live);

form.onsubmit = (event) => {
  event.preventDefault();
  void change(async () => {
    const settings: Settings = {};
    for (const [key, value] of Object.entries(stored.settings)) {
      const field = input(key);
      settings[key] = field ? (typeof value === "boolean" ? field.checked : field.value) : value;
    }
    const file = input("logo_file").files?.[0];
    if (file) {
      const upload = new FormData();
      upload.append("logo", file);
      logo = (await request<{logo: string}>("/api/review-logos", "POST", upload)).logo;
    }
    settings.logo = logo;
    if (input("preset_name").value.trim()) settings.preset_name = input("preset_name").value.trim();
    settings.apply_page_to_library = input("apply_page_to_library").checked;
    render(await request<Appearance>(endpoint(), "PUT", settings));
  }, "Appearance saved.");
};
if (credential.value) void signIn();
