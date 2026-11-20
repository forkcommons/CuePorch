import { setupRegistration } from "./video_registration.js";
import { messageAction, renderChat } from "./event_chat.js";
import { setupProducerEngagement } from "./event_engagement.js";
import { relaySummary } from "./event_health.js";
import { waitingPlayer } from "./event_waiting.js";
import { playerColors, playerSwitches } from "./event_controls.js";
const element = (id) => document.getElementById(id);
const input = (id) => element(id);
const button = (id) => element(id);
const identify = element("eventsIdentify");
const credential = identify.elements.namedItem("credential");
const status = element("eventsStatus");
const workspace = element("eventsWorkspace");
const form = element("newEventForm");
const field = (name) => form.elements.namedItem(name);
const mode = element("eventMode");
const preset = element("eventPreset");
const startingOptions = { method: "browser", registration: false, preview_encoder: false,
    chat: { enabled: false, interval: 0 }, captions: { enabled: false, language: "en" },
    qa: { enabled: false, anonymous: false, review: false } };
const typeDefaults = {
    live: { latency: "standard", chat: true, captions: true, method: "encoder" },
    webinar: { latency: "standard", chat: true, captions: true, registration: true, qa: true },
    team: { latency: "low", chat: true, captions: true },
    learning: { latency: "standard", captions: true, registration: true, qa: true },
    custom: { latency: "standard" },
};
const output = element("eventProduction");
const health = element("eventHealth");
const measurements = element("eventHealthDetails");
const analytics = element("eventAnalytics");
const analyticsRows = element("eventAnalyticsRows");
const analyticsStatus = element("eventAnalyticsStatus");
const captionSettings = element("eventCaptionSettings");
const captionForm = element("productionCaptions");
const captionEnabled = captionForm.elements.namedItem("enabled");
const captionLanguage = captionForm.elements.namedItem("language");
const captionStatus = element("productionCaptionStatus");
const replayCaptions = element("eventReplayCaptions");
const replayCaptionForm = element("replaceReplayCaptions");
const replayCaptionStatus = element("replayCaptionStatus");
const libraryRecording = element("eventLibraryRecording");
const libraryRecordingStatus = element("libraryRecordingStatus");
const libraryRecordingLink = element("libraryRecordingLink");
const saveLibraryRecording = button("saveLibraryRecording");
const eventPlaylistPanel = element("eventPlaylistSettings");
const eventPlaylistForm = element("eventPlaylistForm");
const eventPlaylistStatus = element("eventPlaylistStatus");
const playlistOptions = ["enabled", "autoplay_next", "loop"];
const playlistOption = (name) => eventPlaylistForm.elements.namedItem(name);
const eventPlayerPanel = element("eventPlayerSettings");
const eventPlayerForm = element("eventPlayerForm");
const eventPlayerStatus = element("eventPlayerStatus");
const playerOption = (name) => eventPlayerForm.elements.namedItem(name);
const appearanceFields = element("eventAppearanceOptions");
const playPosition = eventPlayerForm.elements.namedItem("play_position");
const previewWaiting = waitingPlayer(eventPlayerPanel);
const eventLogo = element("eventLogoPreview");
const playerArtwork = { poster: button("removeEventPoster"), logo: button("removeEventLogo") };
const artworkFields = ["poster", "logo"];
const displaySwitches = ["show_schedule", "show_recurring", "show_latest", "show_live_badge", "show_viewers"];
let playerOptions;
const artworkPreview = { poster: "", logo: "" };
const removedArtwork = new Set();
let replayCaptionRevision = "";
const destinations = element("eventDestinations");
const destinationList = element("destinationList");
const destinationForm = element("addDestination");
const destinationStatus = element("destinationStatus");
const encoder = element("encoderSettings");
const key = input("encoderKey");
const previewEncoder = input("privateEncoderPreview");
const encoderPreviewPanel = element("encoderPreview");
const encoderPreviewStatus = element("encoderPreviewStatus");
const encoderPreviewVideo = element("encoderPreviewVideo");
const releasePreview = button("releaseEncoderPreview");
const PreviewHls = window.Hls;
let previewPlayer;
let previewOpened = 0;
let checkingPreview = false;
const starting = input("calendarFirst"), ending = input("calendarLast");
const programPanel = element("eventProgramPanel");
const programChoice = element("programChoice");
const chatPanel = element("operatorChat");
const chatForm = element("eventChatSettings");
const chatEnabled = chatForm.elements.namedItem("enabled");
const chatInterval = chatForm.elements.namedItem("interval");
const chatMessages = element("operatorChatMessages");
const chatStatus = element("operatorChatStatus");
const root = "/api/live-events";
const publicBase = document.body.dataset.publicUrl || location.origin;
let calendar;
let active;
let editing;
let registrationEvent;
let pending = false;
let program;
let programSources = [];
credential.value = sessionStorage.getItem("cueporchOperator") || "";
async function request(path, method = "GET", body) {
    const headers = { Authorization: "Bearer " + credential.value };
    const multipart = body instanceof FormData;
    if (body && !multipart)
        headers["Content-Type"] = "application/json";
    const result = await fetch(path, { method, headers, body: multipart ? body : body ? JSON.stringify(body) : undefined });
    if (!result.ok) {
        const detail = new DOMParser().parseFromString(await result.text(), "text/html").querySelector("p")?.textContent;
        throw new Error(detail || `Request failed (${result.status})`);
    }
    return result.status === 204 ? undefined : result.json();
}
async function action(work, message = "") {
    if (pending)
        return;
    pending = true;
    status.textContent = "";
    workspace.inert = true;
    try {
        await work();
        status.textContent = message;
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        pending = false;
        workspace.inert = false;
    }
}
function mediaPath(suffix) {
    return `${root}/${active.event.id}/${active.instance.id}/${suffix}`;
}
function stopEncoderPreview() {
    previewPlayer?.destroy();
    previewPlayer = undefined;
    if (previewOpened) {
        encoderPreviewVideo.removeAttribute("src");
        encoderPreviewVideo.load();
    }
    previewOpened = 0;
    encoderPreviewVideo.hidden = true;
}
function showEncoderPreview(open) {
    stopEncoderPreview();
    encoderPreviewPanel.hidden = !open;
    releasePreview.disabled = true;
    encoderPreviewStatus.textContent = open ? "Waiting for the private encoder signal." : "";
    if (open)
        void refreshEncoderPreview();
}
async function refreshEncoderPreview() {
    if (!active || checkingPreview || encoderPreviewPanel.hidden)
        return;
    const selection = active;
    checkingPreview = true;
    try {
        const result = await request(mediaPath("preview"));
        if (selection !== active || encoderPreviewPanel.hidden)
            return;
        releasePreview.disabled = result.state !== "signal";
        encoderPreviewStatus.textContent = {
            waiting: "Waiting for the private encoder signal.", signal: "The private encoder signal is ready.",
            ended: "The private encoder signal has ended.", inactive: "This occurrence is no longer in preview.",
            released: "The encoder is live.",
        }[result.state];
        if (!result.address)
            stopEncoderPreview();
        else if (Date.now() - previewOpened > 25 * 60 * 1000) {
            stopEncoderPreview();
            if (PreviewHls?.isSupported()) {
                previewPlayer = new PreviewHls({ enableWorker: true, liveDurationInfinity: true });
                previewPlayer.loadSource(result.address);
                previewPlayer.attachMedia(encoderPreviewVideo);
            }
            else if (encoderPreviewVideo.canPlayType("application/vnd.apple.mpegurl")) {
                encoderPreviewVideo.src = result.address;
            }
            else {
                encoderPreviewStatus.textContent = "This browser cannot play the private preview.";
                return;
            }
            previewOpened = Date.now();
            encoderPreviewVideo.hidden = false;
        }
    }
    catch (error) {
        if (selection === active && !encoderPreviewPanel.hidden) {
            releasePreview.disabled = true;
            encoderPreviewStatus.textContent = error instanceof Error ? error.message : "Could not check the private encoder signal.";
        }
    }
    finally {
        checkingPreview = false;
    }
}
window.setInterval(() => { if (!document.hidden)
    void refreshEncoderPreview(); }, 3000);
