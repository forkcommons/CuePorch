const lobby = document.querySelector<HTMLElement>("#eventLobby")!;
const eventStatus = document.querySelector<HTMLElement>("#eventStatus")!;
const player = document.querySelector<HTMLElement>("#eventPlayer")!;
let checking = false;
let hls: {destroy(): void} | undefined;
interface HlsClient {
  loadSource(url: string): void;
  attachMedia(video: HTMLVideoElement): void;
  destroy(): void;
}
type HlsFactory = {new (config: object): HlsClient; isSupported(): boolean};
const Hls = (window as Window & {Hls?: HlsFactory}).Hls;
const viewer = lobby.dataset.viewerGrant || "";

function renderVideo(address: string, video?: HTMLVideoElement) {
  player.dataset.address = address;
  hls?.destroy();
  hls = undefined;
  if (!video) {
    video = document.createElement("video");
    video.controls = true;
    video.playsInline = true;
    video.setAttribute("aria-label", "Event video");
    player.replaceChildren(video);
  }
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = address;
  } else if (Hls?.isSupported()) {
    const session = new Hls({enableWorker: true});
    session.attachMedia(video);
    session.loadSource(address);
    hls = session;
  } else {
    eventStatus.textContent = "This browser cannot play the stream";
  }
}

function closePlayer() {
  hls?.destroy();
  hls = undefined;
  player.dataset.address = "";
  player.replaceChildren();
}

async function checkStatus() {
  if (checking) return;
  checking = true;
  try {
    const headers: Record<string, string> = {Accept: "application/json"};
    if (viewer) headers["X-CuePorch-Event-Viewer"] = viewer;
    const response = await fetch(location.href, {cache: "no-store", headers});
    if (response.status === 404) {
      eventStatus.textContent = "This event is unavailable";
      closePlayer();
      stopChecking();
      return;
    }
    if (!response.ok) return;
    const {status: next, address: src, protected: restricted}: {
      status: string; address: string | null; protected: boolean;
    } = await response.json();
    const current = player.querySelector("iframe");
    if (next === "This occurrence was withdrawn" || (viewer && next === "Registration required")) {
      closePlayer();
      stopChecking();
    } else if (src && restricted) {
      if (!player.querySelector("video") || eventStatus.textContent !== next) renderVideo(src);
    } else if (src && (current?.getAttribute("src") !== src || eventStatus.textContent !== next)) {
      const frame = document.createElement("iframe");
      frame.title = "Event video";
      frame.src = src;
      frame.allow = "autoplay; fullscreen; picture-in-picture";
      frame.allowFullscreen = true;
      player.replaceChildren(frame);
      if (next === "Replay") stopChecking();
    } else if (current && !src && next === "The stream is unavailable") {
      // A live player can coast through a delayed host poll without restarting.
      return;
    }
    if (eventStatus.textContent !== next) eventStatus.textContent = next;
  } catch {
    // Keep the last confirmed state while the connection is unavailable.
  } finally {
    checking = false;
  }
}

const timer = window.setInterval(() => {
  if (!document.hidden) void checkStatus();
}, 5000);
function stopChecking() { window.clearInterval(timer); }

lobby.addEventListener("click", (event) => {
  if ((event.target as HTMLElement).id === "refreshEvent") void checkStatus();
});
if (["Replay", "This occurrence was withdrawn"].includes(eventStatus.textContent || "")) stopChecking();
if (!viewer && document.querySelector('[name="enrollment"]')) stopChecking();
const initial = player.querySelector("video");
if (initial && player.dataset.address) renderVideo(player.dataset.address, initial);
