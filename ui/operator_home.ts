import {folderChoices, libraryUploader, readUploadPrivacy, type UploadFolder} from "./library_upload.js";

interface RecentWork {
  id: string;
  title: string;
  added_at: string | null;
  asset_type: string;
}

const identity = document.querySelector<HTMLFormElement>("#workspaceHomeIdentity")!;
const key = identity.querySelector<HTMLInputElement>("#key")!;
const open = identity.querySelector<HTMLButtonElement>("button")!;
const overview = document.querySelector<HTMLElement>("#workspaceHomeData")!;
const status = document.querySelector<HTMLElement>("#workspaceHomeStatus")!;
const recent = document.querySelector<HTMLUListElement>("#homeRecentWorks")!;
const project = document.querySelector<HTMLFormElement>("#homeCreateProject")!;
const startVideo = project.querySelector<HTMLSelectElement>("select")!;
const format = new Intl.NumberFormat();
let folders: UploadFolder[] = [];
let credential = "";

async function load(path: string, access: string, filters?: object) {
  const response = await fetch(path, {
    method: filters ? "POST" : "GET",
    headers: {"Authorization": "Bearer " + access, "Content-Type": "application/json"},
    body: filters ? JSON.stringify(filters) : undefined,
  });
  if (response.status === 403) throw new Error("Open the workspace with an Administrator session or operator credential.");
  if (!response.ok) throw new Error("Could not load the workspace overview.");
  return response;
}

function gallery(works: RecentWork[]) {
  works.sort((a, b) => (b.added_at ?? "").localeCompare(a.added_at ?? "") || a.title.localeCompare(b.title));
  recent.replaceChildren(...works.slice(0, 6).map(work => {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = "/operator/videos/" + encodeURIComponent(work.id);
    link.textContent = work.title;
    item.append(link);
    return item;
  }));
  document.querySelector<HTMLElement>("#homeNoRecent")!.hidden = Boolean(works.length);
  const videos = works.filter(work => work.asset_type === "video");
  startVideo.replaceChildren(...videos.map(work => new Option(work.title, work.id)));
  project.hidden = !videos.length;
  document.querySelector<HTMLElement>("#homeNoProjectVideo")!.hidden = Boolean(videos.length);
}

async function refresh() {
  const access = key.value;
  if (!access || open.disabled) return;
  open.disabled = true;
  overview.hidden = true;
  status.textContent = "Loading workspace…";
  try {
    const [works, nextFolders, report] = await Promise.all([
      load("/api/library", access).then(response => response.json()),
      load("/api/library/folders", access).then(response => response.json()),
      load("/api/analytics/date-report", access, {}).then(response => response.json()),
    ]);
    if (key.value !== access) return;
    gallery(works);
    folders = nextFolders;
    for (const [field, name] of [["homeViews", "views"], ["homeImpressions", "impressions"], ["homeUniqueViewers", "unique_viewers"]]) {
      document.getElementById(field)!.textContent = format.format(report.totals[name]);
    }
    credential = access;
    sessionStorage.setItem("cueporchOperator", credential);
    overview.hidden = false;
    status.textContent = "";
  } catch (error) {
    if (key.value === access) status.textContent = error instanceof Error ? error.message : "Could not load the workspace overview.";
  } finally {
    open.disabled = false;
  }
}

identity.onsubmit = event => { event.preventDefault(); void refresh(); };
key.addEventListener("input", () => { overview.hidden = true; status.textContent = ""; });
project.onsubmit = event => {
  event.preventDefault();
  if (startVideo.value) location.assign(`/operator/videos/${encodeURIComponent(startVideo.value)}/timeline`);
};
document.querySelector<HTMLButtonElement>("#homeCreateUpload")!.onclick =
  () => document.querySelector<HTMLButtonElement>("#uploadLibrary")!.click();

libraryUploader({
  available: () => !overview.hidden,
  folder: () => null,
  folderOptions: select => folderChoices(select, folders),
  credential: () => credential,
  defaults: folder => readUploadPrivacy(folder, path => load(path, credential)),
  reload: async () => gallery(await (await load("/api/library", credential)).json()),
});

if (key.value) void refresh();
