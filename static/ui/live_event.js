import { setupLiveCaptions } from "./live_caption_player.js";
import { measureEventPlayback } from "./event_playback.js";
import { waitingPlayer } from "./event_waiting.js";
const lobby = document.querySelector("#eventLobby");
const eventStatus = document.querySelector("#eventStatus");
const player = document.querySelector("#eventPlayer");
const dateRow = document.querySelector("#eventOccurrenceTime");
const date = dateRow.querySelector("time");
const audiencePanels = document.querySelector("#eventAudiencePanels");
const archiveNav = document.querySelector("#eventArchivePlaylist");
const archiveChoice = document.querySelector("#archiveChoice");
const previous = document.querySelector("#archivePrevious");
const next = document.querySelector("#archiveNext");
const waiting = waitingPlayer(lobby);
const openingOccurrence = Number(lobby.dataset.occurrence) || null;
let currentOccurrence = openingOccurrence;
let playlist = null;
let entrySignature = "";
let playNextMuted;
let checking = false;
let hls;
let stopMeasuring;
const Hls = window.Hls;
const viewer = lobby.dataset.viewerGrant || "";
const captions = setupLiveCaptions(viewer, lobby.dataset.embed === "yes");
const liveSyncSegments = new Map([["low", 2], ["standard", 5], ["fail-safe", 10]]);
function neighboringArchive(direction) {
    const entries = playlist?.entries ?? [];
    const index = entries.findIndex(entry => entry.id === currentOccurrence);
    if (index < 0)
        return direction === -1 ? entries.at(-1) : undefined;
    return entries[index + direction] ?? (playlist?.loop ? entries.at(direction === 1 ? 0 : -1) : undefined);
}
function showPlaylist(options) {
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
function viewArchive(opens, autoplayMuted) {
    if (!playlist?.entries.some(entry => entry.id === opens))
        return;
    if (opens === currentOccurrence) {
        const video = player.querySelector("video");
        if (video && autoplayMuted !== undefined) {
            video.currentTime = 0;
            void video.play().catch(() => { });
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
    const replay = phase === "Replay";
    const currentVideo = video;
    currentVideo.onended = replay ? () => {
        const following = neighboringArchive(1);
        if (playlist?.autoplay_next && following)
            viewArchive(following.id, currentVideo.muted);
    } : null;
    if (replay && playNextMuted !== undefined) {
        currentVideo.muted = playNextMuted;
        playNextMuted = undefined;
        currentVideo.addEventListener("canplay", () => { void currentVideo.play().catch(() => { }); }, { once: true });
    }
    const replayDate = () => video.currentTime * 1000;
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
    }
    else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = address;
        const native = video;
        captions.bind(video, replay ? replayDate :
            () => (native.getStartDate?.().getTime() ?? Number.NaN) + native.currentTime * 1000, replay);
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
    const requested = location.href;
    try {
        const headers = { Accept: "application/json" };
        if (viewer)
            headers["X-CuePorch-Event-Viewer"] = viewer;
        const response = await fetch(requested, { cache: "no-store", headers });
        if (requested !== location.href)
            return;
        if (response.status === 404) {
            eventStatus.textContent = "This event is unavailable";
            closePlayer();
            waiting(null);
            showPlaylist(null);
            stopChecking();
            return;
        }
        if (!response.ok)
            return;
        const { status: next, address: src, native_video: native, captions: captionPath, analytics_token: analytics, occurrence, playlist: archives, lobby_preview: before } = await response.json();
        if (requested !== location.href)
            return;
        const changedOccurrence = currentOccurrence !== (occurrence?.id ?? null);
        currentOccurrence = occurrence?.id ?? null;
        dateRow.hidden = !occurrence;
        if (occurrence) {
            date.dateTime = occurrence.start;
            date.textContent = `${occurrence.local.replace("T", " ")} (${lobby.dataset.timezone})`;
        }
        audiencePanels.hidden = currentOccurrence !== openingOccurrence;
        showPlaylist(archives);
        const current = player.querySelector("iframe");
        if (next === "This occurrence was withdrawn" || (viewer && next === "Registration required")) {
            closePlayer();
            if (!archives)
                stopChecking();
        }
        else if (src && native) {
            if (!player.querySelector("video") || player.dataset.phase !== next || changedOccurrence) {
                renderVideo(src, next, analytics || "");
            }
        }
        else if (src && (current?.getAttribute("src") !== src || eventStatus.textContent !== next)) {
            const frame = document.createElement("iframe");
            frame.title = "Event video";
            frame.src = src;
            frame.allow = "autoplay; fullscreen; picture-in-picture";
            frame.allowFullscreen = true;
            captions.bind();
            player.replaceChildren(frame);
            if (next === "Replay" && !archives)
                stopChecking();
        }
        else if (current && !src && next === "The stream is unavailable") {
            // A live player can coast through a delayed host poll without restarting.
            return;
        }
        waiting(src || player.querySelector("video,iframe") ? null : before);
        captions.select(captionPath || "");
        if (eventStatus.textContent !== next)
            eventStatus.textContent = next;
    }
    catch {
        // Keep the last confirmed state while the connection is unavailable.
    }
    finally {
        checking = false;
        if (requested !== location.href)
            void checkStatus();
    }
}
const timer = window.setInterval(() => {
    if (!document.hidden)
        void checkStatus();
}, 5000);
function stopChecking() { window.clearInterval(timer); }
archiveChoice.onchange = () => viewArchive(Number(archiveChoice.value));
previous.onclick = () => { const entry = neighboringArchive(-1); if (entry)
    viewArchive(entry.id); };
next.onclick = () => { const entry = neighboringArchive(1); if (entry)
    viewArchive(entry.id); };
showPlaylist(JSON.parse(document.querySelector("#eventArchiveData").textContent || "null"));
lobby.addEventListener("click", (event) => {
    if (event.target.id === "refreshEvent")
        void checkStatus();
});
if (!viewer && document.querySelector('[name="enrollment"]'))
    stopChecking();
const initial = player.querySelector("video");
if (!playlist && (eventStatus.textContent === "This occurrence was withdrawn" ||
    (!initial && eventStatus.textContent === "Replay")))
    stopChecking();
if (initial && player.dataset.address)
    renderVideo(player.dataset.address, eventStatus.textContent || "", player.dataset.analytics || "", initial);
