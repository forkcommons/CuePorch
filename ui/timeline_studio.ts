import { mountTimelineTemplates, type TemplateItem } from "./timeline_templates.js";
import { uploadLibraryFile } from "./library_upload.js";
import { privateSettings } from "./library_privacy.js";
import type { BrandKit, BrandKits } from "./brand_kits.js";
import {showTimelineCard, type CardClip} from "./timeline_templates.js";
import { createTimelineGraphicPreview, createTimelineOverlayPreview, mountTimelineGraphics, type TimelineGraphic, type GraphicAsset } from "./timeline_graphics.js";
import { mountTimelineStickers, type TimelineSticker } from "./timeline_stickers.js";
import { mountTimelineStock, type StockRights } from "./timeline_stock.js";
import { mountTimelineScale } from "./timeline_scale.js";
import type { TimelineSegment } from "./timeline_scale_layout.js";
import { mountTimelineText, type TextLayer } from "./timeline_text.js";

interface MediaClip { work: string; cut: string; start: number; end: number }
type Clip = (MediaClip | CardClip) & {graphic?: TimelineGraphic; sticker?: TimelineSticker; text_layers?: TextLayer[]};
function isMedia(clip: Clip | undefined): clip is MediaClip & {graphic?: TimelineGraphic; sticker?: TimelineSticker} {
  return clip !== undefined && "work" in clip;
}
interface Music { work: string; cut?: string; start: number; gain: number }
interface Settings { ratio: string; color: string; clips: Clip[]; music: Music | null; brand?: BrandKit | null }
interface Job { id: string; state: "queued" | "rendering" | "saved" | "failed"; result: string | null; error: string | null }
interface Project { revision: string | null; current: string; draft: boolean; settings: Settings; job?: Job | null; standalone?: boolean; title?: string }
interface Media { id: string; title: string; asset_type?: string; source_cut?: string | null; folder: string | null; stock_asset?: StockRights | null }
interface Cue { start: number; end: number; text: string }

const node = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const button = (id: string) => node<HTMLButtonElement>(id);
const input = (id: string) => node<HTMLInputElement>(id);
const select = (id: string) => node<HTMLSelectElement>(id);
const identity = node<HTMLFormElement>("timelineIdentity");
const key = input("timelineKey");
const status = node("timelineStatus");
const heading = node<HTMLHeadingElement>("timelineEditorHeading");
const detailsLink = node<HTMLAnchorElement>("videoDetailsLink");
const trimLink = node<HTMLAnchorElement>("trimGifLink");
const libraryLink = node<HTMLAnchorElement>("libraryNavLink");
const video = node<HTMLVideoElement>("timelinePreview");
const trackList = node<HTMLOListElement>("timelineClips");
const recorderDialog = node<HTMLDialogElement>("timelineRecordDialog");
const recorder = node<HTMLIFrameElement>("timelineRecorder");
const fileInput = input("timelineFile");
const uid = document.body.dataset.work || "";
const endpoint = (work = uid) => "/api/library/work/" + encodeURIComponent(work);

const undo: Settings[] = [];
const redo: Settings[] = [];
let project: Project;
let state: Settings;
let media = new Map<string, Media>();
let selected = -1;
let poll: number | undefined;
let debounce: number | undefined;
let generation = 0;
let confirmed = 0;
let writing: Promise<void> | null = null;
let running: Job | null = null;
let previewVersion = 0;
let cueVersion = 0;
let stopTime: number | undefined;
let cues: Cue[] = [];
let uploading = false;
let uploadId = "";
let kits: BrandKit[] = [];
let graphicControls: ReturnType<typeof mountTimelineGraphics> | undefined;
const paintGraphic = createTimelineGraphicPreview();
const paintSticker = createTimelineOverlayPreview<TimelineSticker>("sticker");
const stickerControls = mountTimelineStickers(setSticker);
const textControls = mountTimelineText(setTextLayers);
const paintText = createTimelineOverlayPreview<TextLayer[]>("text");
const timelineScale = mountTimelineScale((index) => run(() => inspect(index)));
key.value = sessionStorage.getItem("cueporchOperator") || "";

