import { privateSettings } from "./library_privacy.js";
import { createProductVideo } from "./product_video_recording.js";
import {
  libraryUploader,
  uploadLibraryFile,
  readUploadPrivacy,
  folderChoices,
  type UploadFolder,
} from "./library_upload.js";

type LibraryWork = {
  id: string;
  title: string;
  asset_type: string;
  visibility: string | null;
  editable_privacy: boolean;
};

const destinationLabel = document.getElementById(
  "pickerDestination",
) as HTMLElement;
const status = document.getElementById("pickerStatus") as HTMLElement;
const authForm = document.getElementById("pickerAuth") as HTMLFormElement;
const keyInput = document.getElementById("pickerKey") as HTMLInputElement;
const library = document.getElementById("pickerLibrary") as HTMLElement;
const search = document.getElementById("pickerSearch") as HTMLInputElement;
const results = document.getElementById("pickerResults") as HTMLElement;

let works: LibraryWork[] = [];
let folders: UploadFolder[] = [];
let token = "";
try {
  if (typeof sessionStorage !== "undefined") {
    const stored = sessionStorage.getItem("cueporchOperator");
    if (stored) token = stored;
  }
} catch {
  /* sessionStorage unavailable */
}

const workId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const picker = pickerTarget();

function startPicker(): void {
  if (!picker || !picker.destination) {
    status.textContent = "Invalid picker launch";
    return;
  }
  destinationLabel.textContent = picker.destination;
  mountLibraryUploader();
  mountProductCreation();
  mountPhotoCreation();
  void loadLibrary();
  search.addEventListener("input", () => renderWorks());
  authForm.addEventListener("submit", (ev: Event) => {
    ev.preventDefault();
    token = keyInput.value.trim();
    keyInput.value = "";
    void loadLibrary();
  });
}

export {};

function pickerTarget(): {
  destination: string;
  request: string;
  opener: Window;
} | null {
  const p = new URLSearchParams(location.search),
    d = p.get("destination"),
    r = p.get("request");
  if (
    !d ||
    !r ||
    !window.opener ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      r,
    )
  )
    return null;
  let u: URL;
  try {
    u = new URL(d);
  } catch {
    return null;
  }
  if (
    u.origin !== d ||
    u.pathname !== "/" ||
    u.username ||
    u.password ||
    u.search ||
    u.hash
  )
    return null;
  const loop =
    u.protocol === "http:" &&
    (u.hostname === "localhost" ||
      u.hostname === "[::1]" ||
      /^127(?:\.\d{1,3}){3}$/.test(u.hostname));
  if (u.protocol !== "https:" && !loop) return null;
  return { destination: d, request: r, opener: window.opener as Window };
}

async function getJson<T>(path: string): Promise<T> {
  return (await loadLibraryResponse(path)).json() as Promise<T>;
}

async function loadLibraryResponse(path: string): Promise<Response> {
  if (path !== "/api/library" && !path.startsWith("/api/library/"))
    throw Object.assign(new Error("Invalid path"), { status: 400 });
  const response = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  if (!response.ok)
    throw Object.assign(new Error("Request failed"), {
      status: response.status,
    });
  return response;
}

function mountLibraryUploader(): void {
  libraryUploader({
    available: () => !library.hidden,
    folder: () => null,
    folderOptions: (select) => folderChoices(select, folders),
    credential: () => token,
    defaults: (folder) => readUploadPrivacy(folder, loadLibraryResponse),
    reload: async () => {
      works = await getJson<LibraryWork[]>("/api/library");
      renderWorks();
    },
  });
}

async function loadLibrary(): Promise<void> {
  authForm.hidden = true;
  library.hidden = true;
  status.textContent = "Loading library…";
  status.setAttribute("aria-busy", "true");
  try {
    [works, folders] = await Promise.all([
      getJson<LibraryWork[]>("/api/library"),
      getJson<UploadFolder[]>("/api/library/folders").catch(() => []),
    ]);
    renderWorks();
    library.hidden = false;
    keyInput.value = "";
    status.textContent = "Library loaded.";
  } catch (error) {
    const code =
      error instanceof Error && "status" in error
        ? (error as { status: number }).status
        : 0;
    if (code === 401 || code === 403) {
      token = "";
      keyInput.value = "";
      authForm.hidden = false;
      status.textContent = "Session expired. Please sign in again.";
    } else {
      authForm.hidden = false;
      status.textContent = "Could not load the library. Please try again.";
    }
  } finally {
    status.removeAttribute("aria-busy");
  }
}

