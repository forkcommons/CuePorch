type PickKey = [string, string];
interface Video {kind: string; slug: string; title: string; categories: string[];}
interface Picks {revision: string; items: PickKey[]; videos: Video[];}

const access = document.querySelector<HTMLFormElement>("#picksAccess")!;
const credential = document.querySelector<HTMLInputElement>("#key")!;
const editor = document.querySelector<HTMLElement>("#picksEditor")!;
const status = document.querySelector<HTMLElement>("#picksStatus")!;
const save = document.querySelector<HTMLButtonElement>("#savePicks")!;
const selected = document.querySelector<HTMLOListElement>("#picksList")!;
const available = document.querySelector<HTMLUListElement>("#pickOptions")!;
const search = document.querySelector<HTMLInputElement>("#pickSearch")!;
let saved: Picks | undefined;
let items: PickKey[] = [];
let busy = false;
const id = (key: PickKey) => JSON.stringify(key);

function action(label: string, title: string, run: () => void) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.setAttribute("aria-label", `${label} ${title}`);
  button.onclick = run;
  return button;
}

function move(index: number, direction: -1 | 1, label: string) {
  const destination = index + direction;
  [items[index], items[destination]] = [items[destination], items[index]];
  render();
  const row = selected.children[destination];
  (Array.from(row.querySelectorAll("button")).find(button => !button.disabled && button.textContent === label)
    ?? row.querySelector<HTMLButtonElement>("button:not(:disabled)"))?.focus();
}

function row(title: string, ...controls: HTMLButtonElement[]) {
  const item = document.createElement("li");
  const name = document.createElement("span");
  name.textContent = title;
  item.append(name, ...controls);
  return item;
}

function render() {
  if (!saved) return;
  const videos = new Map(saved.videos.map(video => [id([video.kind, video.slug]), video]));
  selected.replaceChildren(...items.map((key, index) => {
    const title = videos.get(id(key))?.title ?? "Unavailable video";
    const up = action("Move up", title, () => move(index, -1, "Move up"));
    const down = action("Move down", title, () => move(index, 1, "Move down"));
    up.disabled = index === 0 || busy;
    down.disabled = index === items.length - 1 || busy;
    const remove = action("Remove", title, () => {
      items.splice(index, 1);
      render();
      (selected.children[Math.min(index, items.length - 1)]?.querySelector<HTMLButtonElement>("button:not(:disabled)") ?? search).focus();
    });
    remove.disabled = busy;
    return row(title, up, down, remove);
  }));
  const taken = new Set(items.map(id));
  const query = search.value.trim().toLowerCase();
  const options = saved.videos.filter(video =>
    !taken.has(id([video.kind, video.slug])) && [video.title, ...video.categories].some(term => term.toLowerCase().includes(query)),
  );
  available.replaceChildren(...options.slice(0, 100).map(video => {
    const add = action("Add", video.title, () => {
      items.push([video.kind, video.slug]);
      render();
      selected.lastElementChild?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    });
    add.disabled = busy;
    return row(video.title, add);
  }));
  if (options.length > 100) available.append(row("Search to narrow the results."));
  save.disabled = busy || idList(items) === idList(saved.items);
}

const idList = (values: PickKey[]) => JSON.stringify(values);

async function load(method: "GET" | "PUT") {
  if (busy || (method === "PUT" && !saved)) return;
  busy = true;
  render();
  status.textContent = method === "PUT" ? "Saving Staff Picks…" : "Loading Staff Picks…";
  try {
    const response = await fetch("/api/watch/staff-picks", {
      method, cache: "no-store",
      headers: {"Authorization": `Bearer ${credential.value}`, "Content-Type": "application/json"},
      ...(method === "PUT" ? {body: JSON.stringify({revision: saved?.revision, items})} : {}),
    });
    if (response.status === 409) throw new Error("Staff Picks changed. Reload before saving.");
    if (response.status === 422) throw new Error("A selection is no longer public. Reload Staff Picks.");
    if (!response.ok) throw new Error(response.status === 403 ? "Check your operator credential." : "Could not load Staff Picks.");
    const snapshot: Picks = await response.json();
    saved = snapshot;
    items = [...snapshot.items];
    sessionStorage.setItem("cueporchOperator", credential.value);
    editor.hidden = false;
    status.textContent = method === "PUT" ? "Staff Picks saved." : "";
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : "Could not load Staff Picks.";
  } finally {
    busy = false;
    render();
  }
}

credential.value = sessionStorage.getItem("cueporchOperator") || "";
access.onsubmit = event => { event.preventDefault(); void load("GET"); };
save.onclick = () => void load("PUT");
search.oninput = render;
if (credential.value) void load("GET");
