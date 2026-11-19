import {setupRegistration} from "./video_registration.js";
import {ChatState, messageAction, renderChat} from "./event_chat.js";
import {setupProducerEngagement} from "./event_engagement.js";
import {relaySummary, type RelayHealth} from "./event_health.js";

type Repeat = "none" | "days" | "weeks" | "months";
interface Schedule {
  timezone: string; start: string; end: string; repeat: Repeat; interval: number;
  weekdays: number[]; until: string | null; count: number | null;
}
interface EventSettings {
  title: string; description: string; type: string; visibility: string;
  latency: string; save_replay: boolean; dvr_seconds?: number; schedule: Schedule;
}
interface Occurrence {id: number; start: string; end: string; local: string; delivery: string; program: string | null;}
interface LiveEvent {
  id: string; slug: string; revision: string; editable: boolean; event: EventSettings; publication: string;
  occurrences: Occurrence[]; watch: string | null; embed: string | null;
}
interface Calendar {events: LiveEvent[]; overlaps: string[]; host_ready: boolean;}
interface Encoder {server: string; secure_server?: string; key: string; public: boolean;}
interface EventHealth extends RelayHealth {
  state: string; viewers?: number;
  delivery_settings?: {latency: string | null; dvr_seconds: number | null};
}
interface ProductionCaptions {enabled: boolean; language: string; status: string; cue_count: number;}
interface SrtListener {server: string; passphrase: string; key_bytes: number;}
interface ProgramSource {work: string; cut: string;}
interface ProgramChoice extends ProgramSource {title: string;}
interface EventProgram {
  revision: string | null; state: string; error: string | null; choices: ProgramChoice[];
  sources: ProgramSource[]; editable: boolean;
}

const element = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const input = (id: string) => element<HTMLInputElement>(id);
const button = (id: string) => element<HTMLButtonElement>(id);
const identify = element<HTMLFormElement>("eventsIdentify");
const credential = identify.elements.namedItem("credential") as HTMLInputElement;
const status = element("eventsStatus");
const workspace = element("eventsWorkspace");
const form = element<HTMLFormElement>("newEventForm");
const field = (name: string) => form.elements.namedItem(name) as HTMLInputElement;
const preset = element<HTMLSelectElement>("eventPreset");
const output = element("eventProduction");
const health = element("eventHealth");
const measurements = element("eventHealthDetails");
const captionSettings = element<HTMLDetailsElement>("eventCaptionSettings");
const captionForm = element<HTMLFormElement>("productionCaptions");
const captionEnabled = captionForm.elements.namedItem("enabled") as HTMLInputElement;
const captionLanguage = captionForm.elements.namedItem("language") as HTMLSelectElement;
const captionStatus = element("productionCaptionStatus");
const encoder = element("encoderSettings");
const key = input("encoderKey");
const starting = input("calendarFirst"), ending = input("calendarLast");
const programPanel = element<HTMLDetailsElement>("eventProgramPanel");
const programChoice = element<HTMLSelectElement>("programChoice");
const chatPanel = element<HTMLDetailsElement>("operatorChat");
const chatForm = element<HTMLFormElement>("eventChatSettings");
const chatEnabled = chatForm.elements.namedItem("enabled") as HTMLInputElement;
const chatInterval = chatForm.elements.namedItem("interval") as HTMLInputElement;
const chatMessages = element<HTMLOListElement>("operatorChatMessages");
const chatStatus = element("operatorChatStatus");
const root = "/api/live-events";
const publicBase = document.body.dataset.publicUrl || location.origin;

let calendar: Calendar;
let active: {event: LiveEvent; instance: Occurrence} | undefined;
let editing: LiveEvent | undefined;
let registrationEvent: string | undefined;
let pending = false;
let program: EventProgram | undefined;
let programSources: ProgramSource[] = [];
credential.value = sessionStorage.getItem("cueporchOperator") || "";

async function request<T>(path: string, method = "GET", body?: object): Promise<T> {
  const headers: Record<string, string> = {Authorization: "Bearer " + credential.value};
  if (body) headers["Content-Type"] = "application/json";
  const result = await fetch(path, {method, headers, body: body ? JSON.stringify(body) : undefined});
  if (!result.ok) {
    const detail = new DOMParser().parseFromString(await result.text(), "text/html").querySelector("p")?.textContent;
    throw new Error(detail || `Request failed (${result.status})`);
  }
  return result.status === 204 ? undefined as T : result.json();
}

