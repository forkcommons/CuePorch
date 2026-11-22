import { checked, sessionMarker } from "./team_api.js";
import { setupSeriesPresentation } from "./event_series_presentation.js";
import type { Presentation } from "./event_series_presentation.js";

export type Visibility = "private" | "team" | "public" | "password";

export interface Settings {
  title: string;
  description: string;
  visibility: Visibility;
  events: string[];
  presentation: Presentation;
}

export interface Series {
  id: string;
  slug: string;
  revision: string;
  settings: Settings;
  publication: string;
  watch: string | null;
  has_password: boolean;
}

export interface EventChoice {
  id: string;
  title: string;
  visibility: Visibility;
}

export interface ListResponse {
  series: Series[];
  events: EventChoice[];
}

const byId = <T extends HTMLElement = HTMLElement>(id: string) =>
  document.getElementById(id) as T;

const identify = byId<HTMLFormElement>("seriesIdentify");
const workspace = byId("seriesWorkspace");
const status = byId("seriesStatus");
const list = byId<HTMLUListElement>("seriesList");
const eventChoices = byId("seriesEventChoices");
const form = byId<HTMLFormElement>("seriesEdit");
const editorHeading = byId("seriesEditHeading");
const previewLink = byId<HTMLAnchorElement>("seriesPreviewLink");
const watchLink = byId<HTMLAnchorElement>("seriesWatchLink");

let credential =
  sessionStorage.getItem("cueporchOperator") || sessionMarker;

const presentation = setupSeriesPresentation(
  document.querySelector<HTMLElement>("#seriesPresentation")!,
  async file => {
    const data = new FormData();
    data.set("logo", file);
    const response = await checked(
      await fetch("/api/review-logos", {
        method: "POST",
        headers: { Authorization: "Bearer " + credential },
        body: data,
      }),
    );
    return (await response.json() as { logo: string }).logo;
  },
);

async function api<T>(path: string, method = "GET", body?: object): Promise<T> {
  const headers: Record<string, string> = {};
  if (credential) headers.Authorization = `Bearer ${credential}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`/api/event-series${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const checkedResponse = await checked(response);
  if (checkedResponse.status === 204) return null as T;
  return (await checkedResponse.json()) as T;
}

let current: Series | null = null;
let choices: EventChoice[] = [];
let entries: Series[] = [];

function field(name: string) {
  return form.elements.namedItem(name) as
    | HTMLInputElement
    | HTMLTextAreaElement
    | HTMLSelectElement;
}

function renderEditor(entry: Series | null) {
  current = entry;
  form.hidden = !entry;
  if (!entry) return;

  presentation.populate(entry.settings.presentation);
  editorHeading.textContent = entry.settings.title;
  (field("title") as HTMLInputElement).value = entry.settings.title;
  (field("description") as HTMLTextAreaElement).value =
    entry.settings.description;
  field("slug").value = entry.slug;
  const visibilitySelect = field("visibility") as HTMLSelectElement;
  visibilitySelect.value = entry.settings.visibility;

  const passwordRow = byId("seriesPasswordRow");
  const passwordInput = field("view_password") as HTMLInputElement;
  passwordInput.value = "";
  passwordInput.required =
    entry.settings.visibility === "password" && !entry.has_password;
  passwordRow.hidden = entry.settings.visibility !== "password";

  const selected = new Set(entry.settings.events);
  const ordered = [
    ...entry.settings.events.flatMap((id) => choices.filter((choice) => choice.id === id)),
    ...choices.filter((c) => !selected.has(c.id)),
  ];
  eventChoices.querySelectorAll("label").forEach((label) => label.remove());
  for (const choice of ordered) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "events";
    input.value = choice.id;
    input.checked = selected.has(choice.id);
    const text = document.createElement("span");
    text.textContent = choice.title;
    label.append(input, text);
    eventChoices.append(label);
  }

  previewLink.hidden = true;
  previewLink.removeAttribute("href");
  watchLink.hidden = !entry.watch;
  if (entry.watch) watchLink.href = entry.watch;
  else watchLink.removeAttribute("href");
}

