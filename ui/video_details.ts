import { embedBuilder } from "./embed_builder.js";
import {sessionMarker} from "./team_api.js";
import {sharingFields, type Sharing} from "./library_privacy.js";
import {videoAIDrafts} from "./video_ai_drafts.js";

interface VideoDetails {
  title: string;
  description: string;
  embed_pages: string[];
  tags: string[];
  categories: string[];
  credits: {name: string; role: string}[];
  license: string | null;
  rating: string;
  content: string[];
  realistic_ai: boolean;
  advertisement: boolean;
  media: {
    bytes: number;
    format: string;
    width?: number;
    height?: number;
    fps?: number;
    audio?: string;
  } | null;
  duration_seconds: number | null;
  projection: string | null;
  panorama: string;
  added_at: string | null;
}

interface WorkspaceInfo {
  watch: string | null;
  embed: string | null;
  preview: string | null;
  vimeo_source: boolean;
  editable_privacy: boolean;
}

interface ShareLink {
  watch: string | null;
  embed_url: string | null;
  alias: {slug: string; days: number; expires: number | null; path: string | null} | null;
}

interface EditorialStatus {
  version: string;
  status: string;
}

const identify = document.querySelector<HTMLFormElement>("#identify")!;
const key = document.querySelector<HTMLInputElement>("#operatorKey")!;
const status = document.querySelector<HTMLElement>("#detailsStatus")!;
const pane = document.querySelector<HTMLElement>("#videoDetails")!;
const form = document.querySelector<HTMLFormElement>("#videoMeta")!;
const submit = form.querySelector<HTMLButtonElement>("button[type=submit]")!;
const facts = document.querySelector<HTMLDListElement>("#videoFacts")!;
const heading = document.querySelector<HTMLElement>("#videoHeading")!;
const descriptors = document.querySelector<HTMLFieldSetElement>("#matureReasons")!;
const input = (name: string) => form.elements.namedItem(name) as HTMLInputElement;
const rating = form.elements.namedItem("rating") as HTMLSelectElement;
const preview = document.querySelector<HTMLVideoElement>("#workspacePreview")!;
const previewMessage = document.querySelector<HTMLParagraphElement>("#previewMessage")!;
const editorial = document.querySelector<HTMLSelectElement>("#editorialState")!;
const viewer = document.querySelector<HTMLAnchorElement>("#viewerPage")!;
const links = document.querySelector<HTMLElement>("#workspaceLinks")!;
const start = document.querySelector<HTMLInputElement>("#shareStart")!;
const capture = document.querySelector<HTMLButtonElement>("#captureTime")!;
const viewerLink = document.querySelector<HTMLInputElement>("#workspaceWatch")!;
const embedCode = document.querySelector<HTMLTextAreaElement>("#workspaceEmbed")!;
const copyEmbed = document.querySelector<HTMLButtonElement>("#copyEmbed")!;
const reviewLink = document.querySelector<HTMLInputElement>("#workspaceReview")!;
const copyReview = document.querySelector<HTMLButtonElement>("#copyReview")!;
const custom = document.querySelector<HTMLFormElement>("#customLink")!;
const expires = document.querySelector<HTMLElement>("#customExpires")!;
const clearCustom = document.querySelector<HTMLButtonElement>("#clearCustom")!;
const sharingMessage = document.querySelector<HTMLElement>("#sharingMessage")!;
const privacy = document.querySelector<HTMLAnchorElement>("#editVideoPrivacy")!;
const sourcePrivacy = document.querySelector<HTMLAnchorElement>("#restoreSourcePrivacy")!;
const builder = embedBuilder();

const teamPreview = document.body.dataset.teamPreview;
key.value = teamPreview ? sessionMarker : sessionStorage.getItem("cueporchOperator") || "";
const endpoint = "/api/library/work/" + encodeURIComponent(document.body.dataset.work || "");
let info: WorkspaceInfo;
let sharing: ShareLink;
let editorialState: EditorialStatus;

async function call<T>(resource = "", method = "GET", body?: object): Promise<T> {
  const headers = {Authorization: "Bearer " + key.value, "Content-Type": "application/json",
    ...(teamPreview ? {"X-CuePorch-Team": "1"} : {})};
  const response = await fetch(endpoint + resource, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const html = new DOMParser().parseFromString(await response.text(), "text/html");
    throw new Error(html.querySelector("p")?.textContent || `Request failed (${response.status})`);
  }
  return response.status === 204 ? undefined as T : response.json();
}

const loadAIDrafts = videoAIDrafts(call, ({title, description, tags}) => {
  heading.textContent = title;
  input("title").value = title;
  input("description").value = description;
  input("tags").value = tags.join("\n");
});