button("replayTimelineText").onclick = () => {
  const layers = state?.clips[selected]?.text_layers;
  if (!layers?.length) return;
  paintText(null, state.ratio, state.color, key.value);
  paintText(layers, state.ratio, state.color, key.value);
};

function message(error: unknown) {
  status.textContent = error instanceof Error ? error.message : String(error);
}

function run(fn: () => Promise<void>) {
  void fn().catch(message);
}

async function call<T>(url: string, method = "GET", payload?: object) {
  const headers: Record<string, string> = {Authorization: "Bearer " + key.value};
  if (payload) headers["Content-Type"] = "application/json";
  const response = await fetch(url, {method, headers, body: payload && JSON.stringify(payload)});
  if (!response.ok) {
    const error = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
    throw new Error(error?.textContent || `Timeline request failed (${response.status}).`);
  }
  return await response.json() as T;
}

const seconds = (value: number) => (value / 1000).toFixed(3).replace(/\.?0+$/, "");
const millis = (field: HTMLInputElement) => Math.round(field.valueAsNumber * 1000);
const duration = (rows: Clip[]) => rows.reduce((total, row) => total + row.end - row.start, 0);
const working = () => running?.state === "queued" || running?.state === "rendering";
const nameOf = (work: string) => media.get(work)?.title || work;

function controls() {
  button("undoTimeline").disabled = !undo.length;
  button("redoTimeline").disabled = !redo.length;
  button("renderTimeline").disabled = !state.clips.length || working();
  button("splitTimeline").disabled = !isMedia(state.clips[selected]) || video.hidden;
  button("playTimelineClip").disabled = button("splitTimeline").disabled;
  button("discardTimeline").disabled = Boolean(working());
  const full = state.clips.length >= 128;
  button("appendTimeline").disabled = !select("timelineVideo").value || full;
  button("recordTimeline").disabled = full;
  button("uploadTimeline").disabled = uploading || full;
  button("applyTimelineKit").disabled = !select("timelineBrandKit").value;
  fileInput.disabled = uploading;
}

function listSources() {
  const filter = input("timelineSearch").value.trim().toLocaleLowerCase();
  const picker = select("timelineVideo");
  const last = picker.value;
  picker.replaceChildren();
  for (const item of media.values()) {
    if ((item.asset_type || "video") === "video" && item.title.toLocaleLowerCase().includes(filter)) {
      picker.add(new Option(item.title, item.id));
    }
  }
  if (Array.from(picker.options).some(option => option.value === last)) picker.value = last;
  controls();
}

function action(label: string, onClick: () => void) {
  const result = document.createElement("button");
  result.type = "button";
  result.textContent = label;
  result.onclick = onClick;
  return result;
}

function timeInput(label: string, value: number, change: (milliseconds: number) => void) {
  const group = document.createElement("label");
  const field = document.createElement("input");
  field.type = "number";
  field.min = "0";
  field.step = ".001";
  field.value = seconds(value);
  field.onchange = () => change(millis(field));
  group.append(label, field);
  return group;
}

function textInput(label: string, value: string, maxLength: number, change: (text: string) => void, required = true) {
  const group = document.createElement("label");
  const field = document.createElement("input");
  field.type = "text";
  field.maxLength = maxLength;
  field.value = value;
  field.onchange = () => {
    const next = field.value.trim();
    if ((required && !next) || next.length > maxLength) {
      paint();
      message(`${label} is invalid.`);
      return;
    }
    change(next);
  };
  group.append(label, field);
  return group;
}

function updateCard(index: number, patch: Partial<CardClip>) {
  const clips = state.clips.map((clip, i) =>
    i === index && !isMedia(clip) ? {...clip, ...patch} : clip
  );
  change({...state, clips});
}

function cardDuration(index: number, milliseconds: number) {
  const clip = state.clips[index];
  if (!clip || isMedia(clip) || !Number.isFinite(milliseconds) || milliseconds < 1000 || milliseconds > 15000) {
    paint();
    message("Choose between 1 and 15 seconds.");
    return;
  }
  updateCard(index, {end: milliseconds});
}