function renderWorks(): void {
  const q = search.value.trim().toLowerCase();
  const list = works.filter(
    (w) =>
      w.editable_privacy &&
      w.asset_type === "video" &&
      (w.visibility === "public" ||
        w.visibility === "password" ||
        w.visibility === "unlisted") &&
      workId.test(w.id) &&
      w.title.toLowerCase().includes(q),
  );
  results.replaceChildren();
  if (!list.length)
    results.append(
      Object.assign(document.createElement("li"), {
        textContent: "No works found",
      }),
    );
  else
    for (const work of list) {
      const li = document.createElement("li");
      const btn = Object.assign(document.createElement("button"), {
        type: "button",
        textContent: `Choose ${work.title}`,
      });
      btn.addEventListener("click", () => void chooseWork(work));
      li.append(btn);
      results.append(li);
    }
}

function safeEmbedUrl(id: string, path: string | null): string | null {
  if (!workId.test(id) || path === null) return null;
  const base = "/embed/work/" + id;
  if (path === base) return location.origin + path;
  const keyPattern = /^\?key=[A-Za-z0-9_-]{1,128}$/;
  if (path.startsWith(base) && keyPattern.test(path.slice(base.length)))
    return location.origin + path;
  return null;
}

async function chooseWork(work: LibraryWork): Promise<void> {
  try {
    if (!picker) throw new Error("Picker window unavailable");
    const privacy = await getJson<{
      visibility: string;
      embed: string;
      embed_url: string | null;
    }>(`/api/library/work/${work.id}/privacy`);
    if (
      !["public", "password", "unlisted"].includes(privacy.visibility) ||
      privacy.embed === "disabled"
    )
      throw new Error("Work is not embeddable");
    const embedUrl = safeEmbedUrl(work.id, privacy.embed_url);
    if (!embedUrl) throw new Error("Invalid embed URL");
    picker.opener.postMessage(
      {
        type: "cueporch-wordpress-selected",
        request: picker.request,
        embedUrl,
      },
      picker.destination,
    );
    window.close();
  } catch {
    status.textContent =
      "This video is unavailable for embedding. Check its sharing settings.";
  }
}

async function saveProductVideoDraft(draft: {
  id: string;
  file: File;
}): Promise<string> {
  const { id, file } = draft;
  const receipt = await uploadLibraryFile(
    file,
    id,
    "",
    token,
    { ...privateSettings, password: "" },
    (progress) => {
      status.textContent =
        progress == null
          ? "Preparing product video…"
          : `Uploading ${Math.round(progress)}%`;
    },
  );
  if (
    receipt.pending ||
    receipt.visibility !== "private" ||
    !workId.test(receipt.id)
  )
    throw new Error("receipt invalid");
  return receipt.id;
}

function parseProductSource(
  message: Record<string, unknown>,
): { title: string; files: File[] } | null {
  const rawTitle = message.title;
  if (typeof rawTitle !== "string") return null;
  const title = rawTitle.trim().slice(0, 100);
  if (!title) return null;
  const rows = message.photos;
  if (!Array.isArray(rows) || rows.length < 1 || rows.length > 8) return null;
  const allowed = ["image/jpeg", "image/png", "image/webp"];
  const files: File[] = [];
  for (const row of rows) {
    if (!row || typeof row !== "object") return null;
    const {
      name,
      mediaType: type,
      bytes,
    } = row as { name?: unknown; mediaType?: unknown; bytes?: unknown };
    if (typeof name !== "string" || !name || name.length > 180) return null;
    if (typeof type !== "string" || !allowed.includes(type)) return null;
    if (
      !(bytes instanceof ArrayBuffer) ||
      bytes.byteLength <= 0 ||
      bytes.byteLength > 8 * 1024 * 1024
    )
      return null;
    files.push(new File([bytes], name, { type }));
  }
  return { title, files };
}

