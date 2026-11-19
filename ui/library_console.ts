interface Work {
  id: string;
  title: string;
  creator: string | null;
  added_at: string | null;
  modified_at: string | null;
  version_count: number;
  starred: boolean;
  watch_later: boolean;
  title_sync_error: boolean;
  folder: string | null;
  source_privacy: string | null;
  duration_seconds: number | null;
  quality: string;
  exceptions: unknown[];
  public_eligible: boolean;
  delivery: {
    status: string;
    error: string | null;
    route_kind: string | null;
  } | null;
}

interface FileInfo {
  role: string;
  sha256: string;
  format: string;
  bytes: number;
  language?: string;
}

interface WorkInfo {
  title: string;
  source_title: string;
  source_privacy: string | null;
  source: string;
  creator: string | null;
  description: string | null;
  duration_seconds: number | null;
  disposition: string;
  files: FileInfo[];
  watch: string | null;
  embed: string | null;
  preview: string | null;
}

type LibraryView = "Library" | "Starred" | "Watch later" | "Recently deleted";

interface Deletion {
  id: string;
  kind: "work" | "folder";
  title: string;
  actor: string;
  deleted_at: number;
  state: "withdrawing" | "deleted" | "restoring";
  error: string | null;
}

interface Folder {
  id: string;
  name: string;
  parent: string | null;
  color: string;
  starred: number;
}

const key = document.querySelector<HTMLInputElement>("#key")!;
const deleteFolder =
  document.querySelector<HTMLButtonElement>("#deleteFolder")!;
const deleteWorks = document.querySelector<HTMLButtonElement>("#deleteWorks")!;
const workBrowse = document.querySelector<HTMLElement>("#workBrowse")!;
const trashBrowse = document.querySelector<HTMLElement>("#trashBrowse")!;
const trashType = document.querySelector<HTMLSelectElement>("#trashType")!;
const trashActor = document.querySelector<HTMLInputElement>("#trashActor")!;
const trashItems =
  document.querySelector<HTMLTableSectionElement>("#trashItems")!;
const trashEmpty = document.querySelector<HTMLElement>("#trashEmpty")!;
const trashTable = document.querySelector<HTMLTableElement>("#trashTable")!;
const result = document.querySelector<HTMLElement>("#result")!;
const creator = document.querySelector<HTMLInputElement>("#creator")!;
const creators = document.querySelector<HTMLDataListElement>("#creators")!;
const starFolder = document.querySelector<HTMLButtonElement>("#starFolder")!;
const library = document.querySelector<HTMLElement>("#library")!;
const tree = document.querySelector<HTMLElement>("#folderTree")!;
const heading = document.querySelector<HTMLElement>("#folderTitle")!;
const items = document.querySelector<HTMLTableSectionElement>("#items")!;
const empty = document.querySelector<HTMLElement>("#empty")!;
const table = document.querySelector<HTMLTableElement>("#workTable")!;
const search = document.querySelector<HTMLInputElement>("#search")!;
const privacy = document.querySelector<HTMLSelectElement>("#privacy")!;
const sort = document.querySelector<HTMLSelectElement>("#sort")!;
const view = document.querySelector<HTMLSelectElement>("#view")!;
const editFolder = document.querySelector<HTMLButtonElement>("#editFolder")!;
const destination = document.querySelector<HTMLSelectElement>("#destination")!;
const move = document.querySelector<HTMLButtonElement>("#move")!;
const editor = document.querySelector<HTMLDialogElement>("#folderEditor")!;
const form = document.querySelector<HTMLFormElement>("#folderForm")!;
const folderName = document.querySelector<HTMLInputElement>("#folderName")!;
const folderColor = document.querySelector<HTMLInputElement>("#folderColor")!;
const folderParent =
  document.querySelector<HTMLSelectElement>("#folderParent")!;