function paint() {
  select("timelineRatio").value = state.ratio;
  input("timelineColor").value = "#" + state.color;
  select("timelineMusic").value = state.music?.work || "";
  input("timelineMusicStart").value = seconds(state.music?.start || 0);
  input("timelineMusicGain").value = String(state.music?.gain ?? 25);
  const bookends = [state.brand?.intro, state.brand?.outro].filter(clip => clip != null);
  node("timelineDuration").textContent = seconds(duration(state.clips) + duration(bookends)) + " seconds";
  const segments: TimelineSegment[] = state.clips.map((clip, index) => ({
    index,
    title: isMedia(clip) ? nameOf(clip.work) : clip.card.title,
    duration: clip.end - clip.start,
  }));
  if (state.brand?.intro) segments.unshift({ index: null, title: "Brand intro: " + nameOf(state.brand.intro.work), duration: state.brand.intro.end - state.brand.intro.start });
  if (state.brand?.outro) segments.push({ index: null, title: "Brand outro: " + nameOf(state.brand.outro.work), duration: state.brand.outro.end - state.brand.outro.start });
  timelineScale.show(segments, selected);
  updateTimelinePlayhead();
  const brandSummary = node("timelineBrandSources");
  brandSummary.hidden = !state.brand;
  if (state.brand) {
    const {name, intro, outro} = state.brand;
    brandSummary.textContent = [name, intro && "Intro: " + nameOf(intro.work), outro && "Outro: " + nameOf(outro.work)]
      .filter(Boolean).join(" · ");
  } else brandSummary.textContent = "";
  button("clearTimelineKit").hidden = !state.brand;
  trackList.replaceChildren();
  for (const [index, clip] of state.clips.entries()) {
    const row = document.createElement("li");
    row.className = "timeline-clip";
    const choose = action(isMedia(clip) ? nameOf(clip.work) : clip.card.title, () => run(() => inspect(index)));
    choose.setAttribute("aria-pressed", String(index === selected));
    const earlier = action("Earlier", () => move(index, -1));
    earlier.disabled = index === 0;
    const later = action("Later", () => move(index, 1));
    later.disabled = index === state.clips.length - 1;
    row.append(choose);
    if (isMedia(clip)) {
      row.append(
        timeInput("Start (seconds)", clip.start, start => range(index, start, clip.end)),
        timeInput("End (seconds)", clip.end, end => range(index, clip.start, end)),
      );
    } else {
      row.append(
        textInput("Scene title", clip.card.title, 80, title => updateCard(index, {card: {...clip.card, title}})),
        textInput("Scene subtitle", clip.card.subtitle, 120, subtitle => updateCard(index, {card: {...clip.card, subtitle}}), false),
        timeInput("Duration (seconds)", clip.end, milliseconds => cardDuration(index, milliseconds)),
      );
    }
    row.append(earlier, later, action("Remove", () => remove(index)));
    trackList.append(row);
  }
  const cardPreview = node<HTMLElement>("timelineCardPreview");
  const selectedClip = state.clips[selected];
  graphicControls?.show(selectedClip?.graphic ?? null, Boolean(selectedClip));
  paintGraphic(selectedClip?.graphic ?? null, state.ratio, state.color, key.value);
  stickerControls.show(selectedClip?.sticker ?? null, Boolean(selectedClip), [...media.values()]);
  paintSticker(selectedClip?.sticker ?? null, state.ratio, state.color, key.value);
  textControls.show(selectedClip?.text_layers ?? [], Boolean(selectedClip));
  paintText(selectedClip?.text_layers?.length ? selectedClip.text_layers : null, state.ratio, state.color, key.value);
  button("replayTimelineText").disabled = !selectedClip?.text_layers?.length;
  if (selectedClip && !isMedia(selectedClip)) {
    cardPreview.hidden = false;
    showTimelineCard(cardPreview, selectedClip, state.ratio);
  } else {
    cardPreview.hidden = true;
    cardPreview.replaceChildren();
  }
  controls();
}