function renderList() {
  list.replaceChildren(
    ...entries.map((entry) => {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = entry.settings.title || entry.slug;
      if (current?.id === entry.id) button.setAttribute("aria-current", "true");
      button.onclick = () => {
        renderEditor(entry);
        list.querySelectorAll("button").forEach((b) => {
          if (b !== button) b.removeAttribute("aria-current");
        });
        button.setAttribute("aria-current", "true");
      };
      li.append(button);
      return li;
    }),
  );
}

let busy = false;

const setStatus = (msg: string, err = false) => {
  status.textContent = msg;
  status.classList.toggle("error", err);
};

const seriesNew = document.querySelector<HTMLButtonElement>("#seriesNew")!;
const seriesDelete = document.querySelector<HTMLButtonElement>("#seriesDelete")!;
const seriesPreview = document.querySelector<HTMLButtonElement>("#seriesPreview")!;

{
  const visibilitySelect = field("visibility") as HTMLSelectElement;
  const passwordRow = byId("seriesPasswordRow");
  const passwordInput = field("view_password") as HTMLInputElement;
  visibilitySelect.addEventListener("change", () => {
    passwordInput.value = "";
    passwordRow.hidden = visibilitySelect.value !== "password";
    passwordInput.required =
      visibilitySelect.value === "password" && !current?.has_password;
  });
}

const setBusy = (v: boolean) => {
  busy = v;
  form.inert = v;
  seriesNew.disabled = v;
  list.inert = identify.inert = v;
};

async function withBusy(fn: () => Promise<void>): Promise<void> {
  if (busy) return;
  setBusy(true);
  try {
    await fn();
  } catch (e) {
    setStatus((e as Error).message || "Request failed.", true);
  } finally {
    setBusy(false);
  }
}

async function load(): Promise<void> {
  const { series, events } = await api<ListResponse>("");
  workspace.hidden = false;
  identify.hidden = true;
  entries = series;
  choices = events;
  if (current) current = entries.find(s => s.id === current!.id) ?? null;
  renderList();
  renderEditor(current);
}

void load().catch(() => {});

identify.addEventListener("submit", e => {
  e.preventDefault();
  const input = identify.querySelector<HTMLInputElement>('[name="credential"]');
  credential = (input?.value ?? "").trim();
  if (!credential) return;
  sessionStorage.setItem("cueporchOperator", credential);
  void withBusy(load);
});

form.addEventListener("submit", e => {
  e.preventDefault();
  if (!current) {
    setStatus("Select a draft first.", true);
    return;
  }
  const target = current;
  void withBusy(async () => {
    const payload = {
      slug: (field("slug") as HTMLInputElement).value,
      view_password:
        (field("view_password") as HTMLInputElement).value || undefined,
      settings: {
        title: (field("title") as HTMLInputElement).value.trim(),
        description: (field("description") as HTMLTextAreaElement).value.trim(),
        visibility: (field("visibility") as HTMLSelectElement).value,
        events: Array.from(
          eventChoices.querySelectorAll<HTMLInputElement>('input[type="checkbox"]:checked'),
        ).map(el => el.value),
        presentation: await presentation.payload(),
      },
    };
    current = await api<Series>(`/${target.id}`, "PUT", {
      revision: target.revision,
      ...payload,
    });
    setStatus(
      current.publication === "eligible"
        ? "Event series published."
        : current.publication === "waiting"
        ? "Waiting for publication approval."
        : "Private draft saved.",
    );
    await load();
  });
});

seriesNew.addEventListener("click", () => {
  void withBusy(async () => {
    current = await api<Series>("", "POST", {});
    setStatus("Private draft saved.");
    await load();
  });
});

seriesDelete.addEventListener("click", e => {
  e.preventDefault();
  if (busy || !current) return;
  const target = current;
  void withBusy(async () => {
    await api(`/${target.id}`, "DELETE", { revision: target.revision });
    current = null;
    renderEditor(null);
    await load();
  });
});

seriesPreview.addEventListener("click", e => {
  e.preventDefault();
  if (busy || !current) return;
  const target = current;
  void withBusy(async () => {
    const { preview } = await api<{ preview: string }>(`/${target.id}/preview`, "POST");
    previewLink.href = preview;
    previewLink.hidden = false;
  });
});
