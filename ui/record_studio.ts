import {drawRecording, drawTileGrid, mediaPreview, recordingMime, recordingSize, type Composition} from "./recording_composition.js";
import {openRecordingStorage, type RecordingDraft} from "./recording_draft.js";
import {uploadLibraryFile} from "./library_upload.js";
import {BrowserBroadcast} from "./event_broadcast.js";
import {setupSceneDeck} from "./event_scenes.js";
import {relaySummary, type RelayHealth} from "./event_health.js";
import {StudioAudio, type StudioInput} from "./studio_audio.js";
import {setupStudioEngagement} from "./studio_engagement.js";
import {setupSpeakers} from "./studio_speakers.js";

const element = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const input = (id: string) => element<HTMLInputElement>(id);
const select = (id: string) => element<HTMLSelectElement>(id);
const button = (id: string) => element<HTMLButtonElement>(id);
const status = element("recordStatus");
const identity = element<HTMLFormElement>("recordIdentity");
const key = input("recordKey");
const canvas = element<HTMLCanvasElement>("recordCanvas");
const context = canvas.getContext("2d", {alpha: false})!;
const audioPanel = element<HTMLDetailsElement>("studioAudio");
const film = element<HTMLVideoElement>("takePreview");
const title = input("recordTitle");
const folder = select("recordFolder");
const script = element<HTMLTextAreaElement>("recordScript");
const prompter = element("recordPrompter");
const liveText = element("recordTime");
const draftList = element("recordDrafts");
const canvasStream = () => canvas.captureStream(30);
const broadcastEndpoint = document.body.dataset.eventBroadcast || "";
const healthEndpoint = document.body.dataset.eventHealth;
const relayHealth = document.querySelector<HTMLElement>("#studioRelayHealth");
const mode = document.querySelector<HTMLSelectElement>("#browserProductionMode");
const sceneEndpoint = document.body.dataset.sceneEndpoint;
const scenes = sceneEndpoint ? setupSceneDeck(sceneEndpoint, () => key.value, sceneChanged, selectClipAudio) : null;
const speakerEndpoint = document.body.dataset.speakerEndpoint;
const speakers = speakerEndpoint ? setupSpeakers(speakerEndpoint, () => key.value, () => {wireAudio(); updateControls();}) : null;
const engagementEndpoint = document.body.dataset.engagementEndpoint;
const engagement = engagementEndpoint ? setupStudioEngagement(engagementEndpoint, () => key.value,
  () => scenes?.stage() ?? {id: ""}) : null;

interface Folder { id: string; parent: string | null; name: string }
interface Source { stream: MediaStream; preview: HTMLVideoElement | null }
interface Session {
  recorder: MediaRecorder;
  output: MediaStream;
  draft: RecordingDraft;
  parts: Blob[];
  sequence: number;
  started: number;
  paused: number;
  pausedTotal: number;
  store: boolean;
  persistence: Promise<void>;
  storageError: boolean;
  broadcast: BrowserBroadcast | null;
  broadcastIssue: boolean;
}
interface Take { draft: RecordingDraft; data: Blob; url: string }

let camera: Source | null = null;
let screen: Source | null = null;
let active: Session | null = null;
let take: Take | null = null;
let storage: Awaited<ReturnType<typeof openRecordingStorage>> | null = null;
let audio: StudioAudio | null = null;
let studioClip: HTMLVideoElement | undefined;
let busy = false;
let pictureStream: MediaStream | null = null;
let lastPaint = performance.now();
let scrollTop = 0;
key.value = sessionStorage.getItem("cueporchOperator") || "";
title.value = "Recording " + new Date().toLocaleDateString();

function message(error: unknown) {
  status.textContent = error instanceof Error ? error.message : String(error);
}

function hasSource(source: Source | null) {
  return source?.stream.getTracks().some(track => track.readyState === "live");
}

function ready() {
  return Boolean(hasSource(screen) || hasSource(camera) || scenes?.ready() || speakers?.activeFeeds().length);
}

function closeSource(source: Source | null) {
  source?.stream.getTracks().forEach(track => track.stop());
  if (source?.preview) source.preview.srcObject = null;
}

