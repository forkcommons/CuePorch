import { setupLiveCaptions } from "./live_caption_player.js";
import { measureEventPlayback } from "./event_playback.js";
const lobby = document.querySelector("#eventLobby");
const eventStatus = document.querySelector("#eventStatus");
const player = document.querySelector("#eventPlayer");
let checking = false;
let hls;
let stopMeasuring;
const Hls = window.Hls;
const viewer = lobby.dataset.viewerGrant || "";
const captions = setupLiveCaptions(viewer, lobby.dataset.embed === "yes");
const liveSyncSegments = new Map([["low", 2], ["standard", 5], ["fail-safe", 10]]);
function renderVideo(address, phase, analytics, video) {
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
    if (Hls?.isSupported()) {
        const session = new Hls({
            enableWorker: true,
            liveDurationInfinity: true,
            liveSyncDurationCount: liveSyncSegments.get(lobby.dataset.latency || "") ?? 5,
        });
        session.loadSource(address);
        session.attachMedia(video);
        hls = session;
        captions.bind(video, () => session.playingDate?.getTime() ?? null);
    }
    else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = address;
        const native = video;
        captions.bind(video, () => (native.getStartDate?.().getTime() ?? Number.NaN) + native.currentTime * 1000);
    }
    else {
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
    if (checking)
        return;
    checking = true;
    try {
        const headers = { Accept: "application/json" };
        if (viewer)
            headers["X-CuePorch-Event-Viewer"] = viewer;
        const response = await fetch(location.href, { cache: "no-store", headers });
        if (response.status === 404) {
            eventStatus.textContent = "This event is unavailable";
            closePlayer();
            stopChecking();
            return;
        }
        if (!response.ok)
            return;
        const { status: next, address: src, native_video: native, captions: captionPath, analytics_token: analytics } = await response.json();
        const current = player.querySelector("iframe");
        if (next === "This occurrence was withdrawn" || (viewer && next === "Registration required")) {
            closePlayer();
            stopChecking();
        }
        else if (src && native) {
            if (!player.querySelector("video") || player.dataset.phase !== next)
                renderVideo(src, next, analytics || "");
        }
        else if (src && (current?.getAttribute("src") !== src || eventStatus.textContent !== next)) {
            const frame = document.createElement("iframe");
            frame.title = "Event video";
            frame.src = src;
            frame.allow = "autoplay; fullscreen; picture-in-picture";
            frame.allowFullscreen = true;
            captions.bind();
            player.replaceChildren(frame);
            if (next === "Replay")
                stopChecking();
        }
        else if (current && !src && next === "The stream is unavailable") {
            // A live player can coast through a delayed host poll without restarting.
            return;
        }
        captions.select(captionPath || "");
        if (eventStatus.textContent !== next)
            eventStatus.textContent = next;
    }
    catch {
        // Keep the last confirmed state while the connection is unavailable.
    }
    finally {
        checking = false;
    }
}
const timer = window.setInterval(() => {
    if (!document.hidden)
        void checkStatus();
}, 5000);
function stopChecking() { window.clearInterval(timer); }
lobby.addEventListener("click", (event) => {
    if (event.target.id === "refreshEvent")
        void checkStatus();
});
if (!viewer && document.querySelector('[name="enrollment"]'))
    stopChecking();
const initial = player.querySelector("video");
if (eventStatus.textContent === "This occurrence was withdrawn" || (!initial && eventStatus.textContent === "Replay"))
    stopChecking();
if (initial && player.dataset.address)
    renderVideo(player.dataset.address, eventStatus.textContent || "", player.dataset.analytics || "", initial);