const folderError = document.querySelector<HTMLElement>("#folderError")!;
const saveFolder = document.querySelector<HTMLButtonElement>("#saveFolder")!;

key.value = sessionStorage.getItem("cueporchOperator") || "";
let works: Work[] = [];
let folders: Folder[] = [];
let current: string | null = null;
let editing: Folder | undefined;
let section: LibraryView = "Library";
let deletions: Deletion[] = [];
let trashTimer: number | undefined;
const selected = new Set<string>();

function request(path: string, method = "GET", body?: object) {
  return fetch(path, {
    method,
    headers: {
      Authorization: "Bearer " + key.value,
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
}

async function completed(response: Response) {
  if (!response.ok) {
    const page = new DOMParser().parseFromString(
      await response.text(),
      "text/html",
    );
    throw new Error(
      page.querySelector("p")?.textContent ||
        `Request failed (${response.status})`,
    );
  }
  return response;
}

function action(button: HTMLButtonElement, run: () => Promise<void>) {
  button.onclick = async () => {
    button.disabled = true;
    try {
      await run();
    } catch (error) {
      result.textContent = String(error);
    } finally {
      button.disabled = false;
      updateMove();
    }
  };
}

function folderOptions(select: HTMLSelectElement) {
  select.replaceChildren(new Option("Library", ""));
  function append(parent: string | null, prefix: string) {
    for (const folder of folders.filter((entry) => entry.parent === parent)) {
      if (select === folderParent && folder.id === editing?.id) continue;
      const label = prefix + folder.name;
      select.append(new Option(label, folder.id));
      append(folder.id, label + " / ");
    }
  }
  append(null, "");
}

function updateMove() {
  destination.disabled = !selected.size;
  deleteWorks.disabled = move.disabled = !selected.size;
}

function renderTree() {
  function branch(parent: string | null) {
    const list = document.createElement("ul");
    for (const folder of folders.filter((entry) => entry.parent === parent)) {
      const li = document.createElement("li");
      li.append(folderButton(folder.name, folder.id, folder.color));
      const children = branch(folder.id);
      if (children.childElementCount) li.append(children);
      list.append(li);
    }
    return list;
  }
  const starredFolders = document.createElement("ul");
  if (section === "Starred") {
    for (const folder of folders.filter((entry) => entry.starred)) {
      const item = document.createElement("li");
      item.append(folderButton(folder.name, folder.id, folder.color));
      starredFolders.append(item);
    }
  }
  tree.replaceChildren(
    folderButton("Library", null),
    branch(null),
    sectionButton("Starred"),
    starredFolders,
    sectionButton("Watch later"),
    sectionButton("Recently deleted"),
  );
  workBrowse.hidden = section === "Recently deleted";
  trashBrowse.hidden = section !== "Recently deleted";
  const folder = folders.find((entry) => entry.id === current);
  heading.textContent =
    section === "Library" ? folder?.name || "Library" : section;
  deleteFolder.hidden = editFolder.hidden = starFolder.hidden = !folder;
  starFolder.textContent = folder?.starred ? "Unstar folder" : "Star folder";
  if (folder) {
    action(starFolder, () => setStar("folder", folder.id, !folder.starred));
    action(deleteFolder, () => removeItems("folder", [folder.id]));
  }
  folderOptions(destination);
  destination.value = current || "";
}

async function navigate(next: LibraryView, folder: string | null = null) {
  const leavingTrash = section === "Recently deleted";
  section = next;
  current = folder;
  selected.clear();
  renderTree();
  renderWorks();
  tree.querySelector<HTMLButtonElement>('[aria-current="true"]')?.focus();
  if (section === "Recently deleted") await loadTrash();
  else if (leavingTrash) await load();
}

function sectionButton(label: LibraryView) {
  const button = document.createElement("button");
  button.textContent = label;
  if (section === label) button.setAttribute("aria-current", "true");
  action(button, () => navigate(label));
  return button;
}

function folderButton(label: string, id: string | null, color?: string) {
  const button = document.createElement("button");
  button.textContent = label;
  if (section === "Library" && id === current)
    button.setAttribute("aria-current", "true");
  if (color) {
    const dot = document.createElement("span");
    dot.className = "folder-dot";
    dot.style.backgroundColor = color;
    dot.setAttribute("aria-hidden", "true");
    button.prepend(dot);
  }
  action(button, () => navigate("Library", id));
  return button;
}

async function setStar(kind: "folder" | "work", id: string, starred: boolean) {
  await completed(
    await request(
      "/api/library/star/" + kind + "/" + encodeURIComponent(id),
      "PUT",
      { starred },
    ),
  );
  await load();
  result.textContent = starred ? "Added to Starred." : "Removed from Starred.";
}

function numericValue(work: Work, order: string) {
  if (order === "long" || order === "short") return work.duration_seconds;
  const field = order.startsWith("added") ? work.added_at : work.modified_at;
  const date = field === null ? NaN : Date.parse(field);
  return Number.isFinite(date) ? date : null;
}

function renderWorks() {
  const query = search.value.trim().toLocaleLowerCase();
  const author = creator.value.trim().toLocaleLowerCase();
  const visible = works.filter(
    (work) =>
      (section === "Starred"
        ? work.starred
        : section === "Watch later"
          ? work.watch_later
          : work.folder === current) &&
      (!author || work.creator?.toLocaleLowerCase().includes(author)) &&
      work.title.toLocaleLowerCase().includes(query) &&
      (!privacy.value || (work.source_privacy || "Unknown") === privacy.value),
  );
  visible.sort((a, b) => {
    const title = a.title.localeCompare(b.title);
    const stable = title || a.id.localeCompare(b.id);
    const order = sort.value;
    if (order === "title") return stable;
    if (order === "title-desc") return -title || a.id.localeCompare(b.id);
    const first = numericValue(a, order);
    const second = numericValue(b, order);
    if (first === null) return second === null ? stable : 1;
    if (second === null) return -1;
    const direction = order === "long" || order.endsWith("-desc") ? -1 : 1;
    return (first - second) * direction || stable;
  });
  items.replaceChildren();
  table.hidden = !visible.length;
  table.dataset.view = view.value;
  empty.hidden = !!visible.length;
  for (const work of visible) {
    const row = items.insertRow();
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selected.has(work.id);
    checkbox.setAttribute("aria-label", "Select " + work.title);
    checkbox.onchange = () => {
      if (checkbox.checked) selected.add(work.id);
      else selected.delete(work.id);
      updateMove();
    };
    row.insertCell().append(checkbox);
    const title = row.insertCell();
    const name = document.createElement("span");
    name.className = "work-title";
    name.textContent = work.title;
    title.append(name);
    const info = document.createElement("small");
    info.className = "work-info";
    const seconds = work.duration_seconds;
    const duration =
      seconds === null
        ? ""
        : Math.floor(seconds / 60) +
          ":" +
          String(Math.floor(seconds % 60)).padStart(2, "0");
    const formattedDate =
      work.added_at && Number.isFinite(Date.parse(work.added_at))
        ? new Date(work.added_at).toLocaleDateString()
        : "";
    info.textContent = [
      work.creator,
      duration,
      work.version_count > 1 ? work.version_count + " retained versions" : "",
      formattedDate,
    ]
      .filter(Boolean)
      .join(" · ");
    title.append(info);
    row.insertCell().textContent = work.source_privacy || "Unknown";
    row.insertCell().textContent = [
      work.quality,
      ...work.exceptions.map((item) =>
        typeof item === "string" ? item : JSON.stringify(item),
      ),
    ].join(" · ");
    row.insertCell().textContent =
      [work.delivery?.status, work.delivery?.error]
        .filter(Boolean)
        .join(" · ") || "Preserved locally";
    const actions = row.insertCell();
    actions.append(cardMenu(work));
    if (work.title_sync_error) {
      const sync = document.createElement("p");
      sync.textContent = "Syncing title with the original video host.";
      actions.append(sync);
    }
    const star = document.createElement("button");
    star.textContent = work.starred ? "Unstar work" : "Star work";
    star.setAttribute(
      "aria-label",
      (work.starred ? "Unstar " : "Star ") + work.title,
    );
    action(star, () => setStar("work", work.id, !work.starred));
    actions.append(star);
    const remove = document.createElement("button");
    remove.textContent = "Delete work";
    remove.setAttribute("aria-label", "Delete " + work.title);
    action(remove, () => removeItems("work", [work.id]));
    actions.append(remove);
    for (const pub of work.public_eligible ? [false, true] : [false]) {
      const button = document.createElement("button");
      button.textContent = pub ? "Publish publicly" : "Deliver privately";
      action(button, async () => {
        await completed(
          await request(
            "/api/delivery/" + encodeURIComponent(work.id),
            "POST",
            { public: pub },
          ),
        );
        result.textContent = "Queued " + work.title;
      });
      actions.append(button);
    }
    if (work.delivery?.route_kind) {
      const link = document.createElement("a");
      link.href = "/watch/" + work.delivery.route_kind + "/" + work.id;
      link.textContent = "Watch";
      actions.append(link);
    }
  }
  updateMove();
}

const workDialog = document.querySelector<HTMLDialogElement>("#workDialog")!;
const workDialogTitle =
  document.querySelector<HTMLElement>("#workDialogTitle")!;
const workDialogBody = document.querySelector<HTMLElement>("#workDialogBody")!;
const workDialogStatus =
  document.querySelector<HTMLElement>("#workDialogStatus")!;
const titleEditor = document.querySelector<HTMLDialogElement>("#titleEditor")!;
const titleForm = document.querySelector<HTMLFormElement>("#titleForm")!;
const workTitle = document.querySelector<HTMLInputElement>("#workTitle")!;
const titleError = document.querySelector<HTMLElement>("#titleError")!;
const saveTitle = document.querySelector<HTMLButtonElement>("#saveTitle")!;

function workEndpoint(work: Work) {
  return "/api/library/work/" + encodeURIComponent(work.id);
}
async function getWorkInfo(work: Work): Promise<WorkInfo> {
  return (await completed(await request(workEndpoint(work)))).json();
}

function cardMenu(work: Work) {
  const menu = document.createElement("details");
  menu.className = "work-menu";
  const summary = document.createElement("summary");
  summary.textContent = "Actions";
  summary.setAttribute("aria-label", "Actions for " + work.title);
  menu.append(summary);
  const entries: [string, () => void | Promise<void>][] = [
    ["Share", () => shareDialog(work)],
    ["Copy link", () => shareDialog(work, "watch")],
    ["Copy embed code", () => shareDialog(work, "embed")],
    [
      work.watch_later ? "Remove from Watch later" : "Watch later",
      () => saveForLater(work),
    ],
    ["Rename", () => editTitle(work, false)],
    ["Create a copy", () => editTitle(work, true)],
    ["Video info", () => infoDialog(work, false)],
    ["Download", () => infoDialog(work, true)],
  ];
  for (const [label, run] of entries) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    action(button, async () => {
      menu.open = false;
      summary.focus();
      await run();
    });
    menu.append(button);
  }
  return menu;
}

function openWorkDialog(title: string) {
  workDialogTitle.textContent = title;
  workDialogBody.replaceChildren();
  workDialogStatus.textContent = "";
  workDialog.showModal();
}

async function copyText(field: HTMLInputElement | HTMLTextAreaElement) {
  try {
    await navigator.clipboard.writeText(field.value);
    workDialogStatus.textContent = "Copied to clipboard.";
  } catch {
    field.focus();
    field.select();
    workDialogStatus.textContent = "Select and copy the highlighted text.";
  }
}

async function shareDialog(work: Work, copy?: "watch" | "embed") {
  const info = await getWorkInfo(work);
  openWorkDialog("Share " + info.title);
  if (!info.watch || !info.embed) {
    const link = document.createElement("a");
    link.href = "/operator/exhibitions";
    link.textContent = "Manage private screening grants";
    workDialogBody.append("This work has no public link. ", link);
    return;
  }
  const url = document.body.dataset.publicUrl || location.origin;
  const watch = document.createElement("input");
  const embed = document.createElement("textarea");
  watch.value = new URL(info.watch, url).href;
  const iframe = document.createElement("iframe");
  iframe.src = new URL(info.embed, url).href;
  iframe.title = info.title;
  iframe.width = "640";
  iframe.height = "360";
  iframe.allowFullscreen = true;
  iframe.setAttribute("allow", "autoplay; fullscreen; picture-in-picture");
  embed.value = iframe.outerHTML;
  for (const [label, field] of [
    ["Watch link", watch],
    ["Embed code", embed],
  ] as const) {
    const labelElement = document.createElement("label");
    labelElement.textContent = label;
    field.readOnly = true;
    field.setAttribute("aria-label", label);
    labelElement.append(field);
    const button = document.createElement("button");
    button.textContent = "Copy " + label.toLowerCase();
    action(button, () => copyText(field));
    workDialogBody.append(labelElement, button);
  }
  if (copy) await copyText(copy === "watch" ? watch : embed);
}

async function mediaTicket(work: Work, sha256: string) {
  const response = await completed(
    await request(workEndpoint(work) + "/ticket", "POST", { sha256 }),
  );
  const urls: { download: string; preview: string } = await response.json();
  return urls;
}

async function infoDialog(work: Work, download: boolean) {
  const info = await getWorkInfo(work);
  openWorkDialog(download ? "Download " + info.title : "Video info");
  if (!download) {
    const facts = document.createElement("dl");
    const fields = {
      Title: info.title,
      "Imported title":
        info.source_title === info.title ? "" : info.source_title,
      Description: info.description,
      Creator: info.creator,
      Source: info.source,
      "Source privacy": info.source_privacy,
      Duration:
        info.duration_seconds === null
          ? ""
          : info.duration_seconds + " seconds",
      Recovery: info.disposition,
    };
    for (const [label, value] of Object.entries(fields)) {
      if (!value) continue;
      const key = document.createElement("dt");
      key.textContent = label;
      const field = document.createElement("dd");
      field.textContent = value;
      facts.append(key, field);
    }
    workDialogBody.append(facts);
    if (info.preview) {
      const hash = info.preview;
      const preview = document.createElement("button");
      preview.textContent = "Load video preview";
      action(preview, async () => {
        const video = document.createElement("video");
        video.src = (await mediaTicket(work, hash)).preview;
        video.controls = true;
        video.preload = "metadata";
        preview.replaceWith(video);
      });
      workDialogBody.append(preview);
    }
  }
  const list = document.createElement("ul");
  for (const file of info.files) {
    const playable = file.role === "original" || file.role === "playback";
    if (download && !playable) continue;
    const item = document.createElement("li");
    item.textContent = [
      file.role,
      file.format.toUpperCase(),
      file.language,
      file.bytes.toLocaleString() + " bytes",
    ]
      .filter(Boolean)
      .join(" · ");
    if (playable) {
      const button = document.createElement("button");
      button.textContent = "Download " + file.role;
      action(button, async () => {
        const link = document.createElement("a");
        link.href = (await mediaTicket(work, file.sha256)).download;
        link.download = "";
        link.referrerPolicy = "no-referrer";
        link.click();
      });
      item.append(" ", button);
    }
    list.append(item);
  }
  if (list.childElementCount) workDialogBody.append(list);
  else workDialogBody.append("No video file is available.");
}

workDialog.onclose = () => workDialogBody.replaceChildren();

async function saveForLater(work: Work) {
  await completed(
    await request(workEndpoint(work) + "/watch-later", "PUT", {
      saved: !work.watch_later,
    }),
  );
  await load();
  result.textContent = work.watch_later
    ? "Removed from Watch later."
    : "Added to Watch later.";
}

function editTitle(work: Work, copy: boolean) {
  document.querySelector("#titleEditorHeading")!.textContent = copy
    ? "Create a copy"
    : "Rename work";
  workTitle.value = copy ? work.title.slice(0, 121) + " (copy)" : work.title;
  titleError.textContent = "";
  saveTitle.textContent = copy ? "Create copy" : "Save title";
  titleForm.onsubmit = async (event) => {
    event.preventDefault();
    saveTitle.disabled = true;
    try {
      await completed(
        await request(
          workEndpoint(work) + (copy ? "/copy" : "/title"),
          copy ? "POST" : "PUT",
          { title: workTitle.value },
        ),
      );
      titleEditor.close();
      if (copy) {
        section = "Library";
        current = null;
        search.value = "";
        creator.value = "";
        privacy.value = "";
      }
      await load();
      result.textContent = copy
        ? "Private copy added to Library."
        : "Title saved.";
    } catch (error) {
      (titleEditor.open ? titleError : result).textContent = String(error);
    } finally {
      saveTitle.disabled = false;
    }
  };
  titleEditor.showModal();
  workTitle.select();
}
document.querySelector<HTMLButtonElement>("#cancelTitle")!.onclick = () =>
  titleEditor.close();

const trashPath = "/api/library/recently-deleted";

function renderTrash() {
  const actor = trashActor.value.trim().toLocaleLowerCase();
  const visible = deletions.filter(
    (item) =>
      (!trashType.value || item.kind === trashType.value) &&
      item.actor.toLocaleLowerCase().includes(actor),
  );
  trashItems.replaceChildren();
  trashEmpty.hidden = !!visible.length;
  trashTable.hidden = !visible.length;
  for (const item of visible) {
    const row = trashItems.insertRow();
    row.insertCell().textContent = item.title;
    row.insertCell().textContent = item.kind === "work" ? "Work" : "Folder";
    row.insertCell().textContent = item.actor;
    row.insertCell().textContent = new Date(
      item.deleted_at * 1000,
    ).toLocaleString();
    row.insertCell().textContent = item.error
      ? "Waiting for the original video host. Retrying."
      : {
          withdrawing: "Withdrawing",
          restoring: "Restoring",
          deleted: "Ready to restore",
        }[item.state];
    const actions = row.insertCell();
    if (item.state === "deleted") {
      const button = document.createElement("button");
      button.textContent = "Restore";
      button.setAttribute("aria-label", "Restore " + item.title);
      action(button, async () => {
        await completed(
          await request(trashPath + "/" + item.id + "/restore", "POST"),
        );
        result.textContent = "Restoration queued.";
        await loadTrash();
      });
      actions.append(button);
    }
  }
}

async function loadTrash() {
  window.clearTimeout(trashTimer);
  const previous = deletions;
  deletions = await (await completed(await request(trashPath))).json();
  if (section !== "Recently deleted") return;
  const currentIds = new Set(deletions.map((item) => item.id));
  if (previous.some((item) => !currentIds.has(item.id))) await load(false);
  renderTrash();
  if (deletions.some((item) => item.state !== "deleted")) {
    trashTimer = window.setTimeout(() => {
      if (section === "Recently deleted")
        void loadTrash().catch((error) => {
          result.textContent = String(error);
        });
    }, 1200);
  }
}

async function removeItems(kind: "work" | "folder", ids: string[]) {
  await completed(await request(trashPath, "POST", { kind, items: ids }));
  result.textContent =
    "Items moved to Recently deleted. Hosted withdrawal may still be in progress.";
  await load();
}

action(deleteWorks, () => removeItems("work", [...selected]));
action(document.querySelector<HTMLButtonElement>("#refreshTrash")!, loadTrash);
trashType.onchange = trashActor.oninput = renderTrash;

async function load(refreshTrash = true) {
  const [libraryResponse, folderResponse] = await Promise.all([
    request("/api/library").then(completed),
    request("/api/library/folders").then(completed),
  ]);
  works = await libraryResponse.json();
  folders = await folderResponse.json();
  const names = [
    ...new Set(works.flatMap((work) => (work.creator ? [work.creator] : []))),
  ].sort();
  creators.replaceChildren(...names.map((name) => new Option(name, name)));
  if (!folders.some((entry) => entry.id === current)) current = null;
  selected.clear();
  const previous = privacy.value;
  const modes = [
    ...new Set(works.map((work) => work.source_privacy || "Unknown")),
  ].sort();
  privacy.replaceChildren(
    new Option("All", ""),
    ...modes.map((mode) => new Option(mode, mode)),
  );
  privacy.value = modes.includes(previous) ? previous : "";
  library.hidden = false;
  renderTree();
  renderWorks();
  if (section === "Recently deleted" && refreshTrash) await loadTrash();
}

function openEditor(folder?: Folder) {
  editing = folder;
  document.querySelector("#folderEditorTitle")!.textContent = folder
    ? "Edit folder"
    : "New folder";
  folderOptions(folderParent);
  folderParent.value = (folder ? folder.parent : current) || "";
  folderName.value = folder?.name || "";
  folderColor.value = folder?.color || "#164e5a";
  folderError.textContent = "";
  editor.showModal();
  folderName.focus();
}

form.onsubmit = async (event) => {
  event.preventDefault();
  saveFolder.disabled = true;
  try {
    const prefix = "/api/library/folders";
    await completed(
      await request(
        editing ? prefix + "/" + editing.id : prefix,
        editing ? "PUT" : "POST",
        {
          name: folderName.value,
          color: folderColor.value,
          parent: folderParent.value || null,
        },
      ),
    );
    editor.close();
    await load();
    result.textContent = "Folder saved.";
  } catch (error) {
    if (editor.open) folderError.textContent = String(error);
    else result.textContent = String(error);
  } finally {
    saveFolder.disabled = false;
  }
};

action(move, async () => {
  const count = selected.size;
  await completed(
    await request("/api/library/placement", "PUT", {
      works: [...selected],
      folder: destination.value || null,
    }),
  );
  await load();
  result.textContent =
    count === 1 ? "Moved one work." : `Moved ${count} works.`;
});

document.querySelector<HTMLButtonElement>("#newFolder")!.onclick = () =>
  openEditor();
editFolder.onclick = () =>
  openEditor(folders.find((folder) => folder.id === current));
document.querySelector<HTMLButtonElement>("#cancelFolder")!.onclick = () =>
  editor.close();
search.oninput =
  creator.oninput =
  privacy.onchange =
  sort.onchange =
  view.onchange =
    () => {
      selected.clear();
      renderWorks();
    };

action(document.querySelector<HTMLButtonElement>("#load")!, async () => {
  await load();
  result.textContent = "Library loaded.";
});

action(
  document.querySelector<HTMLButtonElement>("#publishImports")!,
  async () => {
    const response = await completed(
      await request("/api/migration/publish-eligible", "POST"),
    );
    result.textContent =
      "Queued eligible imports: " + (await response.json()).queued;
  },
);

action(document.querySelector<HTMLButtonElement>("#exportMap")!, async () => {
  const response = await completed(await request("/api/embed-map.csv"));
  const link = document.createElement("a");
  link.href = URL.createObjectURL(await response.blob());
  link.download = "cueporch-embed-map.csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});
