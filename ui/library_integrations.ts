import {checked, sessionMarker} from "./team_api.js";

interface Integration {
  id: string;
  name: string;
  category: string;
  description: string;
  configured: boolean;
  connected: boolean;
  browse_folders: boolean;
}
interface DriveFile {id: string; name: string}
interface Folder {id: string; name: string; parent: string | null}

const access = document.querySelector<HTMLFormElement>("#integrationAccess")!;
const key = access.querySelector<HTMLInputElement>("input")!;
const status = document.querySelector<HTMLElement>("#integrationStatus")!;
const directory = document.querySelector<HTMLElement>("#integrationDirectory")!;
const discover = document.querySelector<HTMLButtonElement>("#discoverIntegrations")!;
const connected = document.querySelector<HTMLButtonElement>("#connectedIntegrations")!;
const search = document.querySelector<HTMLInputElement>("#findIntegration")!;
const category = document.querySelector<HTMLSelectElement>("#integrationCategory")!;
const list = document.querySelector<HTMLUListElement>("#integrationList")!;
const empty = document.querySelector<HTMLElement>("#integrationEmpty")!;
const picker = document.querySelector<HTMLElement>("#drivePicker")!;
const pickerHeading = document.querySelector<HTMLElement>("#drivePickerHeading")!;
const remoteFolder = document.querySelector<HTMLElement>("#cloudFolder")!;
const remoteUp = document.querySelector<HTMLButtonElement>("#cloudFolderUp")!;
const remotePath = document.querySelector<HTMLElement>("#cloudFolderPath")!;
const driveSearch = document.querySelector<HTMLFormElement>("#driveSearch")!;
const videoName = driveSearch.querySelector<HTMLInputElement>("input")!;
const destination = document.querySelector<HTMLSelectElement>("#driveDestination")!;
const videos = document.querySelector<HTMLUListElement>("#driveVideos")!;
const noVideos = document.querySelector<HTMLElement>("#driveEmpty")!;
const more = document.querySelector<HTMLButtonElement>("#driveMore")!;
const receipt = document.querySelector<HTMLElement>("#driveReceipt")!;
const params = new URLSearchParams(location.search);
let credential = sessionStorage.getItem("cueporchOperator") || sessionMarker;
if (params.get("team") === "1" || (params.has("connection") && sessionStorage.getItem("cueporchCloudTeam"))) {
  credential = sessionMarker;
}
sessionStorage.removeItem("cueporchCloudTeam");
let integrations: Integration[] = [];
let onlyConnected = false;
let cursor: string | null = null;
let query = "";
let busy = false;
let active: Integration;
let source: DriveFile[] = [];

function pathFor(item: Integration, action: string) {
  return `/api/integrations/${encodeURIComponent(item.id)}/${action}`;
}

function message(error: unknown) {
  status.textContent = error instanceof Error ? error.message : String(error);
}

async function api<T>(path: string, body?: object): Promise<T> {
  const headers: Record<string, string> = {Authorization: `Bearer ${credential}`};
  if (body) {
    headers["Content-Type"] = "application/json";
    headers["X-CuePorch-Integration"] = "1";
  }
  const response = await checked(await fetch(path, {
    method: body ? "POST" : "GET", headers, body: body ? JSON.stringify(body) : undefined,
  }));
  return response.json();
}

function button(label: string, action: () => Promise<void>) {
  const control = document.createElement("button");
  control.type = "button";
  control.textContent = label;
  control.onclick = () => void run(action);
  return control;
}

async function run(action: () => Promise<void>) {
  if (busy) return;
  busy = true;
  status.textContent = "";
  const controls = [...list.querySelectorAll("button"), ...picker.querySelectorAll("button")];
  for (const control of controls) control.disabled = true;
  try {
    await action();
  } catch (error) {
    message(error);
  } finally {
    busy = false;
    for (const control of controls) control.disabled = false;
  }
}

function render() {
  discover.setAttribute("aria-pressed", String(!onlyConnected));
  connected.setAttribute("aria-pressed", String(onlyConnected));
  const words = search.value.toLowerCase().trim().split(/\s+/);
  const cards = integrations.filter(item => (!onlyConnected || item.connected)
    && (!category.value || category.value === item.category)
    && words.every(word => `${item.name} ${item.description}`.toLowerCase().includes(word)));
  empty.hidden = cards.length > 0;
  list.replaceChildren(...cards.map(item => {
    const card = document.createElement("li");
    const name = document.createElement("h2");
    name.textContent = item.name;
    const description = document.createElement("p");
    description.textContent = item.description;
    const state = document.createElement("p");
    state.textContent = item.connected ? "Connected" : item.configured ? "Available" : "Setup required";
    card.append(name, description, state);
    if (item.connected) {
      if (item.configured) card.append(button("Browse videos", () => openPicker(item)));
      card.append(button(`Disconnect ${item.name}`, async () => {
        const result = await api<{provider_revoked: boolean}>(pathFor(item, "disconnect"), {});
        if (active === item) picker.hidden = true;
        item.connected = false;
        render();
        status.textContent = result.provider_revoked
          ? `${item.name} disconnected.`
          : `Disconnected here. You can remove CuePorch access in your ${item.name} account.`;
      }));
    } else if (item.configured) {
      card.append(button(`Connect ${item.name}`, async () => {
        const result = await api<{authorization_url: string}>(pathFor(item, "connect"), {});
        if (credential === sessionMarker) sessionStorage.setItem("cueporchCloudTeam", "1");
        location.assign(result.authorization_url);
      }));
    } else {
      const tip = document.createElement("p");
      tip.textContent = `Configure the workspace’s ${item.name} OAuth app to connect.`;
      card.append(tip);
    }
    return card;
  }));
}