function updateControls() {
  const running = Boolean(active);
  button("beginRecord").disabled = busy || running || !ready();
  const live = mode?.value === "broadcast";
  button("beginRecord").textContent = live ? "Go live" : "Start recording";
  button("pauseRecord").hidden = !running || Boolean(active?.broadcast);
  button("endRecord").hidden = !running;
  if (mode) mode.disabled = running || busy;
  if (relayHealth && !active?.broadcast) relayHealth.hidden = true;
  button("pauseRecord").disabled = button("endRecord").disabled = busy;
  button("pauseRecord").textContent = active?.recorder.state === "paused" ? "Resume" : "Pause";
  element<HTMLFieldSetElement>("recordFormat").disabled = running || busy;
  element<HTMLFieldSetElement>("recordSources").disabled = running || busy;
  title.disabled = folder.disabled = running || busy;
  button("releaseScreen").hidden = !screen;
  button("saveRecord").disabled = !take || running || busy;
  button("downloadRecord").disabled = !take || running || busy;
  prompter.hidden = Boolean(screen) || !script.value.trim();
  button("pipRecord").hidden = !document.pictureInPictureEnabled || !canvas.captureStream;
}

function sceneChanged() {
  updateControls();
  engagement?.sceneChanged();
}

if (healthEndpoint && relayHealth) {
  let reading = false;
  window.setInterval(async () => {
    const session = active;
    if (!session?.broadcast || reading || document.hidden) return;
    reading = true;
    try {
      const result = await fetch(`${healthEndpoint}?source=relay`, {
        cache: "no-store", signal: AbortSignal.timeout(8000), headers: {Authorization: `Bearer ${key.value}`},
      });
      const value: RelayHealth = result.ok ? await result.json() : {};
      if (active !== session) return;
      relayHealth.textContent = relaySummary(value, "Relay measurements unavailable.");
      relayHealth.hidden = false;
    } catch {
      if (active === session) {
        relayHealth.textContent = "Relay measurements unavailable.";
        relayHealth.hidden = false;
      }
    } finally {
      reading = false;
    }
  }, 3000);
}

async function listDevices() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  for (const [name, kind] of [["cameraDevice", "videoinput"], ["micDevice", "audioinput"]]) {
    const list = select(name);
    const previous = list.value;
    list.replaceChildren(new Option("Default", ""));
    for (const [index, device] of devices.filter(entry => entry.kind === kind && entry.deviceId !== "default").entries()) {
      if (device.deviceId) list.add(new Option(device.label || `Device ${index + 1}`, device.deviceId));
    }
    if (Array.from(list.options).some(option => option.value === previous)) list.value = previous;
  }
}

function wireAudio() {
  if (!audio) return;
  const sources = [
    {id: "mic", name: "Microphone", stream: camera?.stream, onAir: true},
    {id: "screen", name: "Shared screen", stream: screen?.stream, onAir: true},
    ...(speakers?.connectedFeeds().map(feed => ({...feed, id: "speaker-" + feed.id})) || []),
  ];
  const inputs: StudioInput[] = [];
  for (const {id, name, stream, onAir} of sources) {
    const tracks = stream?.getAudioTracks().filter(track => track.readyState === "live");
    if (tracks?.length) inputs.push({id, name, media: tracks, onAir});
  }
  if (studioClip) inputs.push({id: "clip", name: "Scene clip", media: studioClip, onAir: true});
  audio.reconcile(inputs);
}

function selectClipAudio(video?: HTMLVideoElement) {
  studioClip = video;
  wireAudio();
}

async function sound() {
  audio ??= new StudioAudio(element("studioAudioInputs"), element("studioAudioEmpty"));
  wireAudio();
  await audio.context.resume();
}

audioPanel.querySelector("summary")?.addEventListener("click", () => {
  if (!audioPanel.open) run(sound);
});

function draw(now: number) {
  requestAnimationFrame(draw);
  if (now - lastPaint < 28) return;
  const options = {layout: select("recordLayout").value, color: input("recordColor").value,
    background: select("recordBackground").value} as Composition;
  const guests = speakers?.activeFeeds().flatMap(feed => feed.preview ? [feed.preview] : []) || [];
  const presenter = guests[0] || camera?.preview || null;
  const graphic = engagement?.graphic();
  const painted = scenes?.draw(context, presenter, screen?.preview || null, Boolean(graphic));
  if (!painted && guests.length > 1) {
    const local = [screen?.preview, camera?.preview].filter(item => item != null);
    drawTileGrid(context, [...guests, ...local], options.color);
  } else if (!painted) {
    drawRecording(context, options, presenter, screen?.preview || null);
  }
  if (graphic) engagement?.paint(context, graphic);
  audio?.updateMeters();
  if (active) {
    const elapsed = Math.max(0, ((active.paused || now) - active.started - active.pausedTotal) / 1000);
    liveText.textContent = `${Math.floor(elapsed / 60)}:${String(Math.floor(elapsed % 60)).padStart(2, "0")}`;
    if (!active.paused && !prompter.hidden) {
      scrollTop += Math.min(100, now - lastPaint) * .023 * Number(select("recordScroll").value);
      prompter.scrollTop = scrollTop;
    }
  }
  lastPaint = now;
}

