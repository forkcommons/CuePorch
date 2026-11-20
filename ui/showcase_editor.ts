import { setupRegistration } from "./video_registration.js";

type Value = string | boolean | null | string[];
type Settings = Record<string, Value> & {title: string; logo: string | null; videos: string[]; domains: string[]};
interface Showcase {
  id: string;
  slug: string;
  revision: string;
  settings: Settings;
  has_password: boolean;
  watch: string | null;
  embed: string | null;
}
interface Work {id: string; title: string; asset_type: string;}

const identify = document.querySelector<HTMLFormElement>("#showcaseIdentify")!;
const credential = identify.elements.namedItem("credential") as HTMLInputElement;
const workspace = document.querySelector<HTMLElement>("#showcaseWorkspace")!;
const editor = document.querySelector<HTMLElement>("#showcaseSetup")!;
const form = document.querySelector<HTMLFormElement>("#showcaseEdit")!;
const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
const save = form.querySelector<HTMLButtonElement>("[type=submit]")!;
const message = document.querySelector<HTMLElement>("#showcaseStatus")!;
const picker = document.querySelector<HTMLSelectElement>("#showcaseList")!;
const video = document.querySelector<HTMLSelectElement>("#showcaseVideo")!;
const members = document.querySelector<HTMLOListElement>("#showcaseMembers")!;
const add = document.querySelector<HTMLButtonElement>("#showcaseAdd")!;
const frame = document.querySelector<HTMLIFrameElement>("#showcaseFrame")!;
const sharing = document.querySelector<HTMLElement>("#showcaseSharing")!;
const link = document.querySelector<HTMLInputElement>("#showcaseLink")!;
const code = document.querySelector<HTMLInputElement>("#showcaseCode")!;
const logoInput = field("logo_file") as HTMLInputElement;
const clear = document.querySelector<HTMLButtonElement>("#showcaseClearLogo")!;
const base = document.body.dataset.publicUrl || location.origin;

let current: Showcase;
let showcases: Showcase[] = [];
let library: Work[] = [];
let selected: string[] = [];
let logo: string | null = null;
let busy = false;
credential.value = sessionStorage.getItem("cueporchOperator") || "";

async function fetchResult(path: string, method = "GET", body?: object | FormData) {
  const headers: Record<string, string> = {Authorization: "Bearer " + credential.value};
  const payload = body instanceof FormData ? body : body ? JSON.stringify(body) : undefined;
  if (body && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {method, headers, body: payload});
  if (!response.ok) {
    const explanation = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
    throw new Error(explanation?.textContent || `Request failed (${response.status})`);
  }
  return response;
}

async function request<T>(path: string, method = "GET", body?: object | FormData): Promise<T> {
  const response = await fetchResult(path, method, body);
  return response.status === 204 ? undefined as T : response.json();
}

function endpoint() { return "/api/showcases/" + encodeURIComponent(current.id); }

