import {sessionMarker} from "./team_api.js";

type Palette = "primary" | "accent" | "icon" | "text" | "background";
export interface BrandClip {work: string; cut: string; start: number; end: number}
export interface BrandKit {
  id: string;
  revision: string;
  name: string;
  style: Record<Palette, string> & {font: string; logo: string | null};
  intro: BrandClip | null;
  outro: BrandClip | null;
}
export interface BrandKits {
  kits: BrandKit[];
  logos: Record<string, string>;
  workspace_logo: string | null;
}
interface LibraryItem {id: string; title: string; asset_type?: string}

const path = "/api/library/brand-kits";
const fields = ["primary", "accent", "icon", "text", "background", "font"] as const;
const access = document.querySelector<HTMLFormElement>("#brandKitAccess")!;
const key = access.querySelector<HTMLInputElement>("input")!;
const status = document.querySelector<HTMLElement>("#brandKitStatus")!;
const editor = document.querySelector<HTMLElement>("#brandKitsWorkspace")!;
const list = document.querySelector<HTMLUListElement>("#brandKitList")!;
const form = document.querySelector<HTMLFormElement>("#brandKitForm")!;
const remove = document.querySelector<HTMLButtonElement>("#deleteBrandKit")!;
const copyLogo = document.querySelector<HTMLButtonElement>("#copyWorkspaceLogo")!;
const removeLogo = document.querySelector<HTMLButtonElement>("#removeKitLogo")!;
const preview = document.querySelector<HTMLImageElement>("#kitLogoPreview")!;
const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement;
const fileInput = field("logo_file");
let library: LibraryItem[] = [];
let shelf: BrandKits;
let kit: BrandKit | undefined;
let logo: string | null = null;
let localLogo: string | null = null;
let busy = false;
const signedInTeam = document.body.dataset.brandKitTeam === "yes";
key.value = signedInTeam ? sessionMarker : sessionStorage.getItem("cueporchOperator") || "";

async function call<T>(url: string, method = "GET", data?: object | FormData): Promise<T> {
  const headers: Record<string, string> = {Authorization: "Bearer " + key.value};
  if (data && !(data instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(url, {method, headers, body: data instanceof FormData ? data : data ? JSON.stringify(data) : undefined});
  if (!response.ok) {
    const error = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
    throw new Error(error?.textContent || `Request failed (${response.status}).`);
  }
  return response.status === 204 ? undefined as T : response.json();
}

async function perform(action: () => Promise<void>) {
  if (busy) return;
  busy = true;
  status.textContent = "";
  try {
    await action();
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    busy = false;
  }
}

function setLocalLogo(source: string | null) {
  if (localLogo) URL.revokeObjectURL(localLogo);
  localLogo = source;
}

function renderLogo() {
  const url = localLogo || (logo && shelf.logos[logo]);
  preview.hidden = !url;
  if (url) preview.src = url;
  else preview.removeAttribute("src");
  copyLogo.hidden = !shelf.workspace_logo;
  removeLogo.hidden = !localLogo && !logo;
}

function selectClip(name: string, clip: BrandClip | null) {
  const select = form.elements.namedItem(name) as HTMLSelectElement;
  select.replaceChildren(new Option("None", ""));
  for (const item of library) select.add(new Option(item.title, item.id));
  if (clip && !library.some(item => item.id === clip.work)) {
    select.add(new Option("Unavailable clip", clip.work, false, true));
  } else select.value = clip?.work || "";
}

function show(value?: BrandKit) {
  kit = value;
  form.reset();
  setLocalLogo(null);
  logo = kit?.style.logo || null;
  if (kit) {
    field("name").value = kit.name;
    for (const setting of fields) field(setting).value = kit.style[setting];
  }
  selectClip("intro", kit?.intro || null);
  selectClip("outro", kit?.outro || null);
  renderLogo();
  remove.hidden = !kit;
  form.hidden = false;
}

async function refresh() {
  shelf = await call<BrandKits>(path);
  list.replaceChildren();
  for (const item of shelf.kits) {
    const row = document.createElement("li");
    const open = document.createElement("button");
    open.type = "button";
    open.textContent = item.name;
    open.onclick = () => { if (!busy) show(item); };
    row.append(open);
    list.append(row);
  }
}

async function openEditor() {
  const items = await call<LibraryItem[]>("/api/library");
  library = items.filter(item => !item.asset_type || item.asset_type === "video");
  await refresh();
  if (!signedInTeam) sessionStorage.setItem("cueporchOperator", key.value);
  editor.hidden = false;
  access.hidden = true;
}

access.onsubmit = event => { event.preventDefault(); void perform(openEditor); };
document.querySelector<HTMLButtonElement>("#newBrandKit")!.onclick = () => { if (!busy) show(); };
fileInput.onchange = () => {
  const file = fileInput.files?.[0];
  setLocalLogo(file ? URL.createObjectURL(file) : null);
  renderLogo();
};
copyLogo.onclick = () => {
  logo = shelf.workspace_logo;
  fileInput.value = "";
  setLocalLogo(null);
  renderLogo();
};
removeLogo.onclick = () => {
  logo = null;
  fileInput.value = "";
  setLocalLogo(null);
  renderLogo();
};
form.onsubmit = event => {
  event.preventDefault();
  void perform(async () => {
    const file = fileInput.files?.[0];
    if (file) {
      const data = new FormData();
      data.append("logo", file);
      logo = (await call<{logo: string}>("/api/review-logos", "POST", data)).logo;
    }
    const style = {...Object.fromEntries(fields.map(name => [name, field(name).value])), logo};
    const payload = {name: field("name").value, style, intro: field("intro").value || null,
      outro: field("outro").value || null, revision: kit?.revision};
    const saved = kit ? await call<BrandKit>(path + "/" + kit.id, "PUT", payload)
      : await call<BrandKit>(path, "POST", payload);
    await refresh();
    show(saved);
    status.textContent = "Brand kit saved.";
  });
};
remove.onclick = () => {
  const current = kit;
  if (!current) return;
  void perform(async () => {
    await call<void>(path + "/" + current.id, "DELETE", {revision: current.revision});
    await refresh();
    form.hidden = true;
    status.textContent = "Brand kit removed.";
  });
};
if (key.value) void perform(openEditor);
