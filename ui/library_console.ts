interface Work {
  id: string;
  title: string;
  creator: string | null;
  added_at: string | null;
  modified_at: string | null;
  version_count: number;
  starred: boolean;
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
let showStarred = false;
let showTrash = false;
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
  const starredButton = document.createElement("button");
  starredButton.textContent = "Starred";
  if (showStarred) starredButton.setAttribute("aria-current", "true");
  starredButton.onclick = () => {
    const leavingTrash = showTrash;
    showTrash = false;
    showStarred = true;
    current = null;
    selected.clear();
    renderTree();
    renderWorks();
    if (leavingTrash)
      void load().catch((error) => {
        result.textContent = String(error);
      });
    tree.querySelector<HTMLButtonElement>('[aria-current="true"]')?.focus();
  };
  const starredFolders = document.createElement("ul");
  if (showStarred) {
    for (const folder of folders.filter((entry) => entry.starred)) {
      const item = document.createElement("li");
      item.append(folderButton(folder.name, folder.id, folder.color));
      starredFolders.append(item);
    }
  }
  const trashButton = document.createElement("button");
  trashButton.textContent = "Recently deleted";
  if (showTrash) trashButton.setAttribute("aria-current", "true");
  action(trashButton, async () => {
    showTrash = true;
    showStarred = false;
    current = null;
    selected.clear();
    renderTree();
    tree.querySelector<HTMLButtonElement>('[aria-current="true"]')?.focus();
    await loadTrash();
  });
  tree.replaceChildren(
    folderButton("Library", null),
    branch(null),
    starredButton,
    starredFolders,
    trashButton,
  );
  workBrowse.hidden = showTrash;
  trashBrowse.hidden = !showTrash;
  const folder = folders.find((entry) => entry.id === current);
  heading.textContent = showTrash
    ? "Recently deleted"
    : showStarred
      ? "Starred"
      : folder?.name || "Library";
  deleteFolder.hidden = editFolder.hidden = starFolder.hidden = !folder;
  starFolder.textContent = folder?.starred ? "Unstar folder" : "Star folder";
  if (folder) {
    action(starFolder, () => setStar("folder", folder.id, !folder.starred));
    action(deleteFolder, () => removeItems("folder", [folder.id]));
  }
  folderOptions(destination);
  destination.value = current || "";
}

function folderButton(label: string, id: string | null, color?: string) {
  const button = document.createElement("button");
  button.textContent = label;
  if (!showTrash && !showStarred && id === current)
    button.setAttribute("aria-current", "true");
  if (color) {
    const dot = document.createElement("span");
    dot.className = "folder-dot";
    dot.style.backgroundColor = color;
    dot.setAttribute("aria-hidden", "true");
    button.prepend(dot);
  }
  button.onclick = () => {
    const leavingTrash = showTrash;
    showTrash = false;
    showStarred = false;
    current = id;
    selected.clear();
    renderTree();
    renderWorks();
    if (leavingTrash)
      void load().catch((error) => {
        result.textContent = String(error);
      });
    const buttons = Array.from(tree.querySelectorAll("button"));
    buttons.find((entry) => entry.hasAttribute("aria-current"))?.focus();
  };
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
      (showStarred ? work.starred : work.folder === current) &&
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
  if (!showTrash) return;
  const currentIds = new Set(deletions.map((item) => item.id));
  if (previous.some((item) => !currentIds.has(item.id))) await load(false);
  renderTrash();
  if (deletions.some((item) => item.state !== "deleted")) {
    trashTimer = window.setTimeout(() => {
      if (showTrash)
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
  if (showTrash && refreshTrash) await loadTrash();
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