const registration = document.querySelector<HTMLFormElement>("#registrationEditor")!;
const openRegistration = setupRegistration(
  (suffix, method, values) => request(endpoint() + suffix, method, values),
  async () => {
    const result = await fetchResult(endpoint() + "/registration/export");
    const url = URL.createObjectURL(await result.blob());
    const download = document.createElement("a");
    download.href = url;
    download.download = "cueporch-showcase-registrants.csv";
    download.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  () => credential.value,
);

function membership() {
  const included = new Set(selected);
  const titles = new Map(library.map((work) => [work.id, work.title]));
  video.replaceChildren(...library.filter((work) => !included.has(work.id)).map((work) => new Option(work.title, work.id)));
  add.disabled = video.options.length === 0;
  members.replaceChildren(...selected.map((id, index) => {
    const row = document.createElement("li");
    const title = titles.get(id) || "Unavailable video";
    row.append(document.createTextNode(title + " "));
    function button(text: string, disabled: boolean, handle: () => void) {
      const control = document.createElement("button");
      control.type = "button";
      control.textContent = text;
      control.setAttribute("aria-label", text + " " + title);
      control.disabled = disabled;
      control.onclick = () => { handle(); membership(); };
      row.append(control);
    }
    button("Move up", index === 0, () => { selected.splice(index, 1); selected.splice(index - 1, 0, id); });
    button("Move down", index === selected.length - 1, () => { selected.splice(index, 1); selected.splice(index + 1, 0, id); });
    button("Remove", false, () => { selected.splice(index, 1); });
    return row;
  }));
}

function visibility() {
  const mode = field("visibility").value;
  document.querySelector<HTMLElement>("#showcasePassword")!.hidden = mode !== "password";
  document.querySelector<HTMLElement>("#showcaseDomains")!.hidden = mode === "private";
}

function select(value: Showcase) {
  if (current?.id !== value.id) registration.hidden = true;
  current = value;
  picker.value = value.id;
  for (const [name, setting] of Object.entries(value.settings)) {
    const input = field(name);
    if (!input) continue;
    if (typeof setting === "boolean") (input as HTMLInputElement).checked = setting;
    else input.value = Array.isArray(setting) ? setting.join("\n") : setting || "";
  }
  field("slug").value = value.slug;
  field("password").value = "";
  (field("password") as HTMLInputElement).placeholder = value.has_password ? "Keep current password" : "";
  logoInput.value = "";
  logo = value.settings.logo;
  clear.hidden = !logo;
  selected = [...value.settings.videos];
  frame.hidden = true;
  frame.removeAttribute("src");
  sharing.hidden = !value.watch && !value.embed;
  link.value = value.watch ? new URL(value.watch, base).href : "";
  code.value = value.embed ? `<iframe src="${new URL(value.embed, base).href}" title="Video showcase" width="960" height="640" allowfullscreen></iframe>` : "";
  link.parentElement!.hidden = !value.watch;
  code.parentElement!.hidden = !value.embed;
  visibility();
  membership();
  editor.hidden = false;
}

function updateList(focus?: string) {
  picker.replaceChildren(...showcases.map((value) => new Option(value.settings.title, value.id)));
  const value = showcases.find((entry) => entry.id === focus) || showcases[0];
  if (value) select(value);
  else editor.hidden = true;
}

async function action(work: () => Promise<void>, success = "") {
  if (busy) return;
  busy = true;
  message.textContent = "";
  picker.disabled = save.disabled = true;
  const actions = Array.from(document.querySelectorAll<HTMLButtonElement>("[data-showcase-action]"));
  for (const button of actions) button.disabled = true;
  try {
    await work();
    message.textContent = success;
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    busy = picker.disabled = save.disabled = false;
    for (const button of actions) button.disabled = false;
  }
}

async function signIn() {
  showcases = await request<Showcase[]>("/api/showcases");
  library = (await request<Work[]>("/api/library")).filter((work) => work.asset_type === "video");
  sessionStorage.setItem("cueporchOperator", credential.value);
  updateList(new URLSearchParams(location.search).get("select") || undefined);
  workspace.hidden = false;
  identify.hidden = true;
}

identify.onsubmit = (event) => { event.preventDefault(); void action(signIn); };
if (credential.value) void action(signIn);
picker.onchange = () => updateList(picker.value);
field("visibility").onchange = visibility;
add.onclick = () => { selected.push(video.value); membership(); };
logoInput.onchange = () => { clear.hidden = !logo && !logoInput.files?.length; };
clear.onclick = () => { logo = null; logoInput.value = ""; clear.hidden = true; };

const on = (id: string, work: () => Promise<void>, success = "") => {
  document.querySelector<HTMLButtonElement>("#" + id)!.onclick = () => void action(work, success);
};

async function addShowcase(path: string) {
  const value = await request<Showcase>(path, "POST");
  showcases.unshift(value);
  updateList(value.id);
}

on("showcaseNew", () => addShowcase("/api/showcases"), "Showcase created.");
on("showcaseDuplicate", () => addShowcase(endpoint()), "A private copy was created.");
on("showcaseDelete", async () => {
  await request<void>(endpoint(), "DELETE");
  showcases = showcases.filter((value) => value.id !== current.id);
  updateList();
}, "Showcase deleted. Its videos remain in the library.");
on("showcaseRegistration", openRegistration);
on("showcasePreview", async () => {
  const value = await request<{preview: string}>(endpoint() + "/preview", "POST");
  frame.hidden = false;
  frame.src = value.preview;
});
const viewport = document.querySelector<HTMLSelectElement>("#showcaseViewport")!;
viewport.onchange = () => { frame.dataset.width = viewport.value; };

form.onsubmit = (event) => {
  event.preventDefault();
  void action(async () => {
    if (logoInput.files?.length) {
      const file = new FormData();
      file.set("logo", logoInput.files[0]);
      logo = (await request<{logo: string}>("/api/review-logos", "POST", file)).logo;
    }
    const settings: Settings = {...current.settings, logo, videos: selected, domains: field("domains").value.split(/\s+/).filter(Boolean)};
    for (const [name, old] of Object.entries(settings)) {
      if (name === "domains" || name === "videos" || name === "logo") continue;
      const control = field(name);
      settings[name] = typeof old === "boolean" ? (control as HTMLInputElement).checked : control.value;
    }
    const updated = await request<Showcase>(endpoint(), "PUT", {revision: current.revision, slug: field("slug").value,
      settings, password: field("password").value});
    showcases = showcases.map((value) => value.id === updated.id ? updated : value);
    updateList(updated.id);
  }, "Showcase saved.");
};
