import {checked, sessionMarker, teamRequest} from "./team_api.js";

interface Announcement {
  slug: string;
  title: string;
  description: string;
  area: string;
  category: string;
  state: string;
  date: string;
  revision: string;
  feedback?: {up?: number; down?: number};
}

const access = document.querySelector<HTMLFormElement>("#updateAccess")!;
const key = access.querySelector<HTMLInputElement>("input")!;
const editor = document.querySelector<HTMLElement>("#announcementEditor")!;
const status = document.querySelector<HTMLElement>("#updateSaveStatus")!;
const list = document.querySelector<HTMLUListElement>("#editAnnouncements")!;
const add = document.querySelector<HTMLButtonElement>("#newAnnouncement")!;
const form = document.querySelector<HTMLFormElement>("#editAnnouncement")!;
const feedback = document.querySelector<HTMLElement>("#updateFeedbackCounts")!;
const signedInTeam = document.body.dataset.updatesTeam === "yes";
let current: Announcement | undefined;
let busy = false;

key.value = signedInTeam ? "" : sessionStorage.getItem("cueporchOperator") || "";
const credential = () => signedInTeam ? sessionMarker : key.value;
const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement;

async function checkedRequest<T>(path: string, method = "GET", body = {}): Promise<T> {
  return (await checked(await teamRequest(path, method, body, credential()))).json();
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

function show(entry?: Announcement) {
  current = entry;
  form.reset();
  field("slug").readOnly = Boolean(entry);
  if (entry) {
    for (const name of ["slug", "title", "description", "area", "category", "state", "date"] as const) {
      field(name).value = entry[name];
    }
  } else {
    field("date").value = new Date().toISOString().slice(0, 10);
  }
  feedback.hidden = !entry?.feedback;
  feedback.textContent = entry?.feedback
    ? `Thumbs up ${entry.feedback.up || 0} · Thumbs down ${entry.feedback.down || 0}` : "";
  form.hidden = false;
}

async function openList() {
  const {entries} = await checkedRequest<{entries: Announcement[]}>("updates");
  list.replaceChildren();
  for (const entry of entries) {
    const row = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = entry.title;
    button.onclick = () => {
      void perform(async () => show(await checkedRequest<Announcement>("updates/" + encodeURIComponent(entry.slug))));
    };
    const state = entry.state === "released" ? "Released" : entry.state === "up-next" ? "Up next" : "Draft";
    row.append(button, ` · ${state}`);
    list.append(row);
  }
  editor.hidden = false;
  access.hidden = true;
}

add.onclick = () => { if (!busy) show(); };
access.onsubmit = event => { event.preventDefault(); void perform(openList); };
form.onsubmit = event => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(form));
  void perform(async () => {
    const saved = current
      ? await checkedRequest<Announcement>("updates/" + encodeURIComponent(current.slug), "PUT",
                                           {...payload, revision: current.revision})
      : await checkedRequest<Announcement>("updates", "POST", payload);
    show(saved);
    await openList();
    status.textContent = "Announcement saved.";
  });
};
if (credential()) void perform(openList);