function folderName(folder: Folder, lookup: Map<string, Folder>) {
  const names = [folder.name];
  const seen = new Set([folder.id]);
  let parent = folder.parent;
  while (parent && !seen.has(parent)) {
    const ancestor = lookup.get(parent);
    if (!ancestor) break;
    names.unshift(ancestor.name);
    seen.add(parent);
    parent = ancestor.parent;
  }
  return names.join(" / ");
}

async function openPicker(item: Integration) {
  const folders = await api<Folder[]>("/api/library/folders");
  const lookup = new Map(folders.map(folder => [folder.id, folder]));
  destination.replaceChildren(new Option("Library root", ""),
    ...folders.map(folder => new Option(folderName(folder, lookup), folder.id)));
  active = item;
  source = [];
  pickerHeading.textContent = `Import from ${active.name}`;
  remoteFolder.hidden = !active.browse_folders;
  picker.hidden = false;
  receipt.hidden = true;
  query = "";
  videoName.value = "";
  await loadPage(false);
  videoName.focus();
}

async function loadPage(append: boolean) {
  const args = new URLSearchParams({q: query, folder: source.at(-1)?.id || ""});
  if (append && cursor) args.set("cursor", cursor);
  const page = await api<{files: DriveFile[]; folders?: DriveFile[]; cursor: string | null}>(
    `${pathFor(active, "files")}?${args}`);
  cursor = page.cursor;
  more.hidden = !cursor;
  if (!append) videos.replaceChildren();
  remoteUp.hidden = source.length === 0;
  remotePath.textContent = [active.name, ...source.map(folder => folder.name)].join(" / ");
  for (const folder of page.folders || []) {
    const row = document.createElement("li");
    row.append(button(`Open folder ${folder.name}`, () => navigateSource([...source, folder])));
    videos.append(row);
  }
  for (const file of page.files) {
    const row = document.createElement("li");
    row.append(button(`Import ${file.name}`, async () => {
      receipt.hidden = true;
      status.textContent = `Importing ${file.name}…`;
      const saved = await api<{id: string; visibility: string}>(pathFor(active, "import"),
        {file: file.id, folder: destination.value || null});
      const link = document.createElement("a");
      const slug = encodeURIComponent(saved.id);
      link.href = credential === sessionMarker ? `/team/video/${slug}/details` : `/operator/videos/${slug}`;
      link.textContent = file.name;
      receipt.replaceChildren("Library video: ", link, ` (${saved.visibility})`);
      receipt.hidden = false;
      status.textContent = "Import ready.";
    }));
    videos.append(row);
  }
  noVideos.hidden = videos.childElementCount > 0;
}

async function navigateSource(path: DriveFile[]) {
  const previous = source;
  source = path;
  try {
    await loadPage(false);
  } catch (error) {
    source = previous;
    throw error;
  }
}

async function loadDirectory() {
  try {
    const result = await api<{integrations: Integration[]}>("/api/integrations");
    integrations = result.integrations;
    category.replaceChildren(new Option("All categories", ""),
      ...[...new Set(integrations.map(item => item.category))].map(name => new Option(name)));
    directory.hidden = false;
    access.hidden = true;
    if (credential !== sessionMarker) sessionStorage.setItem("cueporchOperator", credential);
    render();
    const completed = integrations.find(item => item.id === params.get("connection"));
    if (completed?.connected && params.get("result") === "connected") {
      status.textContent = `${completed.name} is connected.`;
    }
    if (completed && params.get("result") === "cancelled") {
      status.textContent = `${completed.name} connection was cancelled.`;
    }
    if (params.has("connection")) history.replaceState(null, "", location.pathname);
  } catch (error) {
    directory.hidden = true;
    access.hidden = false;
    message(error);
  }
}

access.onsubmit = event => {
  event.preventDefault();
  credential = key.value;
  void loadDirectory();
};
document.querySelector<HTMLButtonElement>("#integrationTeam")!.onclick = () => {
  credential = sessionMarker;
  void loadDirectory();
};
discover.onclick = () => { onlyConnected = false; render(); };
connected.onclick = () => { onlyConnected = true; render(); };
search.oninput = render;
category.onchange = render;
driveSearch.onsubmit = event => {
  event.preventDefault();
  query = videoName.value;
  void run(() => loadPage(false));
};
more.onclick = () => void run(() => loadPage(true));
remoteUp.onclick = () => void run(() => navigateSource(source.slice(0, -1)));
void loadDirectory();