function resize() {
  const size = recordingSize(Number(select("recordQuality").value), select("recordRatio").value);
  canvas.width = size.width;
  canvas.height = size.height;
}

async function connectDevices() {
  const video = input("recordCameraEnabled").checked;
  const audioEnabled = input("recordMicEnabled").checked;
  const cameraId = select("cameraDevice").value;
  const micId = select("micDevice").value;
  const cameraConstraints = cameraId ? {deviceId: {exact: cameraId}} : true;
  const micConstraints = micId ? {deviceId: {exact: micId}} : true;
  const stream = video || audioEnabled ? await navigator.mediaDevices.getUserMedia({
    video: video && cameraConstraints, audio: audioEnabled && micConstraints,
  }) : null;
  closeSource(camera);
  camera = stream ? {stream, preview: mediaPreview(stream)} : null;
  stream?.getTracks().forEach(track => track.addEventListener("ended", () => {
    if (camera?.stream !== stream) return;
    wireAudio();
    if (active && !hasSource(camera) && !hasSource(screen)) stop();
    updateControls();
  }, {once: true}));
  await sound();
  await listDevices();
  status.textContent = stream ? "Recording devices connected." : "Camera and microphone disconnected.";
  updateControls();
}

function closeScreen() {
  closeSource(screen);
  screen = null;
  wireAudio();
  updateControls();
}

async function shareScreen() {
  const stream = await navigator.mediaDevices.getDisplayMedia({
    video: true, audio: input("recordSystemAudio").checked,
  });
  closeScreen();
  screen = {stream, preview: mediaPreview(stream)};
  stream.getAudioTracks().forEach(track => track.addEventListener("ended", wireAudio, {once: true}));
  stream.getVideoTracks()[0]?.addEventListener("ended", () => {
    if (screen?.stream !== stream) return;
    closeScreen();
    if (active && !hasSource(camera)) stop();
  }, {once: true});
  await sound();
  status.textContent = "Screen sharing is ready.";
  updateControls();
}

function run(action: () => Promise<void>) {
  void action().catch(message);
}

function connect(action: () => Promise<void>) {
  run(async () => {
    if (busy || active) return;
    busy = true;
    updateControls();
    try { await action(); }
    finally { busy = false; updateControls(); }
  });
}

button("enableDevices").onclick = () => connect(connectDevices);
button("enableScreen").onclick = () => connect(shareScreen);
button("releaseScreen").onclick = closeScreen;
if (mode) mode.onchange = updateControls;
updateControls();
select("recordQuality").onchange = select("recordRatio").onchange = resize;
script.oninput = () => { prompter.textContent = script.value; updateControls(); };
input("recordFont").oninput = () => { prompter.style.fontSize = input("recordFont").value + "px"; };

function draftTitle(draft: RecordingDraft) {
  return draft.title || "Recording";
}

function openTake(draft: RecordingDraft, data: Blob) {
  if (!data.size) throw new Error("This draft has no video data yet.");
  if (take) URL.revokeObjectURL(take.url);
  take = {draft, data, url: URL.createObjectURL(data)};
  film.src = take.url;
  film.hidden = false;
  title.value = draft.title;
  folder.value = draft.folder;
  if (folder.selectedIndex < 0) folder.value = "";
  element("savedRecording").hidden = true;
  updateControls();
}

async function showDrafts() {
  if (!storage) return;
  const drafts = await storage.list();
  draftList.replaceChildren();
  for (const draft of drafts.filter(entry => entry.id !== active?.draft.id && entry.id !== take?.draft.id)) {
    const row = document.createElement("li");
    const open = document.createElement("button");
    const discard = document.createElement("button");
    open.type = discard.type = "button";
    open.textContent = "Recover " + draftTitle(draft);
    discard.textContent = "Discard " + draftTitle(draft);
    open.onclick = () => run(async () => {
      openTake(draft, await storage!.load(draft));
      status.textContent = "Recovered recording. Play it or save it to the library.";
      await showDrafts();
    });
    discard.onclick = () => run(async () => { await storage!.remove(draft); await showDrafts(); });
    row.append(open, discard);
    draftList.append(row);
  }
}