async function save(force = false) {
  if (debounce !== undefined) window.clearTimeout(debounce);
  debounce = undefined;
  if (writing) await writing;
  if (confirmed === generation && (!force || project.revision)) return;
  const pending = generation;
  const current = project.current;
  const write = (async () => {
    const updated = await call<Project>(endpoint() + "/timeline", "PUT", {
      revision: project.revision, base: current, settings: state,
    });
    project = updated;
    confirmed = pending;
    if (!working()) status.textContent = "Draft saved.";
  })();
  writing = write;
  try { await write; }
  finally { if (writing === write) writing = null; }
  if (confirmed !== generation) schedule();
}

function schedule() {
  if (debounce !== undefined) window.clearTimeout(debounce);
  debounce = window.setTimeout(() => run(save), 350);
}

function change(next: Settings) {
  undo.push(state);
  redo.length = 0;
  state = next;
  generation++;
  paint();
  schedule();
}

function history(source: Settings[], target: Settings[]) {
  const prior = source.pop();
  if (!prior) return;
  target.push(state);
  state = prior;
  selected = -1;
  video.hidden = true;
  node("timelineTranscript").hidden = true;
  generation++;
  paint();
  schedule();
}

function remove(index: number) {
  selected = -1;
  video.hidden = true;
  node("timelineTranscript").hidden = true;
  change({...state, clips: state.clips.filter((_, value) => value !== index)});
}

function move(index: number, by: number) {
  const clips = [...state.clips];
  const next = index + by;
  [clips[index], clips[next]] = [clips[next], clips[index]];
  if (selected === index) selected = next;
  else if (selected === next) selected = index;
  change({...state, clips});
}

function range(index: number, start: number, end: number) {
  const clip = state.clips[index];
  if (!isMedia(clip)) return;
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end - start < 100) {
    paint();
    message("Choose at least 0.1 seconds within the source.");
    return;
  }
  const clips = state.clips.map((clip, i) => i === index && isMedia(clip) ? {...clip, start, end} : clip);
  change({...state, clips});
}

function seek(position: number) {
  stopTime = undefined;
  video.currentTime = position / 1000;
}

function split(at: number) {
  const clip = state.clips[selected];
  if (!isMedia(clip) || at - clip.start < 100 || clip.end - at < 100) {
    message("Choose a point inside the selected clip.");
    return;
  }
  const clips = [...state.clips];
  clips.splice(selected, 1, {...clip, end: at}, {...clip, start: at});
  change({...state, clips});
}

function omitCue(cue: Cue) {
  const clip = state.clips[selected];
  if (!isMedia(clip)) return;
  const first = Math.max(cue.start, clip.start), last = Math.min(cue.end, clip.end);
  if (first >= last) return;
  const retained = [
    {...clip, end: first}, {...clip, start: last},
  ].filter(part => part.end - part.start >= 100);
  const clips = [...state.clips];
  clips.splice(selected, 1, ...retained);
  if (!retained.length) {
    selected = -1;
    video.hidden = true;
    node("timelineTranscript").hidden = true;
  }
  change({...state, clips});
}

function paintCues() {
  const query = input("timelineTextSearch").value.trim().toLocaleLowerCase();
  const list = node("timelineCues");
  list.replaceChildren();
  for (const cue of cues.filter(cue => cue.text.toLocaleLowerCase().includes(query))) {
    const row = document.createElement("li");
    const text = document.createElement("span");
    text.textContent = cue.text;
    row.append(action(seconds(cue.start) + "s", () => seek(cue.start)), text,
      action("Split before", () => split(cue.start)), action("Remove from video", () => omitCue(cue)));
    list.append(row);
  }
}

async function transcript() {
  const clip = state.clips[selected];
  const language = select("timelineSubtitle").value;
  const version = ++cueVersion;
  if (!isMedia(clip) || !language) return;
  const query = new URLSearchParams({cut: clip.cut, language});
  const response = await call<{cues: Cue[]}>(endpoint(clip.work) + "/timeline/transcript?" + query);
  if (version !== cueVersion) return;
  cues = response.cues;
  paintCues();
}

