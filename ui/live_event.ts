import {setupLiveCaptions} from "./live_caption_player.js";
import {measureEventPlayback} from "./event_playback.js";
import {waitingPlayer, type BeforeEvent} from "./event_waiting.js";

const lobby = document.querySelector<HTMLElement>("#eventLobby")!;
const eventStatus = document.querySelector<HTMLElement>("#eventStatus")!;
const player = document.querySelector<HTMLElement>("#eventPlayer")!;
const dateRow = document.querySelector<HTMLElement>("#eventOccurrenceTime")!;
const date = dateRow.querySelector("time")!;
const audiencePanels = document.querySelector<HTMLElement>("#eventAudiencePanels")!;
const archiveNav = document.querySelector<HTMLElement>("#eventArchivePlaylist")!;
const archiveChoice = document.querySelector<HTMLSelectElement>("#archiveChoice")!;
const previous = document.querySelector<HTMLButtonElement>("#archivePrevious")!;
const next = document.querySelector<HTMLButtonElement>("#archiveNext")!;
interface ArchivePlaylist {entries: {id: number; label: string}[]; autoplay_next: boolean; loop: boolean;}
interface EventView {
  status: string; address: string | null; native_video: boolean; captions: string | null; analytics_token: string | null;
  occurrence: {id: number; start: string; local: string} | null; playlist: ArchivePlaylist | null;
  lobby_preview: BeforeEvent | null;
  preview_replay: boolean;
}
const waiting = waitingPlayer(lobby);
const openingOccurrence = Number(lobby.dataset.occurrence) || null;
let currentOccurrence = Number(lobby.dataset.playing) || openingOccurrence;
let previewReplay = lobby.dataset.previewReplay === "yes";
let playlist: ArchivePlaylist | null = null;
let entrySignature = "";
let playNextMuted: boolean | undefined;
let checking = false;
let hls: {destroy(): void} | undefined;
let stopMeasuring: (() => void) | undefined;
interface HlsClient {
  readonly playingDate: Date | null;
  loadSource(url: string): void;
  attachMedia(video: HTMLVideoElement): void;
  destroy(): void;
}
type HlsFactory = {new (config: object): HlsClient; isSupported(): boolean};
const Hls = (window as Window & {Hls?: HlsFactory}).Hls;
const viewer = lobby.dataset.viewerGrant || "";
const captions = setupLiveCaptions(viewer, lobby.dataset.embed === "yes");
const liveSyncSegments = new Map([["low", 2], ["standard", 5], ["fail-safe", 10]]);

function neighboringArchive(direction: -1 | 1) {
  const entries = playlist?.entries ?? [];
  const index = entries.findIndex(entry => entry.id === currentOccurrence);
  if (index < 0) return direction === -1 ? entries.at(-1) : undefined;
  return entries[index + direction] ?? (playlist?.loop ? entries.at(direction === 1 ? 0 : -1) : undefined);
}

function showPlaylist(options: ArchivePlaylist | null) {
  playlist = options;
  const entries = options?.entries ?? [];
  const signature = JSON.stringify(entries);
  if (entrySignature !== signature) {
    const prompt = new Option("Select a past broadcast", "");
    prompt.disabled = true;
    archiveChoice.replaceChildren(prompt, ...entries.map(entry => new Option(entry.label, String(entry.id))));
    entrySignature = signature;
  }
  archiveChoice.value = entries.some(entry => entry.id === currentOccurrence) ? String(currentOccurrence) : "";
  previous.disabled = !neighboringArchive(-1) || neighboringArchive(-1)?.id === currentOccurrence;
  next.disabled = !neighboringArchive(1) || neighboringArchive(1)?.id === currentOccurrence;
  archiveNav.hidden = !entries.length;
}

function viewArchive(opens: number, autoplayMuted?: boolean) {
  if (!playlist?.entries.some(entry => entry.id === opens)) return;
  if (opens === currentOccurrence) {
    const video = player.querySelector("video");
    if (video && autoplayMuted !== undefined) {
      video.currentTime = 0;
      void video.play().catch(() => {});
    }
    return;
  }
  const address = new URL(location.href);
  address.searchParams.set("occurrence", String(opens));
  history.replaceState(history.state, "", address);
  closePlayer();
  playNextMuted = autoplayMuted;
  audiencePanels.hidden = true;
  eventStatus.textContent = "Loading replay";
  void checkStatus();
}

