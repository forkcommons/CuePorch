import { checked, sessionMarker } from "./team_api.js";
import { setupSeriesPresentation } from "./event_series_presentation.js";
import { setupSeriesLocalization } from "./event_series_localization.js";
import { setupSeriesTranslationJobs } from "./event_series_translation_jobs.js";
import type { Localization } from "./event_series_localization.js";
import type { Presentation } from "./event_series_presentation.js";

export type Visibility = "private" | "team" | "public" | "password";

export type UpcomingMode = "all" | "next" | "grouped";

export interface HiddenRecording {
  event: string;
  opens: number;
}

export interface Recording {
  event: string;
  opens: number;
  title: string;
  label: string;
}

export interface Settings {
  title: string;
  description: string;
  visibility: Visibility;
  custom_host: string;
  upcoming_mode: UpcomingMode;
  localization: Localization;
  events: string[];
  hidden_recordings: HiddenRecording[];
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
  recordings: Recording[];
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
const seriesSearch = document.querySelector<HTMLInputElement>("#seriesSearch")!;
const seriesSort = document.querySelector<HTMLSelectElement>("#seriesSort")!;
const seriesView = document.querySelector<HTMLSelectElement>("#seriesView")!;
const seriesEmpty = document.querySelector<HTMLElement>("#seriesEmpty")!;
const seriesCopyLink = document.querySelector<HTMLButtonElement>("#seriesCopyLink")!;

let credential =
  sessionStorage.getItem("cueporchOperator") || sessionMarker;

const localization = setupSeriesLocalization(byId("seriesLocalization"));
const translationJobs = setupSeriesTranslationJobs(
  byId("seriesLocalization"),
  api,
  (language, translation) => localization.applyDraft(language, translation),
  () => withBusy(load),
);

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
let recordings: Recording[] = [];

const hiddenKey = (event: string, opens: number) => event + "\u0000" + opens;

function renderRecordingChoices(): void {
  if (!current) return;
  const container = byId("seriesHiddenRecordings");

  const prev = new Map<string, boolean>();
  container
    .querySelectorAll<HTMLInputElement>('input[type="checkbox"]')
    .forEach(el =>
      prev.set(
        hiddenKey(el.dataset.event!, Number(el.dataset.opens)),
        el.checked,
      ),
    );

  const selected = new Set(
    Array.from(
      eventChoices.querySelectorAll<HTMLInputElement>('input[name="events"]:checked'),
    ).map(el => el.value),
  );
  const saved = new Set(
    current.settings.hidden_recordings.map(hr => hiddenKey(hr.event, hr.opens)),
  );

  container.replaceChildren();
  for (const rec of recordings) {
    if (!selected.has(rec.event)) continue;
    const key = hiddenKey(rec.event, rec.opens);
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = "hidden_recordings";
    input.dataset.event = rec.event;
    input.dataset.opens = String(rec.opens);
    input.checked = prev.get(key) ?? saved.has(key);
    const text = document.createElement("span");
    text.textContent = `Hide recording: ${rec.title} · ${rec.label}`;
    label.append(input, text);
    container.append(label);
  }
}

function collectHiddenRecordings(target: Series): HiddenRecording[] {
  const selected = new Set(
    Array.from(
      eventChoices.querySelectorAll<HTMLInputElement>('input[name="events"]:checked'),
    ).map(el => el.value),
  );
  const result = new Map<string, HiddenRecording>();
  for (const hr of target.settings.hidden_recordings) {
    if (selected.has(hr.event)) result.set(hiddenKey(hr.event, hr.opens), hr);
  }
  const container = byId("seriesHiddenRecordings");
  for (const el of container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')) {
    const event = el.dataset.event!;
    const opens = Number(el.dataset.opens);
    const key = hiddenKey(event, opens);
    if (el.checked) result.set(key, { event, opens });
    else result.delete(key);
  }
  return Array.from(result.values());
}

function field(name: string) {
  return form.elements.namedItem(name) as
    | HTMLInputElement
    | HTMLTextAreaElement
    | HTMLSelectElement;
}

function renderEditor(entry: Series | null) {
  current = entry;
  form.hidden = !entry;
  if (!entry) {
    translationJobs.populate(null);
    return;
  }

  presentation.populate(entry.settings.presentation);
  localization.populate(entry.settings.localization);
  translationJobs.populate(entry);
  editorHeading.textContent = entry.settings.title;
  (field("title") as HTMLInputElement).value = entry.settings.title;
  (field("description") as HTMLTextAreaElement).value =
    entry.settings.description;
  field("slug").value = entry.slug;
  field("custom_host").value = entry.settings.custom_host;
  const visibilitySelect = field("visibility") as HTMLSelectElement;
  visibilitySelect.value = entry.settings.visibility;
  (field("upcoming_mode") as HTMLSelectElement).value = entry.settings.upcoming_mode;

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
    input.addEventListener("change", renderRecordingChoices);
    const text = document.createElement("span");
    text.textContent = choice.title;
    label.append(input, text);
    eventChoices.append(label);
  }
  byId("seriesHiddenRecordings").replaceChildren();
  renderRecordingChoices();

  previewLink.hidden = true;
  previewLink.removeAttribute("href");
  watchLink.hidden = !entry.watch;
  if (entry.watch) watchLink.href = entry.watch;
  else watchLink.removeAttribute("href");
  seriesCopyLink.hidden = !entry.watch;
  seriesCopyLink.onclick = async () => {
    if (!entry.watch) return;
    try {
      await navigator.clipboard.writeText(new URL(entry.watch, location.href).href);
      setStatus("Link copied.");
    } catch {
      setStatus("Could not copy the link.", true);
    }
  };
}

function getVisibleEntries(): Series[] {
  const searchTerm = seriesSearch.value.trim().toLowerCase();
  const sortMode = seriesSort.value;
  let result = entries.filter(entry => {
    if (!searchTerm) return true;
    const title = entry.settings.title.toLowerCase();
    const slug = entry.slug.toLowerCase();
    return title.includes(searchTerm) || slug.includes(searchTerm);
  });
  if (sortMode === "oldest") {
    result = result.slice().reverse();
  } else if (sortMode === "title") {
    result = result.slice().sort((a, b) => {
      const at = (a.settings.title || a.slug).toLowerCase();
      const bt = (b.settings.title || b.slug).toLowerCase();
      return at.localeCompare(bt);
    });
  }
  return result;
}

function renderList() {
  const visible = getVisibleEntries();
  list.dataset.view = seriesView.value;
  seriesEmpty.textContent = visible.length ? "" : "No event series match your search.";
  seriesEmpty.hidden = visible.length > 0;
  list.replaceChildren(
    ...visible.map((entry) => {
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
    })
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
  const { series, events, recordings: rec } = await api<ListResponse>("");
  workspace.hidden = false;
  identify.hidden = true;
  entries = series;
  choices = events;
  recordings = rec;
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
        custom_host: field("custom_host").value,
        upcoming_mode: (field("upcoming_mode") as HTMLSelectElement).value as UpcomingMode,
        events: Array.from(
          eventChoices.querySelectorAll<HTMLInputElement>('input[name="events"]:checked'),
        ).map(el => el.value),
        hidden_recordings: collectHiddenRecordings(target),
        localization: localization.payload(),
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

seriesSearch.addEventListener("input", () => renderList());
seriesSort.addEventListener("change", () => renderList());
seriesView.addEventListener("change", () => {
  list.dataset.view = seriesView.value;
  renderList();
});