function contentVisibility() {
  descriptors.hidden = descriptors.disabled = rating.value !== "mature";
}
rating.onchange = contentVisibility;

function show(details: VideoDetails) {
  heading.textContent = details.title;
  for (const name of ["title", "description", "license", "rating", "panorama"] as const) {
    input(name).value = details[name] || "";
  }
  for (const name of ["tags", "categories"] as const) input(name).value = details[name].join("\n");
  input("embed_pages").value = details.embed_pages.join("\n");
  input("credits").value = details.credits.map(({ name, role }) => role ? `${name} | ${role}` : name).join("\n");
  input("realistic_ai").checked = details.realistic_ai;
  input("advertisement").checked = details.advertisement;
  for (const control of form.querySelectorAll<HTMLInputElement>("[name=content]")) {
    control.checked = details.content.includes(control.value);
  }
  contentVisibility();
  const media = details.media;
  const values = {
    Format: media?.format.toUpperCase(),
    Resolution: media?.width && media.height ? `${media.width} × ${media.height}` : "",
    "File size": media ? `${media.bytes.toLocaleString()} bytes` : "",
    "Frame rate": media?.fps ? `${media.fps} fps` : "",
    Audio: media?.audio,
    "Imported projection": details.projection,
    Duration: details.duration_seconds === null ? "" : `${details.duration_seconds} seconds`,
    Added: details.added_at ? new Date(details.added_at).toLocaleDateString() : "",
  };
  facts.replaceChildren();
  for (const [label, value] of Object.entries(values)) {
    if (!value) continue;
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    facts.append(term, description);
  }
  if (!media) {
    const empty = document.createElement("dd");
    empty.textContent = "A media file has not been recovered for this work.";
    facts.append(empty);
  }
}

function address(path: string) {
  return new URL(path, document.body.dataset.publicUrl || location.origin);
}

function renderLinks() {
  const watch = sharing.alias?.path || sharing.watch || info.watch;
  const embed = sharing.embed_url || info.embed;
  const target = watch ? address(watch) : null;
  if (target && sharing.watch && start.validity.valid && start.valueAsNumber > 0) {
    target.searchParams.set("start", start.value);
  }
  viewer.hidden = !target;
  if (target) viewer.href = target.href;
  viewerLink.value = target?.href || "";
  links.hidden = !target && !embed;
  viewerLink.parentElement!.hidden = !target;
  start.parentElement!.hidden = !sharing.watch;
  document.querySelector<HTMLButtonElement>("#copyWatch")!.hidden = !target;
  capture.hidden = !sharing.watch || preview.hidden;
  builder.show(embed ? address(embed) : null, heading.textContent || "Video player", Boolean(sharing.embed_url));
  custom.hidden = !sharing.watch;
  const alias = sharing.alias;
  clearCustom.hidden = !alias;
  expires.hidden = !alias;
  expires.textContent = alias ? (alias.expires
    ? (alias.path ? "Expires " : "Expired ") + new Date(alias.expires * 1000).toLocaleString()
    : "This link has no expiration.") : "";
  document.querySelector<HTMLElement>("#shareUnavailable")!.hidden = Boolean(target || embed);
}

function populateCustom() {
  (custom.elements.namedItem("slug") as HTMLInputElement).value = sharing.alias?.slug || "";
  (custom.elements.namedItem("days") as HTMLSelectElement).value = String(sharing.alias?.days || 0);
  renderLinks();
}

async function loadTeamSharing() {
  const container = document.querySelector<HTMLElement>("#teamVideoPrivacy");
  if (!container) return;
  const fields = sharingFields(container);
  const notice = document.querySelector<HTMLElement>("#teamVideoPrivacyStatus")!;
  const watch = document.querySelector<HTMLInputElement>("#teamVideoWatch")!;
  function render(settings: Sharing) {
    fields.populate(settings);
    watch.value = settings.watch ? address(settings.watch).href : "";
    watch.parentElement!.hidden = !settings.watch;
    notice.textContent = settings.pending ? "Waiting for the configured publication approval." : "";
  }
  try {
    render(await call<Sharing>("/privacy"));
  } catch (error) {
    container.hidden = true;
    notice.textContent = String(error);
    return;
  }
  fields.form.onsubmit = async event => {
    event.preventDefault();
    fields.save.disabled = true;
    try {
      const updated = await call<Sharing>("/privacy", "PUT", fields.values());
      render(updated);
      if (!updated.pending) notice.textContent = "Video sharing saved.";
    } catch (error) {
      notice.textContent = String(error);
    } finally {
      fields.save.disabled = false;
    }
  };
}

