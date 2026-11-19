import { renderCommentText } from "./review_comment_text.js";
import { libraryUploader } from "./library_upload.js";
import { openPrivacy } from "./library_privacy.js";

type AssetType = "video" | "audio" | "image" | "document";

interface Work {
  id: string;
  asset_type: AssetType;
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
  editable_privacy: boolean;
  visibility: string | null;
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
  name?: string;
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
  asset_type: AssetType;
  watch: string | null;
  embed: string | null;
  preview: string | null;
  thumbnail: boolean;
}

interface VideoVersion {
  key: string;
  current: boolean;
  saved_at: string | null;
  duration_seconds: number | null;
  video_files: FileInfo[];
}

interface ReviewLink {
  id: string;
  name: string;
  path: string;
  expires: number;
  enabled: boolean;
  password: boolean;
  allow_download: boolean;
  allow_versions: boolean;
  allow_comment: boolean;
  allow_status: boolean;
  allow_resolved: boolean;
  dark: boolean;
  show_other_videos: boolean;
  logo: string | null;
}

type ReviewDefaults = Omit<ReviewLink, "id" | "name" | "path" | "expires" | "enabled"> & {
  lifetime_seconds: number;
  auto_create: boolean;
};

interface ReviewComment {
  id: string;
  parent: string | null;
  author: string;
  body: string;
  seconds: number;
  resolved: boolean;
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
const assetType = document.querySelector<HTMLSelectElement>("#assetType")!;
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
  document.querySelector<HTMLElement>("#uploadHint")!.hidden = section !== "Library";
  document.querySelector<HTMLButtonElement>("#uploadLibrary")!.hidden = section !== "Library";
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

function libraryDate(value: string | null) {
  return value && Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleDateString()
    : "";
}

function durationText(seconds: number | null) {
  return seconds === null
    ? ""
    : Math.floor(seconds / 60) +
      ":" +
      String(Math.floor(seconds % 60)).padStart(2, "0");
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
      (!assetType.value || (assetType.value === "video") === (work.asset_type === "video")) &&
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
    row.dataset.workId = work.id;
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
    info.textContent = [
      work.asset_type === "video" ? "" : work.asset_type,
      work.creator,
      durationText(work.duration_seconds),
      work.version_count > 1 ? work.version_count + " retained versions" : "",
      libraryDate(work.added_at),
    ]
      .filter(Boolean)
      .join(" · ");
    title.append(info);
    const sharing = row.insertCell();
    if (work.editable_privacy) {
      const button = document.createElement("button");
      const visibility = work.visibility || "private";
      button.textContent = visibility === "embed_only"
        ? "Embed only" : visibility[0].toUpperCase() + visibility.slice(1);
      button.setAttribute("aria-label", "Change privacy for " + work.title);
      action(button, () => editPrivacy(work));
      sharing.append(button);
    } else sharing.textContent = work.source_privacy || "Unknown";
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
    const destinations = work.asset_type !== "video" ? [] : work.public_eligible ? [false, true] : [false];
    for (const pub of destinations) {
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
  type Entry = [string, () => void | Promise<void>];
  const video: Entry[] = work.asset_type === "video" ? [
    ["Share", () => shareDialog(work)],
    ["Copy link", () => shareDialog(work, "watch")],
    ["Review link", () => reviewLinksDialog(work)],
    ["Copy embed code", () => shareDialog(work, "embed")],
    [
      work.watch_later ? "Remove from Watch later" : "Watch later",
      () => saveForLater(work),
    ],
    ["Change thumbnail", () => thumbnailDialog(work)],
    ["Replace", () => versionsDialog(work)],
  ] : [];
  const entries: Entry[] = [
    ...video,
    ["Rename", () => editTitle(work, false)],
    ["Create a copy", () => editTitle(work, true)],
    [work.asset_type === "video" ? "Video info" : "File info", () => infoDialog(work, false)],
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
  if (!workDialog.open) workDialog.showModal();
  return workDialogBody;
}

function publicAddress(path: string) {
  return new URL(path, document.body.dataset.publicUrl || location.origin).href;
}

function editPrivacy(work: Work) {
  const endpoint = workEndpoint(work) + "/privacy";
  return openPrivacy({
    open: openWorkDialog,
    read: async () => (await completed(await request(endpoint))).json(),
    save: async (settings) => (await completed(await request(endpoint, "PUT", settings))).json(),
    reload: load,
    absolute: publicAddress,
    status: workDialogStatus,
  });
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
  if (!info.watch && !info.embed) {
    const link = document.createElement("a");
    link.href = "/operator/exhibitions";
    link.textContent = "Manage private screening grants";
    workDialogBody.append("No viewer link has been created. ", link);
    return;
  }
  const watch = info.watch && document.createElement("input");
  const embed = info.embed && document.createElement("textarea");
  if (watch && info.watch) watch.value = publicAddress(info.watch);
  if (embed && info.embed) {
    const iframe = document.createElement("iframe");
    iframe.src = publicAddress(info.embed);
    iframe.title = info.title;
    iframe.width = "640";
    iframe.height = "360";
    iframe.allowFullscreen = true;
    iframe.referrerPolicy = "origin";
    iframe.setAttribute("allow", "autoplay; fullscreen; picture-in-picture");
    embed.value = iframe.outerHTML;
  }
  for (const [label, field] of [
    ["Watch link", watch],
    ["Embed code", embed],
  ] as const) {
    if (!field) continue;
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
  if (copy) {
    const field = copy === "watch" ? watch : embed;
    if (field) await copyText(field);
    else workDialogStatus.textContent = copy === "watch"
      ? "This video is shared through its embed."
      : "Embedding is disabled for this video.";
  }
}

async function mediaTicket(work: Work, sha256: string) {
  const response = await completed(
    await request(workEndpoint(work) + "/ticket", "POST", { sha256 }),
  );
  const urls: { download: string; preview: string } = await response.json();
  return urls;
}

async function reviewLinksDialog(work: Work) {
  const endpoint = workEndpoint(work) + "/review-links";
  const [linkResponse, defaultResponse] = await Promise.all([
    request(endpoint).then(completed),
    request("/api/review-defaults").then(completed),
  ]);
  const links: ReviewLink[] = await linkResponse.json();
  const defaults: ReviewDefaults = await defaultResponse.json();
  openWorkDialog("Review links");
  const editor = document.createElement("form");
  const fields = {
    name: reviewField("Review link name", "text"),
    expires: reviewField("Expires", "datetime-local"),
    password: reviewField("Review password", "password"),
    removePassword: reviewField("Remove saved password", "checkbox"),
    allowDownload: reviewField("Allow download", "checkbox"),
    allowVersions: reviewField("Allow previous versions", "checkbox"),
    allowComment: reviewField("Allow comments", "checkbox"),
    allowStatus: reviewField("Allow status changes", "checkbox"),
    allowResolved: reviewField("Allow resolved comments", "checkbox"),
    dark: reviewField("Dark mode", "checkbox"),
    showOthers: reviewField("Show other videos after playback", "checkbox"),
    logo: reviewField("Review logo", "file"),
    removeLogo: reviewField("Remove saved logo", "checkbox"),
    autoCreate: reviewField("Automatically create review links for new uploads", "checkbox"),
  };
  fields.name.input.required = true;
  fields.name.input.maxLength = 128;
  fields.expires.input.required = true;
  fields.password.input.maxLength = 256;
  fields.password.input.autocomplete = "new-password";
  fields.logo.input.accept = "image/png,image/jpeg,image/webp";
  fields.autoCreate.input.checked = defaults.auto_create;
  const save = document.createElement("button");
  const saveDefaults = document.createElement("button");
  saveDefaults.type = "button";
  saveDefaults.textContent = "Save as default settings";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "New review link";
  let logo: string | null = null;
  const currentChoices = async () => {
    const newLogo = fields.logo.input.files?.[0];
    let reviewLogo = fields.removeLogo.input.checked ? null : logo;
    if (newLogo) {
      const form = new FormData();
      form.append("logo", newLogo);
      const response = await completed(await fetch("/api/review-logos", {
        method: "POST",
        headers: { Authorization: "Bearer " + key.value },
        body: form,
      }));
      reviewLogo = (await response.json()).logo;
    }
    const password = fields.password.input.value;
    return {
      name: fields.name.input.value,
      expires: new Date(fields.expires.input.value).getTime() / 1000,
      allow_download: fields.allowDownload.input.checked,
      allow_versions: fields.allowVersions.input.checked,
      allow_comment: fields.allowComment.input.checked,
      allow_status: fields.allowStatus.input.checked,
      allow_resolved: fields.allowResolved.input.checked,
      dark: fields.dark.input.checked,
      show_other_videos: fields.showOthers.input.checked,
      logo: reviewLogo,
      ...(password || fields.removePassword.input.checked
        ? { password: fields.removePassword.input.checked ? "" : password }
        : {}),
    };
  };
  function populate(link?: ReviewLink) {
    const settings = link || defaults;
    const due = new Date(link ? link.expires * 1000 : Date.now() + defaults.lifetime_seconds * 1000);
    const localDate = new Date(due.getTime() - due.getTimezoneOffset() * 60000);
    fields.name.input.value = link?.name || "";
    fields.expires.input.value = localDate.toISOString().slice(0, 16);
    fields.password.input.value = "";
    fields.removePassword.label.hidden = !settings.password;
    fields.removePassword.input.checked = false;
    fields.allowDownload.input.checked = settings.allow_download;
    fields.allowVersions.input.checked = settings.allow_versions;
    fields.allowComment.input.checked = settings.allow_comment;
    fields.allowStatus.input.checked = settings.allow_status;
    fields.allowResolved.input.checked = settings.allow_resolved;
    fields.dark.input.checked = settings.dark;
    fields.showOthers.input.checked = settings.show_other_videos;
    logo = settings.logo;
    fields.logo.input.value = "";
    fields.removeLogo.label.hidden = !logo;
    fields.removeLogo.input.checked = false;
    save.textContent = link ? "Save review settings" : "Create review link";
    cancel.hidden = !link;
    editor.onsubmit = async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        const body = await currentChoices();
        await completed(await request(
          link ? endpoint + "/" + link.id : endpoint,
          link ? "PUT" : "POST",
          body,
        ));
        await reopen();
        workDialogStatus.textContent = link ? "Review link saved." : "Review link created.";
      } catch (error) {
        workDialogStatus.textContent = String(error);
      } finally {
        save.disabled = false;
      }
    };
  }
  const reopen = () => reviewLinksDialog(work);
  action(saveDefaults, async () => {
    try {
      const choices = await currentChoices();
      await completed(await request("/api/review-defaults", "PUT", {
        ...choices,
        lifetime_seconds: choices.expires - Date.now() / 1000,
        auto_create: fields.autoCreate.input.checked,
      }));
      await reopen();
      workDialogStatus.textContent = "Default review settings saved.";
    } catch (error) {
      workDialogStatus.textContent = String(error);
    }
  });
  cancel.onclick = () => populate();
  editor.append(...Object.values(fields).map(({ label }) => label), save, saveDefaults, cancel);
  populate();

  const list = document.createElement("ul");
  for (const link of links) {
    const row = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = link.name;
    const state = link.expires <= Date.now() / 1000 ? "Expired" : link.enabled ? "Enabled" : "Disabled";
    row.append(title, " · " + state + " · " + new Date(link.expires * 1000).toLocaleString());
    const address = new URL(link.path, document.body.dataset.publicUrl || location.origin);
    const field = document.createElement("input");
    field.value = address.href;
    field.readOnly = true;
    field.setAttribute("aria-label", "Link for " + link.name);
    const open = document.createElement("a");
    open.href = address.href;
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    open.textContent = "Review page";
    row.append(field, open);
    const update = async (path: string, method: string, data?: object) => {
      try {
        await completed(await request(endpoint + "/" + link.id + path, method, data));
        await reopen();
      } catch (error) {
        workDialogStatus.textContent = String(error);
      }
    };
    const buttons: [string, () => Promise<void>][] = [
      ["Copy review link", () => copyText(field)],
      ["Preview as reviewer", async () => {
        const preview = window.open("about:blank", "_blank");
        if (!preview) {
          workDialogStatus.textContent = "Allow a new tab to open the preview.";
          return;
        }
        try {
          preview.opener = null;
          const response = await completed(await request(endpoint + "/" + link.id + "/preview", "POST"));
          const receipt: { path: string } = await response.json();
          preview.location.replace(new URL(receipt.path, location.origin));
        } catch (error) {
          preview.close();
          workDialogStatus.textContent = String(error);
        }
      }],
      ["Manage", async () => { populate(link); fields.name.input.focus(); }],
      ["Comments", () => manageReviewFeedback(work, link, reopen)],
      [link.enabled ? "Disable" : "Enable", () => update("", "PUT", { enabled: !link.enabled })],
      ["Make a copy", () => update("/copy", "POST")],
      ["Delete", () => update("", "DELETE")],
    ];
    for (const [label, click] of buttons) {
      const button = document.createElement("button");
      button.textContent = label;
      action(button, click);
      row.append(button);
    }
    list.append(row);
  }
  workDialogBody.append(list, editor);
}

function reviewField(text: string, type: string) {
  const label = document.createElement("label");
  const input = document.createElement("input");
  input.type = type;
  label.append(text, input);
  return { label, input };
}

async function manageReviewFeedback(work: Work, link: ReviewLink, back: () => Promise<void>) {
  const versions: VideoVersion[] = await (
    await completed(await request(workEndpoint(work) + "/versions"))
  ).json();
  openWorkDialog("Comments for " + link.name);
  const backButton = document.createElement("button");
  backButton.textContent = "Back to review links";
  action(backButton, back);
  const picker = document.createElement("select");
  const label = document.createElement("label");
  label.textContent = "Video version";
  label.append(picker);
  versions.forEach((version, index) => {
    if (version.video_files.some((file) => file.format === "mp4")) {
      picker.append(new Option(version.current ? "Current version" : "Previous version " + index, version.key));
    }
  });
  const list = document.createElement("ol");
  const download = document.createElement("button");
  download.textContent = "Download comments CSV";
  const url = () => link.path + "/feedback/" + picker.value;
  const render = async () => {
    if (!picker.value) {
      workDialogStatus.textContent = "This review has no playable version.";
      download.disabled = true;
      return;
    }
    const data: { comments: ReviewComment[]; status: string } = await (
      await completed(await request(url()))
    ).json();
    workDialogStatus.textContent = data.status;
    list.replaceChildren();
    for (const comment of data.comments) {
      const row = document.createElement("li");
      const author = document.createElement("strong");
      author.textContent = comment.author;
      const text = document.createElement("p");
      renderCommentText(text, comment.body);
      row.append(author, " · " + comment.seconds.toFixed(2) + " seconds", text);
      if (!comment.parent) {
        const control = document.createElement("button");
        control.textContent = comment.resolved ? "Reopen thread" : "Resolve thread";
        action(control, async () => {
          await completed(await request(url() + "/" + comment.id + "/resolved", "PUT", {
            resolved: !comment.resolved,
          }));
          await render();
        });
        row.append(control);
      }
      list.append(row);
    }
    if (!list.children.length) {
      const row = document.createElement("li");
      row.textContent = "No comments yet.";
      list.append(row);
    }
  };
  picker.onchange = () => render().catch((error) => { workDialogStatus.textContent = String(error); });
  action(download, async () => {
    const response = await completed(await request(url() + "/comments.csv"));
    const object = URL.createObjectURL(await response.blob());
    const file = document.createElement("a");
    file.href = object;
    file.download = "review-comments.csv";
    file.click();
    window.setTimeout(() => URL.revokeObjectURL(object), 1000);
  });
  workDialogBody.append(backButton, label, download, list);
  await render();
}

let thumbnailUrl: string | undefined;

async function thumbnailDialog(work: Work) {
  const info = await getWorkInfo(work);
  const endpoint = workEndpoint(work) + "/thumbnail";
  const existing = info.thumbnail ? await request(endpoint) : null;
  const blob = existing?.ok ? await existing.blob() : null;
  openWorkDialog("Change thumbnail");
  if (blob) {
    thumbnailUrl = URL.createObjectURL(blob);
    const current = document.createElement("img");
    current.alt = "Current thumbnail";
    current.src = thumbnailUrl;
    current.className = "thumbnail-preview";
    workDialogBody.append(current);
  }
  const save = async (response: Response) => {
    try {
      await completed(response);
      workDialog.close();
      result.textContent = "Thumbnail saved.";
    } catch (error) {
      workDialogStatus.textContent = String(error);
    }
  };
  const uploadLabel = document.createElement("label");
  const upload = document.createElement("input");
  upload.type = "file";
  upload.accept = "image/jpeg,image/png,image/webp";
  uploadLabel.textContent = "Upload thumbnail";
  uploadLabel.append(upload);
  const uploadButton = document.createElement("button");
  uploadButton.textContent = "Use uploaded image";
  action(uploadButton, async () => {
    const image = upload.files?.[0];
    if (!image) {
      workDialogStatus.textContent = "Choose a thumbnail image.";
      return;
    }
    const data = new FormData();
    data.append("image", image);
    await save(await fetch(endpoint, {
      method: "POST",
      headers: { Authorization: "Bearer " + key.value },
      body: data,
    }));
  });
  workDialogBody.append(uploadLabel, uploadButton);

  if (info.preview) {
    const hash = info.preview;
    const video = document.createElement("video");
    video.muted = true;
    video.preload = "metadata";
    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "0";
    slider.step = "0.01";
    slider.value = "0";
    slider.disabled = true;
    const at = document.createElement("output");
    const label = document.createElement("label");
    label.textContent = "Frame time";
    label.append(slider, at);
    const frameButton = document.createElement("button");
    frameButton.textContent = "Use selected frame";
    frameButton.disabled = true;
    slider.oninput = () => {
      video.currentTime = Number(slider.value);
      at.textContent = slider.value + " seconds";
    };
    video.onloadedmetadata = () => {
      if (!Number.isFinite(video.duration) || video.duration <= 0) return;
      slider.max = Math.max(0, video.duration - 0.1).toFixed(2);
      slider.value = "0";
      slider.disabled = false;
      frameButton.disabled = false;
      at.textContent = "0 seconds";
    };
    action(frameButton, async () => {
      await save(await request(endpoint, "POST", {
        seconds: Number(slider.value),
        media: hash,
      }));
    });
    workDialogBody.append(video, label, frameButton);
    video.src = (await mediaTicket(work, hash)).preview;
  }
}

async function versionsDialog(work: Work) {
  const endpoint = workEndpoint(work) + "/versions";
  const versions: VideoVersion[] = await (
    await completed(await request(endpoint))
  ).json();
  const current = versions[0].key;
  openWorkDialog("Replace video");
  const save = async (response: Promise<Response>, label: string) => {
    try {
      workDialogStatus.textContent = "Preparing the video version…";
      await completed(await response);
      workDialog.close();
      await load();
      result.textContent = label;
    } catch (error) {
      (workDialog.open ? workDialogStatus : result).textContent = String(error);
    }
  };
  const uploadLabel = document.createElement("label");
  const upload = document.createElement("input");
  upload.type = "file";
  upload.accept = ".mp4,.mov,.webm,.mkv";
  uploadLabel.textContent = "Replacement video file";
  uploadLabel.append(upload);
  const replace = document.createElement("button");
  replace.textContent = "Save as new version";
  action(replace, async () => {
    const video = upload.files?.[0];
    if (!video) {
      workDialogStatus.textContent = "Choose a replacement video file.";
      return;
    }
    const inputs = new FormData();
    inputs.append("video", video);
    inputs.append("current", current);
    await save(fetch(endpoint, {
      method: "POST",
      headers: { Authorization: "Bearer " + key.value },
      body: inputs,
    }), "New version saved.");
  });
  const heading = document.createElement("h3");
  heading.textContent = "Version history";
  const list = document.createElement("ol");
  for (const [index, version] of versions.entries()) {
    const entry = document.createElement("li");
    const media = version.video_files[0];
    entry.textContent = [
      version.current ? "Current version" : "Previous version " + index,
      libraryDate(version.saved_at),
      durationText(version.duration_seconds),
      media ? media.format.toUpperCase() : "Video file needed",
    ].filter(Boolean).join(" · ");
    if (!version.current && media) {
      const restore = document.createElement("button");
      restore.textContent = "Restore previous version " + index;
      action(restore, () => save(request(endpoint + "/restore", "POST", {
        current,
        restore: version.key,
      }), "Previous version restored."));
      entry.append(" ", restore);
    }
    list.append(entry);
  }
  workDialogBody.append(uploadLabel, replace, heading, list);
}

async function infoDialog(work: Work, download: boolean) {
  const info = await getWorkInfo(work);
  openWorkDialog(download ? "Download " + info.title : info.asset_type === "video" ? "Video info" : "File info");
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
      preview.textContent = `Load ${info.asset_type} preview`;
      action(preview, async () => {
        const url = (await mediaTicket(work, hash)).preview;
        if (info.asset_type === "image") {
          const image = document.createElement("img");
          image.alt = info.title;
          image.src = url;
          preview.replaceWith(image);
        } else {
          const player = document.createElement(info.asset_type === "audio" ? "audio" : "video");
          player.src = url;
          player.controls = true;
          player.preload = "metadata";
          preview.replaceWith(player);
        }
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
      file.name,
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
  else workDialogBody.append("No file is available.");
}

workDialog.onclose = () => {
  workDialogBody.replaceChildren();
  if (thumbnailUrl) URL.revokeObjectURL(thumbnailUrl);
  thumbnailUrl = undefined;
};

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
  assetType.onchange =
  privacy.onchange =
  sort.onchange =
  view.onchange =
    () => {
      selected.clear();
      renderWorks();
    };

libraryUploader({
  available: () => !library.hidden && section === "Library",
  folder: () => current,
  folderOptions,
  credential: () => key.value,
  reload: () => load(),
});

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