async function inspect(index: number) {
  selected = index;
  video.pause();
  video.hidden = true;
  video.removeAttribute("src");
  video.load();
  video.onloadedmetadata = null;
  stopTime = undefined;
  node("timelineTranscript").hidden = true;
  paint();
  const clip = state.clips[index];
  const version = ++previewVersion;
  ++cueVersion;
  if (!isMedia(clip)) {
    node("timelineSourceName").textContent = clip?.card.title ?? "";
    cues = [];
    paintCues();
    return;
  }
  node("timelineSourceName").textContent = nameOf(clip.work);
  const query = new URLSearchParams({cut: clip.cut});
  const [preview, subtitles] = await Promise.all([
    call<{preview: string}>(endpoint(clip.work) + "/timeline/preview", "POST", {cut: clip.cut}),
    call<{languages: string[]}>(endpoint(clip.work) + "/timeline/transcript?" + query),
  ]);
  if (version !== previewVersion || state.clips[selected] !== clip) return;
  video.src = preview.preview;
  video.onloadedmetadata = () => seek(clip.start);
  video.hidden = false;
  const language = select("timelineSubtitle");
  language.replaceChildren(...subtitles.languages.map(value => new Option(value, value)));
  node("timelineTranscript").hidden = !language.value;
  controls();
  await transcript();
}

function applyProjectChrome(standalone: boolean, title?: string) {
  const renderButton = button("renderTimeline");

  detailsLink.hidden = trimLink.hidden = standalone;
  libraryLink.hidden = !standalone;
  renderButton.textContent = standalone ? "Render video" : "Render as a new version";
  if (title) heading.textContent = title;
}
function showJob(job: Job, fromReload = false) {
  running = job;
  controls();
  if (job.state === "saved") {
    if (job.result && !fromReload) project.current = job.result;
    if (project.standalone) {
      project.standalone = false;
      applyProjectChrome(false);
    }
    status.textContent = "New version saved.";
    return;
  }
  if (job.state === "failed") {
    message(job.error || "The render stopped.");
    return;
  }
  status.textContent = job.state === "queued" ? "Render queued." : "Rendering the timeline…";
  if (poll !== undefined) window.clearTimeout(poll);
  poll = window.setTimeout(() => run(async () => {
    showJob(await call<Job>(endpoint() + "/timeline/render/" + job.id));
  }), 900);
}

button("renderTimeline").onclick = () => run(async () => {
  await save(true);
  showJob(await call<Job>(endpoint() + "/timeline/render", "POST", {revision: project.revision}));
});
button("saveTimeline").onclick = () => run(() => save(true));
button("undoTimeline").onclick = () => history(undo, redo);
button("redoTimeline").onclick = () => history(redo, undo);
button("discardTimeline").onclick = () => run(async () => {
  if (writing) await writing;
  if (debounce !== undefined) window.clearTimeout(debounce);
  project = await call<Project>(endpoint() + "/timeline", "DELETE", {revision: project.revision});
  state = project.settings;
  selected = -1;
  video.pause();
  video.hidden = true;
  video.removeAttribute("src");
  video.load();
  video.onloadedmetadata = null;
  node("timelineTranscript").hidden = true;
  confirmed = ++generation;
  ++cueVersion;
  cues = [];
  undo.length = redo.length = 0;
  status.textContent = "Draft reset.";
  paint();
});
async function appendClip(work: string) {
  const info = await call<{version: string; duration: number}>(endpoint(work) + "/editing");
  if (state.clips.length >= 128) throw new Error("The timeline has reached its 128-clip limit.");
  change({...state, clips: [...state.clips, {work, cut: info.version, start: 0, end: info.duration}]});
}

async function addNewClip(work: string) {
  const items = await call<Media[]>("/api/library");
  const saved = items.find(item => item.id === work && item.asset_type === "video");
  if (!saved) throw new Error("The saved video is unavailable in this Library.");
  media.set(work, saved);
  listSources();
  if (!state.clips.some(clip => isMedia(clip) && clip.work === work)) await appendClip(work);
  await save(true);
}

