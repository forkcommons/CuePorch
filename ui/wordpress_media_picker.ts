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
  return response.json() as Promise<T>;
}

async function loadLibrary(): Promise<void> {
  authForm.hidden = true;
  library.hidden = true;
  status.textContent = "Loading library…";
  status.setAttribute("aria-busy", "true");
  try {
    works = await getJson<LibraryWork[]>("/api/library");
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

startPicker();
