export {};

interface EditingSource {
  version: string;
  duration: number;
  title: string;
  preview: string;
  tracks: {language: string}[];
}
interface Cue { start: number; end: number; text: string }

const identity = document.querySelector<HTMLFormElement>("#identify")!;
const key = document.querySelector<HTMLInputElement>("#editKey")!;
const message = document.querySelector<HTMLElement>("#editStatus")!;
const editor = document.querySelector<HTMLElement>("#videoEditor")!;
const video = document.querySelector<HTMLVideoElement>("#editPreview")!;
const time = document.querySelector<HTMLElement>("#trimTime")!;
const timeline = document.querySelector<HTMLInputElement>("#trimPosition")!;
const start = document.querySelector<HTMLInputElement>("#trimStart")!;
const end = document.querySelector<HTMLInputElement>("#trimEnd")!;
const form = document.querySelector<HTMLFormElement>("#trimSelection")!;
const actions = document.querySelector<HTMLFieldSetElement>("#editActions")!;
const transcript = document.querySelector<HTMLElement>("#trimTranscript")!;
const language = document.querySelector<HTMLSelectElement>("#editLanguage")!;
const search = document.querySelector<HTMLInputElement>("#editSearch")!;
const cueList = document.querySelector<HTMLUListElement>("#editCues")!;
const endpoint = "/api/library/work/" + encodeURIComponent(document.body.dataset.work || "");
let source: EditingSource;
let selectionEnd: number | undefined;
let cues: Cue[] = [];
let requestNumber = 0;
key.value = sessionStorage.getItem("cueporchOperator") || "";

async function call(suffix: string, body?: object) {
  const headers: Record<string, string> = {Authorization: "Bearer " + key.value};
  if (body) headers["Content-Type"] = "application/json";
  const response = await fetch(endpoint + suffix, {
    method: body ? "POST" : "GET", headers, body: body && JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
    throw new Error(detail?.textContent || `Request failed (${response.status})`);
  }
  return response;
}

function seek(seconds: number) {
  selectionEnd = undefined;
  video.currentTime = seconds;
}

function choose(field: HTMLInputElement, milliseconds: number) {
  field.value = (Math.min(milliseconds, source.duration) / 1000).toFixed(3);
}

function action(name: string, onClick: () => void) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = name;
  button.onclick = onClick;
  return button;
}

function renderCues() {
  const query = search.value.toLocaleLowerCase();
  const rows = document.createDocumentFragment();
  for (const cue of cues.filter(row => row.text.toLocaleLowerCase().includes(query))) {
    const row = document.createElement("li");
    const text = document.createElement("span");
    text.textContent = cue.text;
    row.append(action(`${cue.start / 1000}s`, () => seek(cue.start / 1000)), text,
      action("Set start", () => choose(start, cue.start)), action("Set end", () => choose(end, cue.end)));
    rows.append(row);
  }
  cueList.replaceChildren(rows);
}

async function loadTranscript() {
  const request = ++requestNumber;
  cues = [];
  renderCues();
  if (!language.value) return;
  try {
    const track: {version: string; cues: Cue[]} = await
      (await call("/languages/" + encodeURIComponent(language.value))).json();
    if (request !== requestNumber) return;
    if (track.version !== source.version) throw new Error("The cut changed. Reload this editor.");
    cues = track.cues;
    renderCues();
  } catch (error) {
    if (request === requestNumber) message.textContent = error instanceof Error ? error.message : String(error);
  }
}

async function load() {
  source = await (await call("/editing")).json();
  const ticket: {preview: string} = await (await call("/ticket", {sha256: source.preview})).json();
  const maximum = (source.duration / 1000).toFixed(3);
  timeline.max = start.max = end.max = maximum;
  timeline.value = "0";
  start.value = "0";
  end.value = maximum;
  selectionEnd = undefined;
  video.src = ticket.preview;
  document.querySelector("#editorTitle")!.textContent = "Edit " + source.title;
  language.replaceChildren(...source.tracks.map(track => new Option(track.language, track.language)));
  transcript.hidden = source.tracks.length === 0;
  await loadTranscript();
  sessionStorage.setItem("cueporchOperator", key.value);
  identity.hidden = true;
  editor.hidden = false;
}

function open() {
  message.textContent = "";
  void load().catch(error => { message.textContent = String(error.message || error); });
}

identity.onsubmit = (event) => { event.preventDefault(); open(); };
language.onchange = () => void loadTranscript();
search.oninput = renderCues;
timeline.oninput = () => seek(timeline.valueAsNumber);
video.ontimeupdate = () => {
  if (selectionEnd !== undefined && video.currentTime >= selectionEnd) {
    video.pause();
    video.currentTime = selectionEnd;
    selectionEnd = undefined;
  }
  timeline.value = String(video.currentTime);
  time.textContent = `${video.currentTime.toFixed(3)} s`;
};
document.querySelector<HTMLButtonElement>("#timeToStart")!.onclick = () => choose(start, Math.round(video.currentTime * 1000));
document.querySelector<HTMLButtonElement>("#timeToEnd")!.onclick = () => choose(end, Math.round(video.currentTime * 1000));
document.querySelector<HTMLButtonElement>("#playSelection")!.onclick = async () => {
  if (!form.reportValidity() || end.valueAsNumber <= start.valueAsNumber) {
    message.textContent = "Choose the start and end of the selection.";
    return;
  }
  seek(start.valueAsNumber);
  selectionEnd = end.valueAsNumber;
  try {
    await video.play();
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : String(error);
  }
};

form.onsubmit = async (event) => {
  event.preventDefault();
  const mode = (event.submitter as HTMLButtonElement | null)?.value || "trim";
  const body = {version: source.version, start: Math.round(start.valueAsNumber * 1000),
    end: Math.round(end.valueAsNumber * 1000)};
  actions.disabled = true;
  message.textContent = mode === "gif" ? "Making GIF…" : "Rendering new version…";
  try {
    const response = await call("/editing/" + mode, body);
    if (mode === "gif") {
      const object = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = object;
      link.download = "cueporch-excerpt.gif";
      link.click();
      setTimeout(() => URL.revokeObjectURL(object), 1000);
      message.textContent = "GIF downloaded.";
    } else {
      await load();
      message.textContent = "New version saved.";
    }
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    actions.disabled = false;
  }
};
if (key.value) open();