button("appendTimeline").onclick = () => run(() => appendClip(select("timelineVideo").value));
fileInput.onchange = () => { uploadId = ""; };
node<HTMLFormElement>("timelineUpload").onsubmit = event => {
  event.preventDefault();
  const file = fileInput.files?.[0];
  if (!file || uploading || state.clips.length >= 128) return;
  if (!/\.(mp4|mov|webm|mkv)$/i.test(file.name)) {
    message("Choose an MP4, MOV, WebM or MKV video.");
    return;
  }
  run(async () => {
    uploadId ||= crypto.randomUUID();
    uploading = true;
    controls();
    try {
      const receipt = await uploadLibraryFile(file, uploadId, media.get(uid)?.folder || "", key.value,
        privateSettings, percent => {
          status.textContent = percent === null ? "Preparing the uploaded clip…" : `Uploading ${Math.floor(percent)}%…`;
        });
      await addNewClip(receipt.id);
      fileInput.value = uploadId = "";
    } finally {
      uploading = false;
      controls();
    }
  });
};

button("recordTimeline").onclick = () => {
  const query = new URLSearchParams({timeline: "1", folder: media.get(uid)?.folder || ""});
  recorder.src = "/operator/record?" + query;
  recorderDialog.showModal();
};
button("closeTimelineRecorder").onclick = () => recorderDialog.close();
recorderDialog.onclose = () => {
  if (!recorderDialog.open) recorder.removeAttribute("src");
};
window.addEventListener("message", event => {
  if (event.origin !== location.origin || event.source !== recorder.contentWindow || !recorderDialog.open ||
      event.data?.type !== "cueporch-recording-saved" || typeof event.data.work !== "string") return;
  recorderDialog.close();
  run(() => addNewClip(event.data.work));
});
button("setTimelineMusic").onclick = () => {
  const work = select("timelineMusic").value;
  const start = millis(input("timelineMusicStart")), gain = input("timelineMusicGain").valueAsNumber;
  change({...state, music: work ? {work, start, gain} : null});
};
button("splitTimeline").onclick = () => split(Math.round(video.currentTime * 1000));
button("playTimelineClip").onclick = () => run(async () => {
  const clip = state.clips[selected];
  if (!isMedia(clip)) return;
  seek(clip.start);
  stopTime = clip.end / 1000;
  await video.play();
});
function updateTimelinePlayhead() {
  const clip = state?.clips[selected];
  const elapsed = clip && !video.hidden && Number.isFinite(video.currentTime)
    ? video.currentTime * 1000 - clip.start
    : 0;
  timelineScale.position(selected, elapsed);
}
video.ontimeupdate = () => {
  if (stopTime !== undefined && video.currentTime >= stopTime) {
    video.pause();
    stopTime = undefined;
  }
  updateTimelinePlayhead();
};
input("timelineSearch").oninput = listSources;
input("timelineTextSearch").oninput = paintCues;
select("timelineSubtitle").onchange = () => run(transcript);
select("timelineRatio").onchange = () => change({...state, ratio: select("timelineRatio").value});
input("timelineColor").onchange = () => change({...state, color: input("timelineColor").value.slice(1)});
select("timelineBrandKit").onchange = controls;
button("applyTimelineKit").onclick = () => {
  const kit = kits.find(item => item.id === select("timelineBrandKit").value);
  if (kit) change({...state, brand: kit, color: kit.style.background.slice(1)});
};
button("clearTimelineKit").onclick = () => change({...state, brand: null});