async function action(work: () => Promise<void>, message = "") {
  if (pending) return;
  pending = true;
  status.textContent = "";
  workspace.inert = true;
  try {
    await work();
    status.textContent = message;
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    pending = false;
    workspace.inert = false;
  }
}

function mediaPath(suffix: string) {
  return `${root}/${active!.event.id}/${active!.instance.id}/${suffix}`;
}

async function downloadCsv(path: string, filename: string) {
  const response = await fetch(path, {headers: {Authorization: "Bearer " + credential.value}});
  if (!response.ok) throw new Error("Could not download the event report.");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(await response.blob());
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

const closeEngagement = setupProducerEngagement(() => mediaPath("engagement"), request, (feature, filename) =>
  action(() => downloadCsv(mediaPath(`engagement/${feature}/export`), filename)),
);

const registrationEditor = element<HTMLFormElement>("registrationEditor");
const openRegistration = setupRegistration(
  (suffix, method, body) => request(root + "/" + registrationEvent + suffix, method, body),
  async () => {
    const response = await fetch(root + "/" + registrationEvent + "/registration/export", {
      headers: {Authorization: "Bearer " + credential.value},
    });
    if (!response.ok) throw new Error("Could not download the registrants.");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(await response.blob());
    link.download = "cueporch-event-registrants.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  },
);

function choose(event: LiveEvent, instance: Occurrence) {
  if (registrationEvent !== event.id) {
    registrationEditor.hidden = true;
    registrationEvent = undefined;
  }
  active = {event, instance};
  closeEngagement();
  chatPanel.open = false;
  chatMessages.replaceChildren();
  delete chatMessages.dataset.revision;
  programPanel.open = false;
  program = undefined;
  programSources = [];
  renderProgram();
  output.hidden = false;
  encoder.hidden = true;
  key.value = "";
  element("srtSettings").hidden = true;
  input("srtPassphrase").value = "";
  health.textContent = instance.delivery === "ready" ? "Encoder is ready." : instance.delivery.replaceAll("_", " ");
  measurements.textContent = "";
  captionSettings.open = false;
  captionStatus.textContent = "";
  button("editEvent").disabled = !event.editable;
  button("prepareEncoder").disabled = !calendar.host_ready || ["withdrawing", "withdrawn", "replay"].includes(instance.delivery);
  button("prepareSrt").disabled = button("prepareEncoder").disabled;
  button("withdrawEvent").textContent = instance.delivery === "scheduled" ? "Cancel this occurrence" : "Withdraw this occurrence";
  button("withdrawEvent").disabled = instance.delivery === "withdrawn" ||
    (instance.delivery !== "scheduled" && !calendar.host_ready);
  element("productionTitle").textContent = `${event.event.title} · ${instance.local.replace("T", " ")} ${event.event.schedule.timezone}`;
  const watch = element<HTMLAnchorElement>("eventWatch");
  const studio = element<HTMLAnchorElement>("eventStudio");
  studio.href = `/operator/live-events/${event.id}/${instance.id}/studio`;
  element("eventShare").hidden = !event.watch;
  element("eventEmbedField").hidden = !event.embed;
  if (event.watch) watch.href = new URL(event.watch + "?occurrence=" + instance.id, publicBase).href;
  const address = event.embed ? new URL(event.embed + "?occurrence=" + instance.id, publicBase).href : "";
  input("eventEmbedCode").value = address ? `<iframe src="${address}" title="Live event" allowfullscreen></iframe>` : "";
}

function readCalendar(value: Calendar) {
  calendar = value;
  element("eventsHost").hidden = value.host_ready;
  preset.replaceChildren(new Option("Start from scratch", ""), ...value.events.map((entry) => new Option(entry.event.title, entry.id)));
  const rows = element<HTMLTableSectionElement>("eventCalendarRows");
  rows.replaceChildren();
  const sorted = value.events.flatMap((event) => event.occurrences.map((instance) => ({event, instance})))
    .sort((a, b) => a.instance.id - b.instance.id);
  for (const {event, instance} of sorted) {
    const row = rows.insertRow();
    row.insertCell().textContent = event.event.title;
    const timestamp = document.createElement("time");
    timestamp.dateTime = instance.start;
    timestamp.textContent = instance.local.replace("T", " ") + " " + event.event.schedule.timezone;
    row.insertCell().append(timestamp);
    row.insertCell().textContent = event.publication === "waiting" ? "Waiting for publication approval" :
      instance.program ? "Program " + instance.program.replaceAll("_", " ") : instance.delivery.replaceAll("_", " ");
    row.insertCell().textContent = value.overlaps.includes(event.id) ? "Overlapping time" : "";
    const control = document.createElement("button");
    control.type = "button";
    control.textContent = "Open production";
    control.setAttribute("aria-label", `Open production for ${event.event.title} at ${instance.local.replace("T", " ")}`);
    control.onclick = () => choose(event, instance);
    row.insertCell().append(control);
    if (active?.event.id === event.id && active.instance.id === instance.id) choose(event, instance);
  }
}

async function load() {
  const query = new URLSearchParams();
  if (starting.value) query.set("from", starting.value);
  if (ending.value) query.set("to", ending.value);
  readCalendar(await request<Calendar>(root + "?" + query));
  sessionStorage.setItem("cueporchOperator", credential.value);
  identify.hidden = true;
  workspace.hidden = false;
}

identify.onsubmit = (event) => { event.preventDefault(); void action(load); };
if (credential.value) void action(load);
button("calendarRefresh").onclick = () => void action(load);
function openEditor(current?: LiveEvent) {
  editing = current;
  form.reset();
  element("eventEditorHeading").textContent = current ? "Edit event" : "New event";
  element("eventReuse").hidden = Boolean(current);
  field("slug").readOnly = Boolean(current);
  const values = current?.event;
  if (values) {
    for (const name of ["title", "description", "type", "visibility", "latency"] as const) field(name).value = values[name];
    field("save_replay").checked = values.save_replay;
    field("dvr_seconds").value = values.dvr_seconds?.toString() ?? "";
    const schedule = values.schedule;
    for (const name of ["timezone", "start", "end", "repeat"] as const) field(name).value = schedule[name];
    field("interval").value = String(schedule.interval);
    field("count").value = schedule.count ? String(schedule.count) : "";
    field("until").value = schedule.until || "";
    for (const weekday of form.querySelectorAll<HTMLInputElement>('input[name="weekday"]')) {
      weekday.checked = schedule.weekdays.includes(Number(weekday.value));
    }
    field("slug").value = current!.slug;
  } else {
    field("timezone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  }
  recurrence();
  form.hidden = false;
  field("title").focus();
}

function recurrence() {
  element("recurringFields").hidden = field("repeat").value === "none";
  element("eventWeekdays").hidden = field("repeat").value !== "weeks";
}

button("newEvent").onclick = () => openEditor();
button("editEvent").onclick = () => openEditor(active?.event);
button("configureEventRegistration").onclick = () => void action(async () => {
  if (active) {
    registrationEvent = active.event.id;
    await openRegistration();
  }
});
button("closeEventEditor").onclick = () => { form.hidden = true; };
field("repeat").onchange = recurrence;
preset.onchange = () => {
  const value = calendar.events.find((entry) => entry.id === preset.value)?.event;
  if (!value) return;
  for (const name of ["type", "description", "latency"] as const) field(name).value = value[name];
  field("save_replay").checked = value.save_replay;
  field("dvr_seconds").value = value.dvr_seconds?.toString() ?? "";
  field("timezone").value = value.schedule.timezone;
};

form.onsubmit = (event) => {
  event.preventDefault();
  void action(async () => {
    const read = new FormData(form);
    const repeat = field("repeat").value as Repeat;
    const schedule: Schedule = {timezone: field("timezone").value, start: field("start").value, end: field("end").value,
      repeat, interval: Number(field("interval").value || 1), weekdays: read.getAll("weekday").map(Number),
      until: repeat !== "none" && field("until").value || null, count: repeat !== "none" && Number(field("count").value) || null};
    const target = editing ? root + "/" + editing.id : root;
    const method = editing ? "PUT" : "POST";
    const created = await request<LiveEvent>(target, method, {
      revision: editing?.revision,
      slug: field("slug").value, title: field("title").value, description: field("description").value,
      type: field("type").value, visibility: field("visibility").value, latency: field("latency").value,
      save_replay: field("save_replay").checked,
      dvr_seconds: field("dvr_seconds").value === "" ? null : field("dvr_seconds").valueAsNumber, schedule,
    });
    form.hidden = true;
    await load();
    if (created.occurrences.length) choose(created, created.occurrences[0]);
  }, "Event saved.");
};

button("prepareEncoder").onclick = () => void action(async () => {
  const connection = await request<Encoder>(mediaPath("encoder"), "POST");
  input("rtmpServer").value = connection.server;
  input("rtmpsServer").value = connection.secure_server || "";
  element("secureEncoder").hidden = !connection.secure_server;
  key.value = connection.key;
  encoder.hidden = false;
  button("withdrawEvent").disabled = false;
  health.textContent = "Waiting for the encoder.";
});
button("copyEncoderKey").onclick = () => void action(async () => {
  await navigator.clipboard.writeText(key.value);
}, "Stream key copied.");
button("prepareSrt").onclick = () => void action(async () => {
  const result = await request<SrtListener>(mediaPath("srt"), "POST", {
    interface: input("srtInterface").value, port: Number(input("srtPort").value),
  });
  input("srtServer").value = result.server;
  input("srtPassphrase").value = result.passphrase;
  element("srtSettings").hidden = false;
  button("withdrawEvent").disabled = false;
}, "SRT listener is ready for the encoder.");
button("stopSrt").onclick = () => void action(async () => {
  await request(mediaPath("srt"), "DELETE");
  element("srtSettings").hidden = true;
  input("srtPassphrase").value = "";
}, "SRT listener stopped.");
button("copySrtPassphrase").onclick = () => void action(async () => {
  await navigator.clipboard.writeText(input("srtPassphrase").value);
}, "SRT passphrase copied.");

function renderProgram() {
  const controls = element<HTMLOListElement>("eventProgramSources");
  controls.replaceChildren();
  const editable = program?.editable ?? false;
  const choices = program?.choices || [];
  for (const [position, source] of programSources.entries()) {
    const item = document.createElement("li");
    const title = choices.find((choice) => choice.work === source.work && choice.cut === source.cut)?.title || "Unavailable video";
    item.append(title + " ");
    for (const [label, step] of [["Move up", -1], ["Move down", 1], ["Remove", 0]] as const) {
      const control = document.createElement("button");
      control.type = "button";
      control.textContent = label;
      control.setAttribute("aria-label", `${label} ${title}`);
      control.disabled = !editable || position + step < 0 || position + step >= programSources.length;
      control.onclick = () => {
        programSources.splice(position, 1);
        if (step) programSources.splice(position + step, 0, source);
        renderProgram();
      };
      item.append(control);
    }
    controls.append(item);
  }
  programChoice.replaceChildren(...choices.map((choice, index) => new Option(choice.title, String(index))));
  programChoice.disabled = !editable || !choices.length;
  button("addProgramVideo").disabled = programChoice.disabled || programSources.length >= 20;
  button("saveEventProgram").disabled = !editable || !programSources.length;
  button("clearEventProgram").disabled = !editable || !program?.revision;
  const state = program?.state.replaceAll("_", " ");
  element("eventProgramStatus").textContent = program ? (program.error || `Program ${state}.`) : "";
}

async function loadProgram() {
  program = await request<EventProgram>(mediaPath("program"));
  programSources = [...program.sources];
  renderProgram();
}

async function writeProgram(sources: ProgramSource[]) {
  program = await request<EventProgram>(mediaPath("program"), "PUT", {revision: program?.revision ?? null, sources});
  programSources = [...program.sources];
  renderProgram();
}

programPanel.ontoggle = () => { if (programPanel.open) void action(loadProgram); };
button("refreshEventProgram").onclick = () => void action(loadProgram);
button("addProgramVideo").onclick = () => {
  const choice = program?.choices[Number(programChoice.value)];
  if (!choice || programSources.length >= 20) return;
  programSources.push({work: choice.work, cut: choice.cut});
  renderProgram();
};
button("saveEventProgram").onclick = () => void action(() => writeProgram(programSources), "Program queued for the scheduled occurrence.");
button("clearEventProgram").onclick = () => void action(() => writeProgram([]), "Manual production selected.");

let chatRequest: Promise<void> | undefined;
async function updateChat(method = "GET", body?: object, fields = false) {
  if (!active || (method === "GET" && !fields && chatRequest)) return;
  const path = mediaPath("chat");
  const previous = chatRequest;
  const pending = previous ? previous.then(() => readChat(path, method, body, fields)) : readChat(path, method, body, fields);
  chatRequest = pending;
  try { await pending; } finally { if (chatRequest === pending) chatRequest = undefined; }
}

async function readChat(path: string, method: string, body: object | undefined, fields: boolean) {
  if (mediaPath("chat") !== path || !chatPanel.open) return;
  try {
    const state = await request<ChatState>(path, method, body);
    if (mediaPath("chat") !== path || !chatPanel.open) return;
    if (fields) {
      chatEnabled.checked = state.enabled;
      chatInterval.value = String(state.interval);
      chatStatus.textContent = method === "PUT" ? "Chat settings saved." : "";
    }
    renderChat(chatMessages, state.messages, (entry) => [
      messageAction("Remove message", entry, () => void updateChat("POST", {type: "remove", id: entry.id})),
      messageAction("Remove and block viewer", entry, () => void updateChat("POST", {type: "ban", id: entry.id})),
    ]);
  } catch (error) {
    chatStatus.textContent = error instanceof Error ? error.message : String(error);
  }
}

chatPanel.ontoggle = async () => {
  if (!chatPanel.open) return;
  chatForm.inert = true;
  try { await updateChat("GET", undefined, true); } finally { chatForm.inert = false; }
};
window.setInterval(() => { if (chatPanel.open && !document.hidden) void updateChat(); }, 3000);
chatForm.onsubmit = (event) => {
  event.preventDefault();
  void updateChat("PUT", {enabled: chatEnabled.checked, interval: Number(chatInterval.value)}, true);
};
button("exportEventChat").onclick = () => void action(() => downloadCsv(mediaPath("chat/export"), "cueporch-event-chat.csv"));

button("readEventHealth").onclick = () => void action(async () => {
  const result = await request<EventHealth>(mediaPath("health"));
  const settings = result.delivery_settings;
  const lines = [result.state.replaceAll("_", " ")];
  if (typeof result.viewers === "number") lines.push(`${result.viewers} viewers`);
  if (settings?.latency) lines.push(`${settings.latency} latency mode`);
  if (typeof settings?.dvr_seconds === "number") lines.push(settings.dvr_seconds ? `DVR up to ${settings.dvr_seconds} s` : "DVR off");
  health.textContent = lines.join(" · ");
  measurements.textContent = relaySummary(result, result.state === "live" ? "Encoder measurements unavailable from this host." : "");
});
async function updateCaptionSettings(update = false) {
  const value = {enabled: captionEnabled.checked, language: captionLanguage.value};
  const result = await request<ProductionCaptions>(mediaPath("captions"), update ? "PUT" : "GET", update ? value : undefined);
  captionEnabled.checked = result.enabled;
  captionLanguage.value = result.language;
  const detail = {
    waiting_for_stream: "Waiting for the event stream",
    engine_unavailable: "Connect a local TextFlowKit streaming runtime",
    listening: "Transcribing the program",
    media_or_engine_unavailable: "Waiting for usable live audio and a local transcription runtime",
    stopped: "Captioning stopped",
  } as Record<string, string>;
  captionStatus.textContent = result.enabled ? `${detail[result.status] || result.status} · ${result.cue_count} lines generated`
    : "Automatic captions are off.";
}
captionSettings.ontoggle = () => { if (captionSettings.open) void action(() => updateCaptionSettings()); };
captionForm.onsubmit = event => { event.preventDefault(); void action(() => updateCaptionSettings(true)); };
button("refreshLiveCaptions").onclick = () => void action(() => updateCaptionSettings());
button("withdrawEvent").onclick = () => void action(async () => {
  encoder.hidden = true;
  key.value = "";
  await request(mediaPath("delivery"), "DELETE");
  await load();
}, "Occurrence withdrawn.");