function renderVideo(address: string, phase: string, analytics: string, video?: HTMLVideoElement) {
  stopMeasuring?.();
  player.dataset.address = address;
  player.dataset.phase = phase;
  hls?.destroy();
  hls = undefined;
  if (!video) {
    video = document.createElement("video");
    video.controls = true;
    video.playsInline = true;
    video.setAttribute("aria-label", "Event video");
    player.replaceChildren(video);
  }
  stopMeasuring = measureEventPlayback(video, analytics);
  const replay = phase === "Replay";
  const currentVideo = video;
  currentVideo.onended = replay ? () => {
    const following = neighboringArchive(1);
    if (playlist?.autoplay_next && following) viewArchive(following.id, currentVideo.muted);
  } : null;
  if (replay && playNextMuted !== undefined) {
    currentVideo.muted = playNextMuted;
    playNextMuted = undefined;
    currentVideo.addEventListener("canplay", () => { void currentVideo.play().catch(() => {}); }, {once: true});
  }
  const replayDate = () => video!.currentTime * 1000;
  if (Hls?.isSupported()) {
    const session = new Hls({
      enableWorker: true,
      liveDurationInfinity: true,
      liveSyncDurationCount: liveSyncSegments.get(lobby.dataset.latency || "") ?? 5,
    });
    session.loadSource(address);
    session.attachMedia(video);
    hls = session;
    captions.bind(video, replay ? replayDate : () => session.playingDate?.getTime() ?? null, replay);
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = address;
    const native = video as HTMLVideoElement & {getStartDate?(): Date};
    captions.bind(video, replay ? replayDate :
      () => (native.getStartDate?.().getTime() ?? Number.NaN) + native.currentTime * 1000, replay);
  } else {
    eventStatus.textContent = "This browser cannot play the stream";
  }
}

function closePlayer() {
  stopMeasuring?.();
  stopMeasuring = undefined;
  captions.bind();
  captions.select("");
  hls?.destroy();
  hls = undefined;
  player.dataset.address = "";
  player.dataset.phase = "";
  player.replaceChildren();
}

async function checkStatus() {
  if (checking) return;
  checking = true;
  const requested = location.href;
  try {
    const headers: Record<string, string> = {Accept: "application/json"};
    if (viewer) headers["X-CuePorch-Event-Viewer"] = viewer;
    const response = await fetch(requested, {cache: "no-store", headers});
    if (requested !== location.href) return;
    if (response.status === 404) {
      eventStatus.textContent = "This event is unavailable";
      closePlayer();
      waiting(null);
      showPlaylist(null);
      stopChecking();
      return;
    }
    if (!response.ok) return;
    const {status: next, address: src, native_video: native, captions: captionPath, analytics_token: analytics,
      occurrence, playlist: archives, lobby_preview: before, preview_replay: fallback}: EventView = await response.json();
    if (requested !== location.href) return;
    const changedOccurrence = currentOccurrence !== (occurrence?.id ?? null);
    currentOccurrence = occurrence?.id ?? null;
    dateRow.hidden = !occurrence;
    if (occurrence) {
      date.dateTime = occurrence.start;
      date.textContent = `${occurrence.local.replace("T", " ")} (${lobby.dataset.timezone})`;
    }
    audiencePanels.hidden = currentOccurrence !== openingOccurrence;
    showPlaylist(archives);
    if (previewReplay && !src) closePlayer();
    previewReplay = fallback;
    lobby.dataset.previewReplay = fallback ? "yes" : "no";
    const current = player.querySelector("iframe");
    if (next === "This occurrence was withdrawn" || (viewer && next === "Registration required")) {
      closePlayer();
      if (!archives) stopChecking();
    } else if (src && native) {
      if (!player.querySelector("video") || player.dataset.phase !== next || changedOccurrence) {
        renderVideo(src, next, analytics || "");
      }
    } else if (src && (current?.getAttribute("src") !== src || eventStatus.textContent !== next)) {
      const frame = document.createElement("iframe");
      frame.title = "Event video";
      frame.src = src;
      frame.allow = "autoplay; fullscreen; picture-in-picture";
      frame.allowFullscreen = true;
      captions.bind();
      player.replaceChildren(frame);
      if (next === "Replay" && !archives && !previewReplay) stopChecking();
    } else if (current && !src && next === "The stream is unavailable") {
      // A live player can coast through a delayed host poll without restarting.
      return;
    }
    waiting(src || player.querySelector("video,iframe") ? null : before);
    captions.select(captionPath || "");
    if (eventStatus.textContent !== next) eventStatus.textContent = next;
  } catch {
    // Keep the last confirmed state while the connection is unavailable.
  } finally {
    checking = false;
    if (requested !== location.href) void checkStatus();
  }
}

const timer = window.setInterval(() => {
  if (!document.hidden) void checkStatus();
}, 5000);
function stopChecking() { window.clearInterval(timer); }

archiveChoice.onchange = () => viewArchive(Number(archiveChoice.value));
previous.onclick = () => { const entry = neighboringArchive(-1); if (entry) viewArchive(entry.id); };
next.onclick = () => { const entry = neighboringArchive(1); if (entry) viewArchive(entry.id); };
showPlaylist(JSON.parse(document.querySelector("#eventArchiveData")!.textContent || "null"));

lobby.addEventListener("click", (event) => {
  if ((event.target as HTMLElement).id === "refreshEvent") void checkStatus();
});
if (!viewer && document.querySelector('[name="enrollment"]')) stopChecking();
const initial = player.querySelector("video");
if (!playlist && !previewReplay && (eventStatus.textContent === "This occurrence was withdrawn" ||
    (!initial && eventStatus.textContent === "Replay"))) stopChecking();
if (initial && player.dataset.address) renderVideo(player.dataset.address, eventStatus.textContent || "",
  player.dataset.analytics || "", initial);