async function loadTimelineTemplates() {
  const description = node("timelineTemplateDescription");
  try {
    const {templates: items} = await call<{templates: TemplateItem[]}>("/api/library/timeline-templates");
    mountTimelineTemplates(items, apply, insert);
  } catch (error) {
    description.textContent = error instanceof Error ? error.message : String(error);
  }

  function apply(opening: Clip, closing: Clip, ratio: string) {
    if (state.clips.length + 2 > 128) {
      message("Remove enough clips before using both template scenes.");
      return;
    }

    const clips = [opening, ...state.clips, closing];
    selected = -1;
    video.pause();
    video.hidden = true;
    node("timelineTranscript").hidden = true;
    previewVersion += 1;
    cueVersion += 1;
    change({...state, ratio, clips});
  }

  function insert(clip: Clip) {
    if (state.clips.length >= 128) {
      message("Timeline supports at most 128 clips. Remove a clip before inserting another.");
      return;
    }

    const clips = [...state.clips];
    const newPosition = selected >= 0 ? selected + 1 : clips.length;
    clips.splice(newPosition, 0, clip);
    change({...state, clips});
    run(() => inspect(newPosition));
  }
}

async function loadTimelineGraphics() {
  try {
    const {assets} = await call<{assets: GraphicAsset[]}>("/api/library/timeline-graphics");
    graphicControls = mountTimelineGraphics(assets, graphic => {
      const clip = state.clips[selected];
      if (!clip) return;
      let updated: Clip;
      if (graphic) {
        updated = {...clip, graphic};
      } else {
        const {graphic: _removed, ...rest} = clip;
        updated = rest as Clip;
      }
      change({...state, clips: state.clips.map((item, index) => index === selected ? updated : item)});
    });
    paint();
  } catch (error) {
    node("timelineGraphicStatus").textContent = error instanceof Error ? error.message : String(error);
  }
}

function setSticker(sticker: TimelineSticker | null) {
  const clip = state.clips[selected];
  if (!clip) throw new Error("Select a clip before applying a sticker.");
  let updated: Clip;
  if (sticker) {
    updated = {...clip, sticker};
  } else {
    const {sticker: _removed, ...rest} = clip;
    updated = rest as Clip;
  }
  change({...state, clips: state.clips.map((item, index) => index === selected ? updated : item)});
}

function setTextLayers(layers: TextLayer[]) {
  const clips = state.clips.map((clip, index) => {
    if (index !== selected) return clip;
    const {text_layers: _removed, ...rest} = clip;
    return layers.length ? {...rest, text_layers: layers} : rest;
  });
  change({...state, clips});
}

async function open() {
  const [items, loaded, shelf] = await Promise.all([
    call<Media[]>("/api/library"), call<Project>(endpoint() + "/timeline"), call<BrandKits>("/api/library/brand-kits"),
  ]);
  kits = shelf.kits;
  select("timelineBrandKit").replaceChildren(new Option("Choose a brand kit", ""),
    ...kits.map(kit => new Option(kit.name, kit.id)));
  media = new Map(items.map(item => [item.id, item]));
  project = loaded;
  applyProjectChrome(Boolean(project.standalone), project.title);
  state = project.settings;
  const music = select("timelineMusic");
  for (const item of items) if (item.asset_type === "audio") music.add(new Option(item.title, item.id));
  listSources();
  paint();
  sessionStorage.setItem("cueporchOperator", key.value);
  identity.hidden = true;
  node("timelineStudio").hidden = false;
  node("timelineRecovery").hidden = !project.draft;
  if (project.job) showJob(project.job, true);
  void loadTimelineTemplates();
  void loadTimelineGraphics();
  void mountTimelineStock({
    items: () => [...media.values()],
    credential: () => key.value,
    folder: () => "",
    refresh: async () => {
      const items = await call<Media[]>("/api/library");
      media = new Map(items.map(item => [item.id, item]));
      listSources();
      select("timelineMusic").replaceChildren(
        new Option("None", ""),
        ...items.filter(item => item.asset_type === "audio").map(item => new Option(item.title, item.id)),
      );
      paint();
    },
    use: async item => {
      if (item.asset_type === "image" && item.source_cut) {
        setSticker({ work: item.id, cut: item.source_cut, position: "bottom-right", size: 30 });
      } else if (item.asset_type === "audio") {
        change({ ...state, music: { work: item.id, start: 0, gain: 25 } });
      } else {
        await appendClip(item.id);
      }
    },
  });
}
identity.onsubmit = event => { event.preventDefault(); run(open); };
if (key.value) run(open);
