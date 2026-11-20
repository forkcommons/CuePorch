const identify = document.querySelector("#identify");
const credential = document.querySelector("#languageKey");
const status = document.querySelector("#languagesStatus");
const title = document.querySelector("#languagesTitle");
const pane = document.querySelector("#languagesEditor");
const original = document.querySelector("#sourceLanguage");
const upload = document.querySelector("#uploadSubtitle");
const tracks = document.querySelector("#subtitleTracks");
const editor = document.querySelector("#subtitleEditor");
const editingTitle = document.querySelector("#selectedTrack");
const cues = document.querySelector("#cueRows");
const count = document.querySelector("#transcriptCount");
const search = document.querySelector("#transcriptSearch");
const preview = document.querySelector("#languagePreview");
const previous = document.querySelector("#previousCues");
const next = document.querySelector("#nextCues");
const save = document.querySelector("#saveCues");
const generate = document.querySelector("#generateCaptions");
const generationStatus = document.querySelector("#captionJobStatus");
const endpoint = "/api/library/work/" + encodeURIComponent(document.body.dataset.work || "");
const languages = endpoint + "/languages";
const audio = endpoint + "/audio-tracks";
const audioForm = document.querySelector("#uploadAudio");
const audioSubmit = audioForm.querySelector("button[type=submit]");
const cancelReplacement = document.querySelector("#cancelAudioReplacement");
const audioList = document.querySelector("#audioTracks");
const audioStatus = document.querySelector("#audioUploadStatus");
const audioLanguage = audioForm.elements.namedItem("language");
const audioType = audioForm.elements.namedItem("kind");
const audioFile = audioForm.elements.namedItem("track");
const perPage = 30;
let state;
let selected = null;
let draft = [];
let track;
let page = 0;
let captionPoll;
let refreshedJob = null;
let audioState;
let replacing = null;
let audioBusy = false;
credential.value = sessionStorage.getItem("cueporchOperator") || "";
async function request(path, method = "GET", body) {
    const headers = { Authorization: "Bearer " + credential.value };
    if (body && !(body instanceof FormData))
        headers["Content-Type"] = "application/json";
    const response = await fetch(path, {
        method, headers, body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
        const error = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
        throw new Error(error?.textContent || `Request failed (${response.status})`);
    }
    return response;
}
async function json(path, method = "GET", body) {
    return (await request(path, method, body)).json();
}
function button(label) {
    const control = document.createElement("button");
    control.type = "button";
    control.textContent = label;
    return control;
}
async function audioAction(callback, done = "") {
    if (audioBusy)
        return;
    audioBusy = true;
    audioSubmit.disabled = audioFile.disabled = true;
    audioStatus.textContent = "";
    try {
        await callback();
        audioStatus.textContent = done;
    }
    catch (error) {
        audioStatus.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        audioBusy = false;
        audioSubmit.disabled = audioFile.disabled = false;
    }
}
function audioReplacement(track) {
    replacing = track;
    audioForm.reset();
    audioLanguage.disabled = audioType.disabled = Boolean(track);
    audioSubmit.textContent = track ? "Replace audio track" : "Upload audio";
    cancelReplacement.hidden = !track;
    if (track) {
        audioLanguage.value = track.language;
        audioType.value = track.kind;
        audioFile.focus();
    }
}
function showAudio(updated) {
    audioState = updated;
    audioForm.hidden = updated.available === false;
    audioList.replaceChildren();
    if (!updated.tracks.length) {
        const empty = document.createElement("li");
        empty.textContent = updated.available === false ? "This video has no local playback copy." : "No alternate audio has been added.";
        audioList.append(empty);
    }
    for (const track of updated.tracks) {
        const path = `${audio}/${encodeURIComponent(track.kind)}/${encodeURIComponent(track.language)}`;
        const row = document.createElement("li");
        const type = Array.from(audioType.options).find(option => option.value === track.kind)?.text || track.kind;
        const label = `${track.language} ${type.toLowerCase()}`;
        const replace = button(`Replace ${label}`);
        const download = button(`Download ${label}`);
        const remove = button(`Remove ${label}`);
        replace.onclick = () => { if (!audioBusy)
            audioReplacement(track); };
        download.onclick = () => void audioAction(async () => {
            const uri = URL.createObjectURL(await (await request(path)).blob());
            const link = document.createElement("a");
            link.href = uri;
            link.download = `${track.language}-${track.kind}.${track.format}`;
            link.click();
            setTimeout(() => URL.revokeObjectURL(uri), 1000);
        });
        remove.onclick = () => void audioAction(async () => {
            showAudio(await json(path, "DELETE", { version: audioState.version, revision: track.revision }));
            audioReplacement(null);
        }, "Audio track removed.");
        row.append(`${track.language} · ${type} `, replace, " ", download, " ", remove);
        audioList.append(row);
    }
}
cancelReplacement.onclick = () => { if (!audioBusy)
    audioReplacement(null); };
audioForm.onsubmit = event => {
    event.preventDefault();
    if (!audioState || audioBusy)
        return;
    const body = new FormData(audioForm);
    body.set("language", audioLanguage.value);
    body.set("kind", audioType.value);
    body.set("version", audioState.version);
    if (replacing)
        body.set("revision", replacing.revision);
    void audioAction(async () => {
        showAudio(await json(audio, "POST", body));
        audioReplacement(null);
    }, "Audio track saved.");
};
async function action(callback, done = "") {
    status.textContent = "";
    try {
        await callback();
        status.textContent = done;
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
}
function present(updated) {
    state = updated;
    title.textContent = "Languages for " + state.title;
    original.elements.namedItem("language").value = state.original_language;
    tracks.replaceChildren();
    if (!state.tracks.length) {
        const blank = document.createElement("li");
        blank.textContent = "No subtitles have been added.";
        tracks.append(blank);
    }
    for (const entry of state.tracks) {
        const path = languages + "/" + encodeURIComponent(entry.language);
        const row = document.createElement("li");
        const name = document.createElement("strong");
        name.textContent = `${entry.language} · ${entry.kind}`;
        const edit = button("Edit " + entry.language);
        const download = button("Download " + entry.language);
        const remove = button("Remove " + entry.language);
        edit.onclick = () => void action(() => openTrack(entry.language));
        download.onclick = () => void action(async () => {
            const blob = await (await request(path + "?download=1")).blob();
            const uri = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = uri;
            link.download = entry.language + ".vtt";
            link.click();
            setTimeout(() => URL.revokeObjectURL(uri), 1000);
        });
        remove.onclick = () => void action(async () => {
            const latest = await json(path);
            present(await json(path, "DELETE", { version: latest.version, revision: latest.revision }));
            if (selected === entry.language) {
                selected = null;
                editor.hidden = true;
            }
        }, "Subtitle track removed.");
        row.append(name);
        if (entry.provenance)
            row.append(" · " + entry.provenance);
        row.append(" ", edit, download, remove);
        tracks.append(row);
    }
}
function formatted(ms) {
    const fraction = String(ms % 1000).padStart(3, "0");
    const seconds = Math.floor(ms / 1000);
    const parts = [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60];
    return parts.map((value) => String(value).padStart(2, "0")).join(":") + "." + fraction;
}
function timestamp(text) {
    const value = /^(?:(\d{1,3}):)?([0-5]\d):([0-5]\d)[.,](\d{3})$/.exec(text);
    if (!value)
        throw new Error("Use timestamps such as 00:00:01.500.");
    const [, hours, minutes, seconds, millis] = value;
    return 1000 * (3600 * Number(hours || 0) + 60 * Number(minutes) + Number(seconds)) + Number(millis);
}
function renderDraft() {
    const query = search.value.trim().toLocaleLowerCase();
    const matches = draft.map((cue, index) => ({ cue, index })).filter(({ cue }) => !query
        || [cue.start, cue.end, cue.text].some((value) => value.toLocaleLowerCase().includes(query)));
    const lastPage = Math.max(0, Math.ceil(matches.length / perPage) - 1);
    page = Math.min(page, lastPage);
    const results = matches.slice(page * perPage, (page + 1) * perPage);
    count.textContent = matches.length === 0 ? "No matching cues."
        : `Showing ${page * perPage + 1}–${page * perPage + results.length} of ${matches.length} cues.`;
    previous.disabled = page === 0;
    next.disabled = page === lastPage;
    cues.replaceChildren();
    for (const { cue, index } of results) {
        const line = document.createElement("li");
        line.value = index + 1;
        for (const [part, word] of [["start", "Start"], ["end", "End"]]) {
            const label = document.createElement("label");
            label.textContent = word;
            const control = document.createElement("input");
            control.value = cue[part];
            control.oninput = () => { cue[part] = control.value; };
            label.append(control);
            line.append(label);
        }
        const label = document.createElement("label");
        label.textContent = "Cue text";
        const text = document.createElement("textarea");
        text.value = cue.text;
        text.maxLength = 4000;
        text.rows = 2;
        text.oninput = () => { cue.text = text.value; };
        label.append(text);
        line.append(label);
        if (!preview.hidden) {
            const jump = button("Jump to cue");
            jump.onclick = () => void action(async () => {
                preview.currentTime = timestamp(cue.start) / 1000;
            });
            line.append(jump);
        }
        const remove = button("Remove cue");
        remove.onclick = () => { draft.splice(index, 1); renderDraft(); };
        line.append(remove);
        cues.append(line);
    }
}
async function openTrack(language) {
    const current = await json(languages + "/" + encodeURIComponent(language));
    selected = language;
    track = current;
    draft = track.cues.map(({ start, end, ...fields }) => ({ ...fields, start: formatted(start), end: formatted(end) }));
    page = 0;
    search.value = "";
    editingTitle.textContent = "Edit " + language + " subtitles";
    editor.hidden = false;
    renderDraft();
}
async function renderJobs(data) {
    clearTimeout(captionPoll);
    if (!data)
        data = await json(endpoint + "/caption-jobs");
    const latest = data.jobs[0];
    const running = latest && ["queued", "running", "waiting"].includes(latest.state);
    generate.disabled = !data.available || !state.original_language || Boolean(running);
    const messages = {
        queued: "Subtitle generation queued.",
        running: "TextFlowKit is generating subtitles.",
        waiting: "Waiting for caption approval.",
        complete: "TextFlowKit subtitles are ready.",
        superseded: "A later edit or video version replaced this job.",
        rejected: "Subtitle generation was rejected.",
        failed: "TextFlowKit could not generate subtitles.",
    };
    generationStatus.textContent = latest ? (latest.error || messages[latest.state] || "")
        : data.available ? (state.original_language ? "TextFlowKit is ready to caption this cut."
            : "Set the original language to prepare automatic subtitles.")
            : "Set up local TextFlowKit to generate subtitles. You can still upload subtitle files.";
    if (running) {
        captionPoll = window.setTimeout(() => void renderJobs().catch((error) => { generationStatus.textContent = String(error); }), 1000);
    }
    else if (latest?.state === "complete" && refreshedJob !== latest.id) {
        refreshedJob = latest.id;
        present(await json(languages));
    }
}
async function load() {
    present(await json(languages));
    sessionStorage.setItem("cueporchOperator", credential.value);
    identify.hidden = true;
    pane.hidden = false;
    void audioAction(async () => { showAudio(await json(audio)); });
    await renderJobs();
    const info = await json(endpoint);
    if (info.preview) {
        const ticket = await json(endpoint + "/ticket", "POST", { sha256: info.preview });
        preview.src = ticket.preview;
        preview.hidden = false;
    }
}
identify.onsubmit = (event) => { event.preventDefault(); void action(load); };
original.onsubmit = (event) => {
    event.preventDefault();
    void action(async () => {
        const settings = new FormData(original);
        const language = settings.get("language");
        present(await json(languages, "PUT", { language, generate: settings.has("generate"), version: state.version }));
        await renderJobs();
    }, "Original language saved.");
};
upload.onsubmit = (event) => {
    event.preventDefault();
    void action(async () => {
        const body = new FormData(upload);
        body.append("version", state.version);
        present(await json(languages, "POST", body));
        upload.reset();
        await openTrack(String(body.get("language")).toLowerCase());
    }, "Subtitles uploaded.");
};
search.oninput = () => { page = 0; renderDraft(); };
previous.onclick = () => { --page; renderDraft(); };
next.onclick = () => { ++page; renderDraft(); };
document.querySelector("#addCue").onclick = () => {
    const start = Math.floor((preview.hidden ? 0 : preview.currentTime) * 1000);
    draft.push({ start: formatted(start), end: formatted(start + 1000), text: "", settings: "" });
    search.value = "";
    page = Math.floor((draft.length - 1) / perPage);
    renderDraft();
    cues.lastElementChild?.querySelector("textarea")?.focus();
};
save.onclick = () => void action(async () => {
    if (!selected)
        return;
    const payload = {
        version: track.version, revision: track.revision,
        cues: draft.map(({ start, end, ...fields }) => ({ ...fields, start: timestamp(start), end: timestamp(end) })),
    };
    present(await json(languages + "/" + encodeURIComponent(selected), "PUT", payload));
    await openTrack(selected);
}, "Subtitles saved.");
generate.onclick = () => void action(async () => {
    const outcome = await json(endpoint + "/caption-jobs", "POST", { version: state.version });
    await renderJobs(outcome);
}, "Subtitle generation requested.");
if (credential.value)
    void action(load);
export {};
