import { setupLiveCaptions } from "./live_caption_player.js";
import { measureEventPlayback } from "./event_playback.js";
import { measurePeerEventPlayback } from "./peer_event_playback.js";
import { waitingPlayer } from "./event_waiting.js";
import { eventControls } from "./event_controls.js";
const lobby = document.querySelector("#eventLobby");
const eventStatus = document.querySelector("#eventStatus");
const player = document.querySelector("#eventPlayer");
const branding = document.querySelector("#eventBranding");
const logo = branding.querySelector("img");
const byline = branding.querySelector(".event-byline");
const liveBadge = branding.querySelector(".event-live-badge");
const viewerCount = branding.querySelector(".event-viewer-count");
const dateRow = document.querySelector("#eventOccurrenceTime");
const eventTitle = document.querySelector("#eventHeader h1");
const controls = eventControls(player, eventTitle, JSON.parse(document.querySelector("#eventAppearanceData").textContent || "{}"));
const date = dateRow.querySelector("time");
const audiencePanels = document.querySelector("#eventAudiencePanels");
const archiveNav = document.querySelector("#eventArchivePlaylist");
const archiveChoice = document.querySelector("#archiveChoice");
const previous = document.querySelector("#archivePrevious");
const next = document.querySelector("#archiveNext");
const waiting = waitingPlayer(lobby);
const openingOccurrence = Number(lobby.dataset.occurrence) || null;
let currentOccurrence = Number(lobby.dataset.playing) || openingOccurrence;
let previewReplay = lobby.dataset.previewReplay === "yes";
let playlist = null;
let entrySignature = "";
let playNextMuted;
let checking = false;
let hls;
let stopMeasuring;
const Hls = window.Hls;
const viewer = lobby.dataset.viewerGrant || "";
const sharing = document.querySelector("#stopAttendeeSharing");
if (sharing) {
    const status = document.querySelector("#attendeeSharingStatus");
    sharing.onclick = async () => {
        sharing.disabled = true;
        try {
            const reply = await fetch("/api/event-playback/withdraw-sharing", { method: "POST",
                headers: { "X-CuePorch-Event-Viewer": viewer } });
            if (!reply.ok)
                throw new Error("CuePorch couldn't change sharing. Try again.");
            sharing.hidden = true;
            status.textContent = "Watch-time sharing stopped for this viewing session.";
        }
        catch {
            sharing.disabled = false;
            status.textContent = "CuePorch couldn't change sharing. Try again.";
        }
    };
}
const audienceAccess = lobby.dataset.audienceAccess || "";
const captions = setupLiveCaptions(viewer, lobby.dataset.embed === "yes", audienceAccess);
const latencyMode = lobby.dataset.latency || "standard";
const liveSyncSegments = new Map([["low", 2], ["standard", 2], ["fail-safe", 10]]);
function showBranding(value) {
    logo.hidden = !value.logo;
    if (value.logo && logo.getAttribute("src") !== value.logo)
        logo.src = value.logo;
    if (!value.logo)
        logo.removeAttribute("src");
    byline.textContent = value.byline;
    byline.hidden = !value.byline;
    liveBadge.hidden = !value.live_badge;
    viewerCount.hidden = value.viewers === null;
    viewerCount.textContent = value.viewers === null ? "" : `${value.viewers} watching`;
    branding.hidden = logo.hidden && byline.hidden && liveBadge.hidden && viewerCount.hidden;
}
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
function renderVideo(address, phase, analytics, transport, video) {
    const local = transport.startsWith("local:");
    const liveSegments = local ? 2 : liveSyncSegments.get(latencyMode) ?? 2;
    stopMeasuring?.();
    player.dataset.address = address;
    player.dataset.phase = phase;
    player.dataset.transport = transport;
    hls?.destroy();
    hls = undefined;
    if (!video) {
        video = document.createElement("video");
        video.controls = true;
        video.playsInline = true;
        video.setAttribute("aria-label", "Event video");
        player.replaceChildren(video, branding);
    }
    stopMeasuring = measureEventPlayback(video, analytics);
    const replay = phase === "Replay";
    const currentVideo = video;
    controls.attach(video, !replay, () => {
        const ranges = currentVideo.seekable;
        if (!ranges.length)
            return null;
        const last = ranges.length - 1;
        const start = ranges.start(last);
        const end = ranges.end(last);
        const seconds = local ? (latencyMode === "fail-safe" ? 2 : 1) : latencyMode === "low" ? 2 : 4;
        const position = hls ? hls.liveSyncPosition : end - liveSegments * seconds;
        return position === null || !Number.isFinite(position) || end - start < 1
            ? null : Math.min(end - .5, Math.max(start, position));
    });
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
            liveSyncDurationCount: liveSegments,
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
    controls.reset();
    hls?.destroy();
    hls = undefined;
    player.dataset.address = "";
    player.dataset.phase = "";
    player.dataset.transport = "";
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
        if (audienceAccess)
            headers["X-CuePorch-Event-Access"] = audienceAccess;
        if (lobby.dataset.frameGrant)
            headers["X-CuePorch-Event-Frame"] = lobby.dataset.frameGrant;
        if (lobby.dataset.analyticsContext)
            headers["X-CuePorch-Event-Context"] = lobby.dataset.analyticsContext;
        const response = await fetch(requested, { cache: "no-store", headers });
        if (requested !== location.href)
            return;
        if (response.status === 404 || response.status === 403) {
            eventStatus.textContent = response.status === 403 ? "This event is unavailable in your region" : "This event is unavailable";
            closePlayer();
            waiting(null);
            showPlaylist(null);
            stopChecking();
            return;
        }
        if (!response.ok)
            return;
        const { status: next, address: src, native_video: native, transport, captions: captionPath, analytics_token: analytics, occurrence, playlist: archives, lobby_preview: before, preview_replay: fallback, appearance, branding: brand } = await response.json();
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
        if (previewReplay && !src)
            closePlayer();
        previewReplay = fallback;
        lobby.dataset.previewReplay = fallback ? "yes" : "no";
        controls.setAppearance(appearance);
        showBranding(brand);
        const current = player.querySelector("iframe");
        if (next === "This occurrence was withdrawn" || (viewer && next === "Registration required")) {
            closePlayer();
            if (!archives)
                stopChecking();
        }
        else if (src && native) {
            const video = player.querySelector("video");
            if (!video || player.dataset.phase !== next || changedOccurrence) {
                renderVideo(src, next, analytics || "", transport);
            }
            else if (player.dataset.transport !== transport) {
                const playing = !video.paused;
                renderVideo(src, next, analytics || "", transport, video);
                if (playing)
                    void video.play().catch(() => { });
            }
        }
        else if (src && (current?.getAttribute("src") !== src || eventStatus.textContent !== next)) {
            closePlayer();
            const frame = document.createElement("iframe");
            frame.title = "Event video";
            frame.src = src;
            frame.allow = "autoplay; fullscreen; picture-in-picture";
            frame.allowFullscreen = true;
            player.replaceChildren(frame, branding);
            stopMeasuring = measurePeerEventPlayback(frame, analytics || "");
            if (next === "Replay" && !archives && !previewReplay)
                stopChecking();
        }
        else if (current && !src && next === "The stream is unavailable") {
            // A live player can coast through a delayed host poll without restarting.
            return;
        }
        else if (current && !src) {
            closePlayer();
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
if (!playlist && !previewReplay && (eventStatus.textContent === "This occurrence was withdrawn" ||
    (!initial && eventStatus.textContent === "Replay")))
    stopChecking();
if (initial && player.dataset.address)
    renderVideo(player.dataset.address, eventStatus.textContent || "", player.dataset.analytics || "", player.dataset.transport || "hosted", initial);
const initialFrame = player.querySelector("iframe");
if (initialFrame)
    stopMeasuring = measurePeerEventPlayback(initialFrame, player.dataset.analytics || "");