async function loadWorkspace() {
  if (teamPreview) {
    editorialState = await call<EditorialStatus>("/editorial-status");
    editorial.value = editorialState.status;
    editorial.disabled = false;
    preview.src = teamPreview;
    preview.hidden = false;
    await loadTeamSharing();
    return;
  }
  const [work, viewerSharing, state, reviews] = await Promise.all([
    call<WorkspaceInfo>(), call<ShareLink>("/share-link"), call<EditorialStatus>("/editorial-status"),
    call<{path: string; enabled: boolean; expires: number}[]>("/review-links"),
  ]);
  info = work;
  sharing = viewerSharing;
  editorialState = state;
  editorial.value = editorialState.status;
  editorial.disabled = false;
  sourcePrivacy.hidden = !info.vimeo_source;
  privacy.hidden = !info.editable_privacy;
  const activeReview = reviews.find((link) => link.enabled && link.expires * 1000 > Date.now());
  copyReview.hidden = reviewLink.parentElement!.hidden = !activeReview;
  reviewLink.value = activeReview ? address(activeReview.path).href : "";
  preview.hidden = !info.preview;
  previewMessage.hidden = Boolean(info.preview);
  if (info.preview) {
    const ticket = await call<{preview: string}>("/ticket", "POST", {sha256: info.preview});
    preview.src = ticket.preview;
  }
  populateCustom();
}

async function load() {
  try {
    show(await call<VideoDetails>("/details"));
    if (!teamPreview) sessionStorage.setItem("cueporchOperator", key.value);
    identify.hidden = true;
    pane.hidden = false;
    status.textContent = "";
    void loadAIDrafts();
    await loadWorkspace();
  } catch (error) {
    status.textContent = String(error);
  }
}

function lines(name: string) {
  return input(name).value.split("\n").map((value) => value.trim()).filter(Boolean);
}

identify.onsubmit = (event) => { event.preventDefault(); void load(); };
form.onsubmit = async (event) => {
  event.preventDefault();
  submit.disabled = true;
  try {
    const body = {
      title: input("title").value,
      description: input("description").value,
      embed_pages: lines("embed_pages"),
      tags: lines("tags"),
      categories: lines("categories"),
      credits: lines("credits").map((value) => {
        const [name, ...role] = value.split("|");
        return {name: name.trim(), role: role.join("|").trim()};
      }),
      license: input("license").value || null,
      rating: rating.value,
      content: new FormData(form).getAll("content"),
      realistic_ai: input("realistic_ai").checked,
      advertisement: input("advertisement").checked,
      panorama: input("panorama").value,
    };
    show(await call<VideoDetails>("/details", "PUT", body));
    status.textContent = "Video details saved.";
  } catch (error) {
    status.textContent = String(error);
  } finally {
    submit.disabled = false;
  }
};

editorial.onchange = async () => {
  editorial.disabled = true;
  try {
    editorialState = await call<EditorialStatus>("/editorial-status", "PUT", {
      status: editorial.value, version: editorialState.version,
    });
    status.textContent = "Editorial status saved.";
  } catch (error) {
    editorial.value = editorialState.status;
    status.textContent = String(error);
  } finally {
    editorial.disabled = false;
  }
};

start.oninput = renderLinks;
capture.onclick = () => { start.value = preview.currentTime.toFixed(3); renderLinks(); };

function copy(button: HTMLButtonElement, field: HTMLInputElement | HTMLTextAreaElement, message: string) {
  button.onclick = async () => {
    try {
      await navigator.clipboard.writeText(field.value);
      sharingMessage.textContent = message;
    } catch {
      field.select();
      sharingMessage.textContent = "Selected for copying.";
    }
  };
}
copy(document.querySelector<HTMLButtonElement>("#copyWatch")!, viewerLink, "Viewer link copied.");
copy(copyEmbed, embedCode, "Embed code copied.");
copy(copyReview, reviewLink, "Review link copied.");

custom.onsubmit = async (event) => {
  event.preventDefault();
  const values = new FormData(custom);
  try {
    sharing = await call<ShareLink>("/share-link", "PUT", {slug: values.get("slug"), days: Number(values.get("days"))});
    populateCustom();
    sharingMessage.textContent = "Custom link saved.";
  } catch (error) {
    sharingMessage.textContent = String(error);
  }
};
clearCustom.onclick = async () => {
  try {
    await call<void>("/share-link", "DELETE");
    sharing = await call<ShareLink>("/share-link");
    populateCustom();
    sharingMessage.textContent = "Custom link removed.";
  } catch (error) {
    sharingMessage.textContent = String(error);
  }
};
if (key.value) void load();