async function start() {
  if (active || busy || !ready()) return;
  busy = true;
  element("savedRecording").hidden = true;
  updateControls();
  let stream: MediaStream | null = null;
  let broadcast: BrowserBroadcast | null = null;
  try {
    if (typeof MediaRecorder === "undefined" || !canvas.captureStream) {
      throw new Error("Use a browser with MediaRecorder and canvas capture to record here.");
    }
    await sound();
    stream = canvasStream();
    if (audio && (audio.hasProgramInput || scenes)) {
      const track = audio.output.stream.getAudioTracks()[0];
      if (track) stream.addTrack(track.clone());
    }
    const mime = recordingMime();
    if (mode?.value === "broadcast" && !mime?.startsWith("video/webm")) {
      throw new Error("Choose a browser with WebM recording support for live production.");
    }
    const recorder = new MediaRecorder(stream, mime ? {mimeType: mime} : undefined);
    const draft = {id: crypto.randomUUID(), mime: recorder.mimeType, created: Date.now(),
      title: title.value.trim() || "Recording", folder: folder.value};
    let persisted = Boolean(storage);
    if (storage) {
      try { await storage.save(draft); } catch { persisted = false; }
    }
    const session: Session = {recorder, output: stream, draft, started: performance.now(),
      paused: 0, pausedTotal: 0, parts: [], sequence: 0,
      store: persisted, persistence: Promise.resolve(), storageError: false,
      broadcast: null, broadcastIssue: false};
    if (broadcastEndpoint && mode?.value === "broadcast") {
      broadcast = await BrowserBroadcast.open(broadcastEndpoint, key.value, () => {
        session.broadcastIssue = true;
        status.textContent = "Broadcast stopped. The local recording continues.";
      });
      session.broadcast = broadcast;
    }
    recorder.ondataavailable = ({data}) => {
      if (!data.size) return;
      const sequence = session.sequence++;
      session.broadcast?.add(data.type ? data : new Blob([data], {type: "video/webm"}));
      session.persistence = session.persistence.then(async () => {
        if (session.store && !session.storageError) {
          try { await storage!.append(draft, sequence, data); return; }
          catch { session.storageError = true; stop(); }
        }
        session.parts.push(data);
      });
    };
    recorder.onstop = () => run(() => finish(session));
    recorder.onerror = () => { status.textContent = "Recording interrupted. CuePorch will keep the available video."; };
    if (take) URL.revokeObjectURL(take.url);
    take = null;
    film.hidden = true;
    film.removeAttribute("src");
    film.load();
    scrollTop = prompter.scrollTop = 0;
    recorder.start(broadcast ? 500 : 1000);
    active = session;
    status.textContent = broadcast ? "Sending the live broadcast and keeping a local recording."
      : persisted ? "Recording." : "Recording. Save this take before leaving Studio.";
  } catch (error) {
    void broadcast?.close();
    stream?.getTracks().forEach(track => track.stop());
    throw error;
  } finally {
    busy = false;
    updateControls();
  }
}

function stop() {
  if (!active || active.recorder.state === "inactive") return;
  busy = true;
  updateControls();
  status.textContent = "Preparing recording…";
  active.recorder.stop();
}

async function finish(session: Session) {
  busy = true;
  session.output.getTracks().forEach(track => track.stop());
  active = null;
  closeSource(camera);
  camera = null;
  closeScreen();
  void session.broadcast?.finish();
  try {
    await session.persistence;
    const initial = session.store ? await storage!.load(session.draft) : null;
    const data = new Blob([...(initial ? [initial] : []), ...session.parts], {type: session.draft.mime});
    openTake(session.draft, data);
    status.textContent = session.broadcastIssue ? "Broadcast interrupted. The local recording is ready."
      : session.storageError ? "Browser storage filled. Save this recording before leaving Studio."
      : "Recording ready. Play it, download it or save it to the library.";
    await showDrafts();
  } finally {
    busy = false;
    updateControls();
  }
}