async function downloadEventFile(path, filename) {
    const response = await fetch(path, { headers: { Authorization: "Bearer " + credential.value } });
    if (!response.ok)
        throw new Error("Could not download the event report.");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(await response.blob());
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
const { close: closeEngagement } = setupProducerEngagement(() => mediaPath("engagement"), request, (feature, filename) => action(() => downloadEventFile(mediaPath(`engagement/${feature}/export`), filename)));
const registrationEditor = element("registrationEditor");
const openRegistration = setupRegistration((suffix, method, body) => request(root + "/" + registrationEvent + suffix, method, body), async () => {
    const response = await fetch(root + "/" + registrationEvent + "/registration/export", {
        headers: { Authorization: "Bearer " + credential.value },
    });
    if (!response.ok)
        throw new Error("Could not download the registrants.");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(await response.blob());
    link.download = "cueporch-event-registrants.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});
function choose(event, instance) {
    if (registrationEvent !== event.id) {
        registrationEditor.hidden = true;
        registrationEvent = undefined;
    }
    active = { event, instance };
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
    previewEncoder.checked = event.preview_encoder;
    showEncoderPreview(instance.delivery === "preview");
    health.textContent = instance.delivery === "ready" ? "Encoder is ready." : instance.delivery.replaceAll("_", " ");
    measurements.textContent = "";
    analytics.open = false;
    analyticsRows.replaceChildren();
    analyticsStatus.textContent = "";
    captionSettings.open = false;
    captionStatus.textContent = "";
    replayCaptions.open = false;
    replayCaptionForm.reset();
    replayCaptionStatus.textContent = replayCaptionRevision = "";
    button("downloadReplayCaptions").disabled = button("saveReplayCaptions").disabled = true;
    libraryRecording.open = false;
    libraryRecordingStatus.textContent = "";
    libraryRecordingLink.hidden = saveLibraryRecording.disabled = true;
    eventPlaylistPanel.open = false;
    eventPlaylistPanel.hidden = event.event.schedule.repeat === "none";
    eventPlaylistForm.reset();
    eventPlaylistStatus.textContent = "";
    button("saveEventPlaylist").disabled = true;
    eventPlayerPanel.open = false;
    clearPlayerOptions();
    destinations.open = false;
    destinationForm.reset();
    destinationList.replaceChildren();
    destinationStatus.textContent = "";
    button("editEvent").disabled = !event.editable;
    button("prepareEncoder").disabled = !calendar.host_ready || ["withdrawing", "withdrawn", "replay"].includes(instance.delivery);
    button("prepareSrt").disabled = button("prepareEncoder").disabled;
    button("withdrawEvent").textContent = instance.delivery === "scheduled" ? "Cancel this occurrence" : "Withdraw this occurrence";
    button("withdrawEvent").disabled = instance.delivery === "withdrawn" ||
        (instance.delivery !== "scheduled" && !calendar.host_ready);
    element("productionTitle").textContent = `${event.event.title} · ${instance.local.replace("T", " ")} ${event.event.schedule.timezone}`;
    const preference = event.event.setup?.method;
    element("productionPreference").textContent = preference ?
        `Preferred streaming method: ${preference === "encoder" ? "External encoder" : "Browser studio"}` : "";
    const watch = element("eventWatch");
    const studio = element("eventStudio");
    studio.href = `/operator/live-events/${event.id}/${instance.id}/studio`;
    element("eventShare").hidden = !event.watch;
    element("eventEmbedField").hidden = !event.embed;
    if (event.watch)
        watch.href = new URL(event.watch + "?occurrence=" + instance.id, publicBase).href;
    const address = event.embed ? new URL(event.embed + "?occurrence=" + instance.id, publicBase).href : "";
    input("eventEmbedCode").value = address ? `<iframe src="${address}" title="Live event" allowfullscreen></iframe>` : "";
}
function releaseArtworkPreview(name) {
    if (artworkPreview[name])
        URL.revokeObjectURL(artworkPreview[name]);
    artworkPreview[name] = "";
}
function clearPlayerOptions() {
    playerOptions = undefined;
    removedArtwork.clear();
    for (const name of artworkFields) {
        releaseArtworkPreview(name);
        playerArtwork[name].disabled = true;
    }
    eventPlayerForm.reset();
    appearanceFields.disabled = true;
    eventPlayerStatus.textContent = "";
    button("saveEventPlayer").disabled = true;
    eventLogo.hidden = true;
    eventLogo.removeAttribute("src");
    previewWaiting(null);
}
function paintWaitingPreview() {
    if (!active || !playerOptions)
        return;
    const { event, instance } = active;
    const next = playerOption("show_schedule").checked;
    const recurring = playerOption("show_recurring").checked ? playerOptions.recurring : "";
    const poster = artworkPreview.poster || (!removedArtwork.has("poster") ? playerOptions.image : null);
    const logo = artworkPreview.logo || (!removedArtwork.has("logo") ? playerOptions.logo_image : null);
    playerArtwork.poster.disabled = !poster;
    playerArtwork.logo.disabled = !logo;
    eventLogo.hidden = !logo;
    if (logo)
        eventLogo.src = logo;
    else
        eventLogo.removeAttribute("src");
    previewWaiting(next || recurring || poster ? {
        poster, date: next ? instance.start : "", recurring,
        message: next ? `${instance.local.replace("T", " ")} (${event.event.schedule.timezone})` : "",
    } : null);
}
function showPlayerOptions(options) {
    playerOptions = options;
    removedArtwork.clear();
    for (const name of artworkFields)
        releaseArtworkPreview(name);
    eventPlayerForm.reset();
    for (const key of displaySwitches)
        playerOption(key).checked = options[key];
    playerOption("byline").value = options.byline;
    for (const key of playerSwitches)
        playerOption(key).checked = options[key];
    for (const key of playerColors)
        playerOption(key).value = options[key];
    playPosition.value = options.play_position;
    appearanceFields.disabled = !options.customize;
    playerOption("show_recurring").disabled = !options.recurring;
    button("saveEventPlayer").disabled = false;
    paintWaitingPreview();
}
eventPlayerPanel.ontoggle = () => {
    if (eventPlayerPanel.open && active && !playerOptions) {
        void action(async () => showPlayerOptions(await request(`${root}/${active.event.id}/player`)));
    }
};
eventPlayerForm.oninput = (event) => {
    if (event.target === playerOption("customize"))
        appearanceFields.disabled = !playerOption("customize").checked;
    for (const name of artworkFields) {
        if (event.target !== playerOption(name))
            continue;
        releaseArtworkPreview(name);
        const file = playerOption(name).files?.[0];
        if (file)
            artworkPreview[name] = URL.createObjectURL(file);
    }
    paintWaitingPreview();
};
for (const name of artworkFields) {
    playerArtwork[name].onclick = () => {
        removedArtwork.add(name);
        playerOption(name).value = "";
        releaseArtworkPreview(name);
        paintWaitingPreview();
    };
}
eventPlayerForm.onsubmit = (event) => {
    event.preventDefault();
    void action(async () => {
        if (!active || !playerOptions)
            return;
        const eventId = active.event.id;
        const fields = {
            byline: playerOption("byline").value,
            ...Object.fromEntries(displaySwitches.map(key => [key, playerOption(key).checked])),
            ...Object.fromEntries(playerSwitches.map(key => [key, playerOption(key).checked])),
            ...Object.fromEntries(playerColors.map(key => [key, playerOption(key).value])),
            play_position: playPosition.value,
        };
        const images = {
            poster: removedArtwork.has("poster") ? null : playerOptions.poster,
            logo: removedArtwork.has("logo") ? null : playerOptions.logo,
        };
        const files = artworkFields.map(name => ({ name, file: playerOption(name).files?.[0] }));
        for (const { name, file } of files) {
            if (!file)
                continue;
            const upload = new FormData();
            upload.append("logo", file);
            images[name] = (await request("/api/review-logos", "POST", upload)).logo;
        }
        const options = await request(`${root}/${eventId}/player`, "PUT", { ...fields, ...images });
        if (active?.event.id !== eventId)
            return;
        showPlayerOptions(options);
        eventPlayerStatus.textContent = "Event player saved.";
    });
};
function readCalendar(value) {
    calendar = value;
    element("eventsHost").hidden = value.host_ready;
    const priorPreset = preset.value;
    preset.replaceChildren(new Option("Choose an earlier event", ""), ...value.events.map((entry) => new Option(entry.event.title, entry.id)));
    preset.value = value.events.some((entry) => entry.id === priorPreset) ? priorPreset : "";
    const rows = element("eventCalendarRows");
    rows.replaceChildren();
    const sorted = value.events.flatMap((event) => event.occurrences.map((instance) => ({ event, instance })))
        .sort((a, b) => a.instance.id - b.instance.id);
    for (const { event, instance } of sorted) {
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
        if (active?.event.id === event.id && active.instance.id === instance.id)
            choose(event, instance);
    }
}
let startNew = new URL(location.href).searchParams.get("create") === "1";
async function load() {
    const query = new URLSearchParams();
    if (starting.value)
        query.set("from", starting.value);
    if (ending.value)
        query.set("to", ending.value);
    readCalendar(await request(root + "?" + query));
    sessionStorage.setItem("cueporchOperator", credential.value);
    identify.hidden = true;
    workspace.hidden = false;
    if (startNew) {
        startNew = false;
        openEditor();
    }
}
identify.onsubmit = (event) => { event.preventDefault(); void action(load); };
if (credential.value)
    void action(load);
button("calendarRefresh").onclick = () => void action(load);
function openEditor(current) {
    editing = current;
    form.reset();
    element("eventEditorHeading").textContent = current ? "Edit event" : "New event";
    element("eventSetupMode").hidden = Boolean(current);
    element("eventReuse").hidden = true;
    element("eventStartingOptions").hidden = Boolean(current);
    preset.value = "";
    preset.required = false;
    field("slug").readOnly = Boolean(current);
    const values = current?.event;
    if (values) {
        for (const name of ["title", "description", "type", "visibility", "latency"])
            field(name).value = values[name];
        field("save_replay").checked = values.save_replay;
        field("archive_replay").checked = Boolean(values.archive_replay);
        field("dvr_seconds").value = values.dvr_seconds?.toString() ?? "";
        const schedule = values.schedule;
        for (const name of ["timezone", "start", "end", "repeat"])
            field(name).value = schedule[name];
        field("interval").value = String(schedule.interval);
        field("count").value = schedule.count ? String(schedule.count) : "";
        field("until").value = schedule.until || "";
        for (const weekday of form.querySelectorAll('input[name="weekday"]')) {
            weekday.checked = schedule.weekdays.includes(Number(weekday.value));
        }
        field("slug").value = current.slug;
        field("method").value = values.setup?.method ?? "browser";
    }
    else {
        field("timezone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    }
    recurrence();
    syncArchiveOption();
    form.hidden = false;
    field("title").focus();
}
function recurrence() {
    element("recurringFields").hidden = field("repeat").value === "none";
    element("eventWeekdays").hidden = field("repeat").value !== "weeks";
}
function syncArchiveOption() {
    field("archive_replay").disabled = !field("save_replay").checked;
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
field("save_replay").onchange = syncArchiveOption;
function showStartingOptions(options) {
    field("method").value = options.method;
    field("registration").checked = options.registration;
    field("preview_encoder").checked = options.preview_encoder;
    field("chat").checked = options.chat.enabled;
    field("chat_interval").value = String(options.chat.interval);
    field("captions").checked = options.captions.enabled;
    field("caption_language").value = options.captions.language;
    field("qa").checked = options.qa.enabled;
    field("qa_anonymous").checked = options.qa.anonymous;
    field("qa_review").checked = options.qa.review;
}
function showTypeDefaults() {
    const definition = typeDefaults[field("type").value];
    field("latency").value = definition.latency;
    showStartingOptions({ ...startingOptions, method: definition.method ?? "browser",
        registration: !!definition.registration, chat: { ...startingOptions.chat, enabled: !!definition.chat },
        captions: { ...startingOptions.captions, enabled: !!definition.captions },
        qa: { ...startingOptions.qa, enabled: !!definition.qa } });
}
mode.onchange = () => {
    const reuse = mode.value === "reuse";
    element("eventReuse").hidden = !reuse;
    preset.required = reuse;
    preset.value = "";
    if (mode.value === "type")
        showTypeDefaults();
    else {
        field("latency").value = "standard";
        showStartingOptions(startingOptions);
    }
};
field("type").onchange = () => { if (!editing && mode.value === "type")
    showTypeDefaults(); };
preset.onchange = () => void action(async () => {
    const prior = calendar.events.find((entry) => entry.id === preset.value);
    if (!prior || mode.value !== "reuse")
        return;
    const options = await request(`${root}/${prior.id}/setup`);
    const value = prior.event;
    for (const name of ["title", "type", "description", "latency"])
        field(name).value = value[name];
    field("visibility").value = "private";
    field("save_replay").checked = value.save_replay;
    field("archive_replay").checked = value.archive_replay ?? true;
    syncArchiveOption();
    field("dvr_seconds").value = value.dvr_seconds?.toString() ?? "";
    field("timezone").value = value.schedule.timezone;
    showStartingOptions(options);
}, "Event settings loaded.");
form.onsubmit = (event) => {
    event.preventDefault();
    void action(async () => {
        const read = new FormData(form);
        const repeat = field("repeat").value;
        const schedule = { timezone: field("timezone").value, start: field("start").value, end: field("end").value,
            repeat, interval: Number(field("interval").value || 1), weekdays: read.getAll("weekday").map(Number),
            until: repeat !== "none" && field("until").value || null, count: repeat !== "none" && Number(field("count").value) || null };
        const target = editing ? root + "/" + editing.id : root;
        const method = editing ? "PUT" : "POST";
        const setup = editing ? undefined : {
            method: field("method").value, registration: field("registration").checked,
            preview_encoder: field("preview_encoder").checked,
            chat: { enabled: field("chat").checked, interval: Number(field("chat_interval").value) },
            captions: { enabled: field("captions").checked, language: field("caption_language").value },
            qa: { enabled: field("qa").checked, anonymous: field("qa_anonymous").checked, review: field("qa_review").checked },
        };
        const created = await request(target, method, {
            revision: editing?.revision,
            preferred_method: editing && field("method").value !== (editing.event.setup?.method ?? "browser") ? field("method").value : undefined,
            starting_options: setup, reuse_event: !editing && mode.value === "reuse" ? preset.value : undefined,
            slug: field("slug").value, title: field("title").value, description: field("description").value,
            type: field("type").value, visibility: field("visibility").value, latency: field("latency").value,
            save_replay: field("save_replay").checked,
            archive_replay: field("archive_replay").checked,
            dvr_seconds: field("dvr_seconds").value === "" ? null : field("dvr_seconds").valueAsNumber, schedule,
        });
        form.hidden = true;
        await load();
        if (created.occurrences.length)
            choose(created, created.occurrences[0]);
    }, "Event saved.");
};
previewEncoder.onchange = () => void action(async () => {
    const event = active.event;
    try {
        const value = await request(`${root}/${event.id}/encoder-settings`, "PUT", { preview: previewEncoder.checked });
        event.preview_encoder = value.preview;
    }
    finally {
        previewEncoder.checked = event.preview_encoder;
    }
}, "Encoder preview preference saved.");
button("prepareEncoder").onclick = () => void action(async () => {
    const connection = await request(mediaPath("encoder"), "POST");
    input("rtmpServer").value = connection.server;
    input("rtmpsServer").value = connection.secure_server || "";
    element("secureEncoder").hidden = !connection.secure_server;
    key.value = connection.key;
    encoder.hidden = false;
    button("withdrawEvent").textContent = "Withdraw this occurrence";
    button("withdrawEvent").disabled = false;
    showEncoderPreview(connection.preview);
    health.textContent = connection.preview ? "Encoder prepared for private preview." : "Waiting for the encoder.";
});
releasePreview.onclick = () => void action(async () => {
    await request(mediaPath("preview"), "POST");
    showEncoderPreview(false);
    active.instance.delivery = "ready";
    health.textContent = "Released. Waiting for the audience stream to start.";
}, "Private encoder released.");
button("copyEncoderKey").onclick = () => void action(async () => {
    await navigator.clipboard.writeText(key.value);
}, "Stream key copied.");
button("prepareSrt").onclick = () => void action(async () => {
    const result = await request(mediaPath("srt"), "POST", {
        interface: input("srtInterface").value, port: Number(input("srtPort").value),
    });
    input("srtServer").value = result.server;
    input("srtPassphrase").value = result.passphrase;
    element("srtSettings").hidden = false;
    button("withdrawEvent").textContent = "Withdraw this occurrence";
    button("withdrawEvent").disabled = false;
    if (result.preview)
        showEncoderPreview(true);
}, "SRT listener is ready for the encoder.");
button("stopSrt").onclick = () => void action(async () => {
    await request(mediaPath("srt"), "DELETE");
    element("srtSettings").hidden = true;
    input("srtPassphrase").value = "";
}, "SRT listener stopped.");
button("copySrtPassphrase").onclick = () => void action(async () => {
    await navigator.clipboard.writeText(input("srtPassphrase").value);
}, "SRT passphrase copied.");
async function refreshDestinations(result) {
    const selection = active;
    result = result || await request(mediaPath("destinations"));
    if (selection !== active)
        return;
    destinationForm.hidden = !result.available;
    destinationStatus.textContent = result.available ? "" : "Destinations require an event open to the public without registration.";
    const labels = {
        waiting: "Waiting for live signal", connecting: "Connecting", sending: "Sending",
        retrying: "Reconnecting", disabled: "Off",
    };
    destinationList.replaceChildren(...result.destinations.map(item => {
        const row = document.createElement("li");
        row.textContent = `${item.name} · ${item.server} · ${labels[item.state] || item.state}`;
        const control = document.createElement("button");
        control.type = "button";
        control.textContent = item.enabled ? "Turn off" : "Turn on";
        control.setAttribute("aria-label", `Turn ${item.enabled ? "off" : "on"} ${item.name}`);
        control.disabled = !item.enabled && !result.available;
        control.onclick = () => void action(async () => {
            await request(mediaPath(`destinations/${item.id}`), "PUT", { enabled: !item.enabled });
            await refreshDestinations();
        });
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Remove";
        remove.setAttribute("aria-label", `Remove ${item.name}`);
        remove.onclick = () => void action(async () => {
            await request(mediaPath(`destinations/${item.id}`), "DELETE");
            await refreshDestinations();
        });
        row.append(control, remove);
        return row;
    }));
}
destinations.ontoggle = () => { if (destinations.open)
    void action(() => refreshDestinations()); };
button("refreshDestinations").onclick = () => void action(() => refreshDestinations());
destinationForm.onsubmit = event => {
    event.preventDefault();
    const values = new FormData(destinationForm);
    void action(async () => {
        const data = { name: values.get("name"), server: values.get("server"),
            key: values.get("key"), enabled: values.has("enabled") };
        const result = await request(mediaPath("destinations"), "POST", data);
        destinationForm.reset();
        await refreshDestinations(result);
    });
};
window.setInterval(() => {
    if (destinations.open && !document.hidden && !pending) {
        void refreshDestinations().catch(() => { });
    }
}, 5000);
function renderProgram() {
    const controls = element("eventProgramSources");
    controls.replaceChildren();
    const editable = program?.editable ?? false;
    const choices = program?.choices || [];
    for (const [position, source] of programSources.entries()) {
        const item = document.createElement("li");
        const title = choices.find((choice) => choice.work === source.work && choice.cut === source.cut)?.title || "Unavailable video";
        item.append(title + " ");
        for (const [label, step] of [["Move up", -1], ["Move down", 1], ["Remove", 0]]) {
            const control = document.createElement("button");
            control.type = "button";
            control.textContent = label;
            control.setAttribute("aria-label", `${label} ${title}`);
            control.disabled = !editable || position + step < 0 || position + step >= programSources.length;
            control.onclick = () => {
                programSources.splice(position, 1);
                if (step)
                    programSources.splice(position + step, 0, source);
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
    program = await request(mediaPath("program"));
    programSources = [...program.sources];
    renderProgram();
}
async function writeProgram(sources) {
    program = await request(mediaPath("program"), "PUT", { revision: program?.revision ?? null, sources });
    programSources = [...program.sources];
    renderProgram();
}
programPanel.ontoggle = () => { if (programPanel.open)
    void action(loadProgram); };
button("refreshEventProgram").onclick = () => void action(loadProgram);
button("addProgramVideo").onclick = () => {
    const choice = program?.choices[Number(programChoice.value)];
    if (!choice || programSources.length >= 20)
        return;
    programSources.push({ work: choice.work, cut: choice.cut });
    renderProgram();
};
button("saveEventProgram").onclick = () => void action(() => writeProgram(programSources), "Program queued for the scheduled occurrence.");
button("clearEventProgram").onclick = () => void action(() => writeProgram([]), "Manual production selected.");
let chatRequest;
async function updateChat(method = "GET", body, fields = false) {
    if (!active || (method === "GET" && !fields && chatRequest))
        return;
    const path = mediaPath("chat");
    const previous = chatRequest;
    const pending = previous ? previous.then(() => readChat(path, method, body, fields)) : readChat(path, method, body, fields);
    chatRequest = pending;
    try {
        await pending;
    }
    finally {
        if (chatRequest === pending)
            chatRequest = undefined;
    }
}
async function readChat(path, method, body, fields) {
    if (mediaPath("chat") !== path || !chatPanel.open)
        return;
    try {
        const state = await request(path, method, body);
        if (mediaPath("chat") !== path || !chatPanel.open)
            return;
        if (fields) {
            chatEnabled.checked = state.enabled;
            chatInterval.value = String(state.interval);
            chatStatus.textContent = method === "PUT" ? "Chat settings saved." : "";
        }
        renderChat(chatMessages, state.messages, (entry) => [
            messageAction("Remove message", entry, () => void updateChat("POST", { type: "remove", id: entry.id })),
            messageAction("Remove and block viewer", entry, () => void updateChat("POST", { type: "ban", id: entry.id })),
        ]);
    }
    catch (error) {
        chatStatus.textContent = error instanceof Error ? error.message : String(error);
    }
}
chatPanel.ontoggle = async () => {
    if (!chatPanel.open)
        return;
    chatForm.inert = true;
    try {
        await updateChat("GET", undefined, true);
    }
    finally {
        chatForm.inert = false;
    }
};
window.setInterval(() => { if (chatPanel.open && !document.hidden)
    void updateChat(); }, 3000);
chatForm.onsubmit = (event) => {
    event.preventDefault();
    void updateChat("PUT", { enabled: chatEnabled.checked, interval: Number(chatInterval.value) }, true);
};
button("exportEventChat").onclick = () => void action(() => downloadEventFile(mediaPath("chat/export"), "cueporch-event-chat.csv"));
button("readEventHealth").onclick = () => void action(async () => {
    const result = await request(mediaPath("health"));
    const settings = result.delivery_settings;
    const lines = [result.state.replaceAll("_", " ")];
    if (typeof result.viewers === "number")
        lines.push(`${result.viewers} viewers`);
    if (["live", "waiting_for_encoder", "preparing_replay", "replay", "ended"].includes(result.state)) {
        button("withdrawEvent").textContent = "Withdraw this occurrence";
    }
    if (settings?.latency)
        lines.push(`${settings.latency} latency mode`);
    if (typeof settings?.dvr_seconds === "number")
        lines.push(settings.dvr_seconds ? `DVR up to ${settings.dvr_seconds} s` : "DVR off");
    health.textContent = lines.join(" · ");
    measurements.textContent = relaySummary(result, result.state === "live" ? "Encoder measurements unavailable from this host." : "");
});
let analyticsPending = false;
async function refreshAnalytics() {
    if (!active || analyticsPending)
        return;
    const selection = active;
    analyticsPending = true;
    try {
        const stats = await request(mediaPath("analytics"));
        if (selection !== active)
            return;
        analyticsRows.replaceChildren();
        for (const phase of ["live", "replay"]) {
            const metrics = stats[phase];
            const row = analyticsRows.insertRow();
            const title = document.createElement("th");
            title.scope = "row";
            title.textContent = phase === "live" ? "Live" : "Replay";
            row.append(title);
            for (const count of [metrics.current, metrics.peak, metrics.loads, metrics.plays]) {
                row.insertCell().textContent = count.toLocaleString();
            }
            for (const seconds of [metrics.watched_seconds, metrics.average_seconds_per_load]) {
                row.insertCell().textContent = `${seconds.toLocaleString(undefined, { maximumFractionDigits: 1 })} s`;
            }
        }
        analyticsStatus.textContent = "";
    }
    catch (error) {
        if (selection === active)
            analyticsStatus.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        analyticsPending = false;
    }
}
analytics.ontoggle = () => { if (analytics.open)
    void refreshAnalytics(); };
button("refreshEventAnalytics").onclick = () => void refreshAnalytics();
window.setInterval(() => { if (analytics.open && !document.hidden)
    void refreshAnalytics(); }, 5000);
async function updateCaptionSettings(update = false) {
    const value = { enabled: captionEnabled.checked, language: captionLanguage.value };
    const result = await request(mediaPath("captions"), update ? "PUT" : "GET", update ? value : undefined);
    captionEnabled.checked = result.enabled;
    captionLanguage.value = result.language;
    const detail = {
        waiting_for_stream: "Waiting for the event stream",
        engine_unavailable: "Connect a local TextFlowKit streaming runtime",
        listening: "Transcribing the program",
        media_or_engine_unavailable: "Waiting for usable live audio and a local transcription runtime",
        stopped: "Captioning stopped",
    };
    captionStatus.textContent = result.enabled ? `${detail[result.status] || result.status} · ${result.cue_count} lines generated`
        : "Automatic captions are off.";
}
captionSettings.ontoggle = () => { if (captionSettings.open)
    void action(() => updateCaptionSettings()); };
captionForm.onsubmit = event => { event.preventDefault(); void action(() => updateCaptionSettings(true)); };
button("refreshLiveCaptions").onclick = () => void action(() => updateCaptionSettings());
async function updateReplayCaptions(data) {
    if (data)
        data.set("revision", replayCaptionRevision);
    const result = await request(mediaPath("replay-captions"), data ? "POST" : "GET", data);
    replayCaptionRevision = result.revision;
    replayCaptionForm.elements.namedItem("language").value = result.language;
    button("downloadReplayCaptions").disabled = !result.has_track;
    button("saveReplayCaptions").disabled = !result.archived;
    const states = {
        queued: "Caption generation is queued.",
        running: "Transcribing the finished recording.",
        failed: "Could not generate captions. You can upload a subtitle file.",
        no_speech: "The caption engine did not find speech.",
        unavailable: result.archived ? "No caption track has been saved." : "The recording is still being prepared.",
    };
    replayCaptionStatus.textContent = result.has_track
        ? (result.published ? "Replay captions are ready." : "Replay captions are awaiting approval.")
        : states[result.state];
}
replayCaptions.ontoggle = () => { if (replayCaptions.open)
    void action(() => updateReplayCaptions()); };
button("refreshReplayCaptions").onclick = () => void action(() => updateReplayCaptions());
button("downloadReplayCaptions").onclick = () => void action(() => downloadEventFile(mediaPath("replay-captions") + "?download=1", "cueporch-replay-captions.vtt"));
replayCaptionForm.onsubmit = event => {
    event.preventDefault();
    const data = new FormData(replayCaptionForm);
    void action(async () => {
        await updateReplayCaptions(data);
        replayCaptionForm.elements.namedItem("track").value = "";
    });
};
async function updateLibraryRecording(save = false) {
    const result = await request(mediaPath("library-recording"), save ? "POST" : "GET");
    const states = {
        available: "You can keep a private Library copy when recording finishes.",
        unavailable: "Prepare an occurrence with replay recording enabled to save a Library copy.",
        waiting: "Waiting for the hosted replay.",
        preparing: "Copying the finished recording.",
        failed: result.error || "Could not save the recording.",
        ready: "Saved privately in the Library.",
    };
    libraryRecordingStatus.textContent = states[result.state];
    saveLibraryRecording.disabled = !["available", "failed"].includes(result.state);
    saveLibraryRecording.textContent = result.state === "failed" ? "Retry saving to Library" : "Save to Library";
    libraryRecordingLink.hidden = !result.work;
    if (result.work)
        libraryRecordingLink.href = "/operator/videos/" + result.work;
}
libraryRecording.ontoggle = () => { if (libraryRecording.open)
    void action(() => updateLibraryRecording()); };
button("refreshLibraryRecording").onclick = () => void action(() => updateLibraryRecording());
saveLibraryRecording.onclick = () => void action(() => updateLibraryRecording(true));
async function updatePlaylist(save = false) {
    const body = save ? Object.fromEntries(playlistOptions.map(name => [name, playlistOption(name).checked])) : undefined;
    const result = await request(`${root}/${active.event.id}/playlist`, save ? "PUT" : "GET", body);
    for (const name of playlistOptions)
        playlistOption(name).checked = result[name];
    button("saveEventPlaylist").disabled = false;
    eventPlaylistStatus.textContent = save ? "Replay playlist settings saved." : "";
}
eventPlaylistPanel.ontoggle = () => { if (eventPlaylistPanel.open)
    void action(() => updatePlaylist()); };
eventPlaylistForm.onsubmit = (event) => { event.preventDefault(); void action(() => updatePlaylist(true)); };
button("withdrawEvent").onclick = () => void action(async () => {
    encoder.hidden = true;
    key.value = "";
    await request(mediaPath("delivery"), "DELETE");
    await load();
}, "Occurrence withdrawn.");