function mountProductCreation(): void {
  if (!picker) return;
  const context = picker;
  if (new URLSearchParams(location.search).get("mode") !== "create") return;
  const section = document.getElementById("pickerProductCreate") as HTMLElement;
  const button = document.getElementById(
    "pickerCreateVideo",
  ) as HTMLButtonElement;
  const editLink = document.getElementById(
    "pickerEditDraft",
  ) as HTMLAnchorElement;
  let staged: { id: string; file: File } | null = null;
  let awaiting = false;

  async function receiveSource(event: MessageEvent): Promise<void> {
    if (!awaiting) return;
    if (event.source !== context.opener || event.origin !== context.destination)
      return;
    if (event.data === null || typeof event.data !== "object") return;
    const m = event.data as Record<string, unknown>;
    if (
      m.request !== context.request ||
      (m.type !== "cueporch-wordpress-product-source" &&
        m.type !== "cueporch-wordpress-product-error")
    )
      return;
    awaiting = false;
    const source = parseProductSource(m);
    if (m.type === "cueporch-wordpress-product-error" || !source) {
      status.textContent = "Invalid product source.";
      button.disabled = false;
      return;
    }
    try {
      const file = await createProductVideo(source.title, source.files);
      staged = { id: crypto.randomUUID(), file };
      await uploadStaged();
    } catch {
      status.textContent = "Unable to decode product source.";
      button.disabled = false;
    }
  }

  function requestSource(): void {
    button.disabled = true;
    if (staged) {
      void uploadStaged();
      return;
    }
    awaiting = true;
    context.opener.postMessage(
      { type: "cueporch-wordpress-product-request", request: context.request },
      context.destination,
    );
    status.textContent = "Reading WordPress photos";
  }

  async function uploadStaged(): Promise<void> {
    if (!staged) return;
    try {
      const id = await saveProductVideoDraft(staged);
      editLink.href = `/operator/videos/${id}/timeline`;
      editLink.hidden = false;
      status.textContent = "Draft saved privately";
      button.disabled = true;
    } catch {
      button.disabled = false;
      status.textContent = "Upload failed. Retry the same upload.";
    }
  }

  button.addEventListener("click", requestSource);
  window.addEventListener("message", (event) => void receiveSource(event));
  section.hidden = false;
  button.disabled = false;
}

function mountPhotoCreation(): void {
  if (new URLSearchParams(location.search).get("mode") === "create") return;
  const details = document.getElementById(
    "pickerPhotoCreation",
  ) as HTMLDetailsElement | null;
  const form = document.getElementById(
    "pickerPhotoCreateForm",
  ) as HTMLFormElement | null;
  const titleInput = document.getElementById(
    "pickerPhotoTitle",
  ) as HTMLInputElement | null;
  const filesInput = document.getElementById(
    "pickerPhotoFiles",
  ) as HTMLInputElement | null;
  const startBtn = document.getElementById(
    "pickerPhotoCreateStart",
  ) as HTMLButtonElement | null;
  const editLink = document.getElementById(
    "pickerPhotoEditDraft",
  ) as HTMLAnchorElement | null;
  if (!details || !form || !titleInput || !filesInput || !startBtn || !editLink)
    return;
  details.hidden = false;
  const allowed = ["image/jpeg", "image/png", "image/webp"];
  let staged: { id: string; file: File } | null = null;
  const reset = () => {
    staged = null;
    editLink.hidden = true;
  };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (startBtn.disabled) return;
    const title = titleInput.value.trim();
    const files = Array.from(filesInput.files ?? []);
    if (!title || files.length < 1 || files.length > 8) {
      status.textContent = "Enter a title and select 1–8 photos.";
      return;
    }
    const bad = files.some(
      (f) =>
        !allowed.includes(f.type) || f.size <= 0 || f.size > 8 * 1024 * 1024,
    );
    if (bad) {
      status.textContent =
        "Use 1–8 JPEG, PNG, or WebP photos up to 8 MiB each.";
      return;
    }
    titleInput.disabled = filesInput.disabled = startBtn.disabled = true;
    try {
      if (!staged)
        staged = {
          id: crypto.randomUUID(),
          file: await createProductVideo(title, files),
        };
      const id = await saveProductVideoDraft(staged);
      editLink.href = `/operator/videos/${id}/timeline`;
      editLink.hidden = false;
      status.textContent = "Draft saved privately";
    } catch {
      status.textContent = "Draft save failed. Retry to reuse staged upload.";
    } finally {
      titleInput.disabled = filesInput.disabled = startBtn.disabled = false;
    }
  });
  titleInput.addEventListener("input", reset);
  filesInput.addEventListener("change", reset);
}

startPicker();