button("beginRecord").onclick = () => run(start);
button("endRecord").onclick = stop;
button("pauseRecord").onclick = () => {
  if (!active) return;
  const now = performance.now();
  if (active.recorder.state === "paused") {
    active.recorder.resume();
    active.pausedTotal += now - active.paused;
    active.paused = 0;
    status.textContent = "Recording.";
  } else if (active.recorder.state === "recording") {
    active.recorder.pause();
    active.paused = now;
    status.textContent = "Recording paused.";
  }
  updateControls();
};

button("pipRecord").onclick = () => run(async () => {
  if (document.pictureInPictureElement) { await document.exitPictureInPicture(); return; }
  const picture = document.createElement("video");
  picture.muted = true;
  picture.playsInline = true;
  const stream = canvasStream();
  pictureStream = stream;
  const close = () => { stream.getTracks().forEach(track => track.stop()); pictureStream = null; };
  picture.srcObject = stream;
  try {
    await picture.play();
    await picture.requestPictureInPicture();
    picture.addEventListener("leavepictureinpicture", close, {once: true});
  } catch (error) { close(); throw error; }
});

function filename(draft: RecordingDraft) {
  const name = title.value.trim() || draft.title || "Recording";
  return name + (draft.mime.startsWith("video/mp4") ? ".mp4" : ".webm");
}

button("downloadRecord").onclick = () => {
  if (!take) return;
  const link = document.createElement("a");
  link.href = take.url;
  link.download = filename(take.draft);
  link.click();
};

element<HTMLFormElement>("recordDestination").onsubmit = event => {
  event.preventDefault();
  run(async () => {
    if (!take || busy) return;
    busy = true;
    updateControls();
    const {draft, data} = take;
    try {
      const file = new File([data], filename(draft), {type: data.type});
      const receipt = await uploadLibraryFile(file, draft.id, folder.value, key.value, null, (progress) => {
        status.textContent = progress === null ? "Preparing the library copy…" : `Uploading ${Math.floor(progress)}%…`;
      });
      element<HTMLAnchorElement>("savedRecordingLink").href = "/operator/videos/" + encodeURIComponent(receipt.id);
      element("savedRecording").hidden = false;
      status.textContent = "Recording saved to the library.";
      try {
        if (storage) await storage.remove(draft);
        await showDrafts();
      } catch {
        status.textContent = "Recording saved to the library. The browser still holds a local draft.";
      }
      if (document.body.dataset.embedded === "timeline" && window.parent !== window) {
        window.parent.postMessage({type: "cueporch-recording-saved", work: receipt.id}, location.origin);
      }
    } finally {
      busy = false;
      updateControls();
    }
  });
};

async function open() {
  const response = await fetch("/api/library/folders", {headers: {Authorization: "Bearer " + key.value}});
  if (!response.ok) throw new Error("Enter a valid operator credential.");
  const folders: Folder[] = await response.json();
  const byId = new Map(folders.map(item => [item.id, item]));
  const names = (item: Folder): string => {
    const parent = item.parent && byId.get(item.parent);
    return parent ? names(parent) + " / " + item.name : item.name;
  };
  for (const item of folders) folder.add(new Option(names(item), item.id));
  const specified = new URLSearchParams(location.search).get("folder");
  if (specified && byId.has(specified)) folder.value = specified;
  sessionStorage.setItem("cueporchOperator", key.value);
  identity.hidden = true;
  element("recordStudio").hidden = false;
  if (!navigator.mediaDevices) {
    element<HTMLFieldSetElement>("recordSources").disabled = true;
    status.textContent = "Open Studio on localhost or HTTPS to access recording devices.";
  }
  try { storage = await openRecordingStorage(); await showDrafts(); } catch { storage = null; }
  if (navigator.mediaDevices) await listDevices();
  if (scenes) await scenes.load().catch(message);
  requestAnimationFrame(draw);
}

identity.onsubmit = event => { event.preventDefault(); run(open); };
if (key.value) run(open);
window.addEventListener("pagehide", () => {
  engagement?.close();
  speakers?.close();
  scenes?.close();
  closeSource(camera);
  closeSource(screen);
  pictureStream?.getTracks().forEach(track => track.stop());
  active?.output.getTracks().forEach(track => track.stop());
  void active?.broadcast?.close();
  if (take) URL.revokeObjectURL(take.url);
  void audio?.context.close();
});
