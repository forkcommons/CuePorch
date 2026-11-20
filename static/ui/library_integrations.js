import { checked, sessionMarker } from "./team_api.js";
const access = document.querySelector("#integrationAccess");
const key = access.querySelector("input");
const status = document.querySelector("#integrationStatus");
const directory = document.querySelector("#integrationDirectory");
const discover = document.querySelector("#discoverIntegrations");
const connected = document.querySelector("#connectedIntegrations");
const search = document.querySelector("#findIntegration");
const category = document.querySelector("#integrationCategory");
const list = document.querySelector("#integrationList");
const empty = document.querySelector("#integrationEmpty");
const picker = document.querySelector("#drivePicker");
const pickerHeading = document.querySelector("#drivePickerHeading");
const remoteFolder = document.querySelector("#cloudFolder");
const remoteUp = document.querySelector("#cloudFolderUp");
const remotePath = document.querySelector("#cloudFolderPath");
const driveSearch = document.querySelector("#driveSearch");
const videoName = driveSearch.querySelector("input");
const destination = document.querySelector("#driveDestination");
const videos = document.querySelector("#driveVideos");
const noVideos = document.querySelector("#driveEmpty");
const more = document.querySelector("#driveMore");
const importSelected = document.querySelector("#importCloudSelection");
const batchReceipts = document.querySelector("#cloudBatchReceipts");
const receipt = document.querySelector("#driveReceipt");
const watch = document.querySelector("#dropboxWatch");
const watchHeading = document.querySelector("#dropboxWatchHeading");
const watchDescription = document.querySelector("#watchDescription");
const watchState = document.querySelector("#dropboxWatchStatus");
const watchJobs = document.querySelector("#dropboxWatchJobs");
const enableWatch = document.querySelector("#enableDropboxWatch");
const disableWatch = document.querySelector("#disableDropboxWatch");
const refreshWatch = document.querySelector("#refreshDropboxWatch");
const params = new URLSearchParams(location.search);
const personal = document.body.dataset.personal === "1";
let credential = sessionMarker;
if (!personal) {
    credential = sessionStorage.getItem("cueporchOperator") || sessionMarker;
    if (params.get("team") === "1" || (params.has("connection") && sessionStorage.getItem("cueporchCloudTeam"))) {
        credential = sessionMarker;
    }
    sessionStorage.removeItem("cueporchCloudTeam");
}
let integrations = [];
let onlyConnected = false;
let cursor = null;
let query = "";
let busy = false;
let active;
let source = [];
let currentWatch;
const recentFolders = new Map();
const selectedFiles = new Map();
function withPersonal(path) {
    if (!personal)
        return path;
    const url = new URL(path, location.origin);
    url.searchParams.set("personal", "1");
    return `${url.pathname}${url.search}`;
}
function pathFor(item, action) {
    return withPersonal(`/api/integrations/${encodeURIComponent(item.id)}/${action}`);
}
function videoLink(work, title) {
    const link = document.createElement("a");
    const slug = encodeURIComponent(work);
    link.href = credential === sessionMarker ? `/team/video/${slug}/details` : `/operator/videos/${slug}`;
    link.textContent = title;
    return link;
}
function message(error) {
    status.textContent = error instanceof Error ? error.message : String(error);
}
async function api(path, body) {
    const headers = { Authorization: `Bearer ${credential}` };
    if (body) {
        headers["Content-Type"] = "application/json";
        headers["X-CuePorch-Integration"] = "1";
    }
    const response = await checked(await fetch(path, {
        method: body ? "POST" : "GET", headers, body: body ? JSON.stringify(body) : undefined,
    }));
    return response.json();
}
function button(label, action) {
    const control = document.createElement("button");
    control.type = "button";
    control.textContent = label;
    control.onclick = () => void run(action);
    return control;
}
async function run(action) {
    if (busy)
        return;
    busy = true;
    status.textContent = "";
    const controls = [...list.querySelectorAll("button"),
        ...picker.querySelectorAll("button,input[type=checkbox]")];
    for (const control of controls)
        control.disabled = true;
    try {
        await action();
    }
    catch (error) {
        message(error);
    }
    finally {
        busy = false;
        for (const control of controls)
            control.disabled = false;
    }
}
function updateSelection() {
    importSelected.hidden = selectedFiles.size === 0;
    importSelected.textContent = `Import selected videos (${selectedFiles.size})`;
    for (const element of videos.querySelectorAll("input[data-cloud-file]")) {
        element.checked = selectedFiles.has(element.dataset.cloudFile);
    }
}
function importFile(file, folder) {
    return api(pathFor(active, "import"), { file: file.id, folder });
}
function render() {
    discover.setAttribute("aria-pressed", String(!onlyConnected));
    connected.setAttribute("aria-pressed", String(onlyConnected));
    const words = search.value.toLowerCase().trim().split(/\s+/);
    const cards = integrations.filter(item => (!onlyConnected || item.connected)
        && (!category.value || category.value === item.category)
        && words.every(word => `${item.name} ${item.description}`.toLowerCase().includes(word)));
    empty.hidden = cards.length > 0;
    list.replaceChildren(...cards.map(item => {
        const card = document.createElement("li");
        const name = document.createElement("h2");
        name.textContent = item.name;
        const description = document.createElement("p");
        description.textContent = item.description;
        const state = document.createElement("p");
        state.textContent = item.connected ? "Connected" : item.configured ? "Available" : "Setup required";
        card.append(name, description, state);
        if (item.id === "editor-exports") {
            if (item.configured || item.connected) {
                card.append(button("Manage editor exports", () => openPicker(item)));
            }
            else {
                const setup = document.createElement("span");
                setup.textContent = "Configure CUEPORCH_EDITOR_EXPORT_FOLDER on the CuePorch service.";
                card.append(setup);
            }
        }
        else if (item.manage_path) {
            const manage = document.createElement("a");
            manage.href = item.manage_path;
            manage.textContent = "Open Library to manage Slack";
            card.append(manage);
        }
        else if (item.connected) {
            if (item.configured) {
                if (item.id === "hubspot") {
                    const live = document.createElement("a");
                    live.href = "/operator/live-events";
                    live.textContent = "Open webinar attendees";
                    card.append(live);
                }
                else if (item.publishing) {
                    const library = document.createElement("a");
                    library.href = personal ? "/team" : "/operator/library";
                    library.textContent = "Choose a video in Library";
                    card.append(library);
                }
                else
                    card.append(button(item.meeting ? "Browse recordings" : "Browse videos", () => openPicker(item)));
            }
            card.append(button(`Disconnect ${item.name}`, async () => {
                const result = await api(pathFor(item, "disconnect"), {});
                if (active === item)
                    picker.hidden = true;
                item.connected = false;
                render();
                status.textContent = result.provider_revoked
                    ? `${item.name} disconnected.`
                    : `Disconnected here. You can remove CuePorch access in your ${item.name} account.`;
            }));
        }
        else if (item.configured) {
            let shopifyDomain;
            if (item.name === "Shopify") {
                const label = document.createElement("label");
                label.textContent = "Shopify store domain";
                shopifyDomain = document.createElement("input");
                shopifyDomain.placeholder = "your-store.myshopify.com";
                shopifyDomain.maxLength = 130;
                shopifyDomain.autocomplete = "off";
                label.append(shopifyDomain);
                card.prepend(label);
            }
            card.append(button(`Connect ${item.name}`, async () => {
                const result = await api(pathFor(item, "connect"), shopifyDomain ? { shop: shopifyDomain.value.trim() } : {});
                if (!personal && credential === sessionMarker)
                    sessionStorage.setItem("cueporchCloudTeam", "1");
                location.assign(result.authorization_url);
            }));
        }
        else {
            const tip = document.createElement("p");
            tip.textContent = `Configure the workspace’s ${item.name} OAuth app to connect.`;
            card.append(tip);
        }
        return card;
    }));
}
function folderName(folder, lookup) {
    const names = [folder.name];
    const seen = new Set([folder.id]);
    let parent = folder.parent;
    while (parent && !seen.has(parent)) {
        const ancestor = lookup.get(parent);
        if (!ancestor)
            break;
        names.unshift(ancestor.name);
        seen.add(parent);
        parent = ancestor.parent;
    }
    return names.join(" / ");
}
async function openPicker(item) {
    const folders = await api("/api/library/folders");
    const lookup = new Map(folders.map(folder => [folder.id, folder]));
    destination.replaceChildren(new Option("Library root", ""), ...folders.map(folder => new Option(folderName(folder, lookup), folder.id)));
    active = item;
    source = [];
    selectedFiles.clear();
    batchReceipts.hidden = true;
    batchReceipts.replaceChildren();
    pickerHeading.textContent = `Import from ${active.name}`;
    remoteFolder.hidden = !active.browse_folders;
    const meeting = active.meeting;
    const editor = active.id === "editor-exports";
    driveSearch.hidden = editor;
    videos.hidden = editor;
    watch.hidden = !meeting && !editor && active.id !== "dropbox";
    watchHeading.textContent = meeting ? `Automatic ${active.name} archive` : "Dropbox auto-import";
    watchDescription.textContent = meeting
        ? "Copy generated recordings from meetings that end after you enable archiving."
        : "Copy new videos from this Dropbox folder into the selected Library destination.";
    currentWatch = { enabled: false, jobs: [] };
    picker.hidden = false;
    receipt.hidden = true;
    query = "";
    videoName.value = "";
    if (editor) {
        pickerHeading.textContent = "Editor exports";
        watchHeading.textContent = "Automatic export intake";
        watchDescription.textContent = "Copy new completed exports into the selected private Library destination.";
        noVideos.hidden = true;
        more.hidden = true;
        importSelected.hidden = true;
        const state = await api(pathFor(active, "watch"));
        destination.value = state.destination || "";
        renderWatch(state);
        destination.focus();
        return;
    }
    await loadPage(false);
    if (!watch.hidden)
        await loadWatch();
    videoName.focus();
}
async function loadPage(append) {
    const args = new URLSearchParams({ q: query, folder: source.at(-1)?.id || "" });
    if (append && cursor)
        args.set("cursor", cursor);
    const page = await api(`${pathFor(active, "files")}?${args}`);
    cursor = page.cursor;
    more.hidden = !cursor;
    if (!append)
        videos.replaceChildren();
    remoteUp.hidden = source.length === 0;
    remotePath.textContent = [active.name, ...source.map(folder => folder.name)].join(" / ");
    for (const folder of page.folders || []) {
        recentFolders.set(folder.id, folder.name);
        const row = document.createElement("li");
        row.append(button(active.id === "google-meet" ? `Open ${folder.name}` : `Open folder ${folder.name}`, () => navigateSource([...source, folder])));
        videos.append(row);
    }
    for (const file of page.files) {
        const row = document.createElement("li");
        const toggle = document.createElement("input");
        toggle.type = "checkbox";
        toggle.dataset.cloudFile = file.id;
        const label = document.createElement("label");
        label.append(toggle, ` Select ${file.name} `);
        toggle.onchange = () => {
            if (toggle.checked)
                selectedFiles.set(file.id, file);
            else
                selectedFiles.delete(file.id);
            updateSelection();
        };
        row.append(label);
        row.append(button(`Import ${file.name}`, async () => {
            receipt.hidden = true;
            status.textContent = `Importing ${file.name}…`;
            const saved = await importFile(file, destination.value || null);
            selectedFiles.delete(file.id);
            updateSelection();
            receipt.replaceChildren("Library video: ", videoLink(saved.id, file.name), ` (${saved.visibility})`);
            receipt.hidden = false;
            status.textContent = "Import ready.";
        }));
        videos.append(row);
    }
    noVideos.hidden = videos.childElementCount > 0;
    updateSelection();
    if (!watch.hidden && currentWatch)
        renderWatch(currentWatch);
}
function renderWatch(data) {
    currentWatch = data;
    const meeting = active.meeting;
    const editor = active.id === "editor-exports";
    disableWatch.hidden = !data.enabled;
    refreshWatch.hidden = !data.enabled;
    const selected = meeting || editor ? "" : source.at(-1)?.id || "";
    enableWatch.hidden = (editor && !active.configured) || (data.enabled && data.source === selected && (data.destination || "") === destination.value);
    enableWatch.textContent = meeting
        ? (data.enabled ? "Change archive destination" : "Enable automatic archive")
        : (data.enabled ? "Change watched folder or destination" : "Watch this folder");
    disableWatch.textContent = meeting ? "Stop automatic archive" : "Stop watching";
    if (editor) {
        enableWatch.textContent = data.enabled ? "Change export destination" : "Watch for exports";
        if (active.connected !== data.enabled) {
            active.connected = data.enabled;
            render();
        }
    }
    const folder = data.source
        ? recentFolders.get(data.source) || "Previously selected Dropbox folder"
        : "Dropbox root";
    const target = Array.from(destination.options).find(option => option.value === data.destination)?.text
        || (data.destination ? "Previously selected Library folder" : "Library root");
    const checked = data.checked ? ` Last checked ${new Date(data.checked * 1000).toLocaleString()}.` : "";
    const mode = editor ? "Watching the configured export folder." : meeting ? `Archiving new ${active.name} recordings.` : `Watching ${folder}.`;
    watchState.textContent = data.enabled ? `${mode} Destination: ${target}.${checked}${data.error ? " " + data.error : ""}`
        : meeting ? "Automatic archive is off." : "Folder watching is off.";
    watchJobs.replaceChildren(...data.jobs.map(job => {
        const line = document.createElement("li");
        line.append(job.work ? videoLink(job.work, job.name) : job.name, ` — ${job.state}`);
        if (job.error)
            line.append(`. ${job.error}`);
        if (job.email === "queued")
            line.append(". Completion email queued.");
        if (job.email === "accepted")
            line.append(". Completion email accepted by the relay.");
        if (job.email === "failed")
            line.append(". The relay could not send the completion email.");
        if (job.state === "failed")
            line.append(button(`Retry ${job.name}`, async () => {
                renderWatch(await api(pathFor(active, `watch/${job.id}/retry`), {}));
            }));
        return line;
    }));
}
async function loadWatch() {
    renderWatch(await api(pathFor(active, "watch")));
}
async function navigateSource(path) {
    const previous = source;
    const previousQuery = query;
    source = path;
    if (active.id === "google-meet") {
        query = "";
        videoName.value = "";
    }
    try {
        await loadPage(false);
    }
    catch (error) {
        source = previous;
        query = previousQuery;
        videoName.value = previousQuery;
        throw error;
    }
}
async function loadDirectory() {
    try {
        const result = await api(withPersonal("/api/integrations"));
        integrations = result.integrations;
        category.replaceChildren(new Option("All categories", ""), ...[...new Set(integrations.map(item => item.category))].map(name => new Option(name)));
        directory.hidden = false;
        access.hidden = true;
        if (!personal && credential !== sessionMarker)
            sessionStorage.setItem("cueporchOperator", credential);
        render();
        const completed = integrations.find(item => item.id === params.get("connection"));
        if (completed?.connected && params.get("result") === "connected") {
            status.textContent = `${completed.name} is connected.`;
        }
        if (completed && params.get("result") === "cancelled") {
            status.textContent = `${completed.name} connection was cancelled.`;
        }
        if (params.has("connection"))
            history.replaceState(null, "", location.pathname);
    }
    catch (error) {
        directory.hidden = true;
        access.hidden = personal;
        message(error);
    }
}
access.onsubmit = event => {
    event.preventDefault();
    if (personal)
        return;
    credential = key.value;
    void loadDirectory();
};
document.querySelector("#integrationTeam").onclick = () => {
    credential = sessionMarker;
    void loadDirectory();
};
discover.onclick = () => { onlyConnected = false; render(); };
connected.onclick = () => { onlyConnected = true; render(); };
search.oninput = render;
category.onchange = render;
driveSearch.onsubmit = event => {
    event.preventDefault();
    query = videoName.value;
    void run(() => loadPage(false));
};
more.onclick = () => void run(() => loadPage(true));
remoteUp.onclick = () => void run(() => navigateSource(source.slice(0, -1)));
destination.onchange = () => { if (!watch.hidden && currentWatch)
    renderWatch(currentWatch); };
refreshWatch.onclick = () => void run(loadWatch);
importSelected.onclick = () => void run(async () => {
    const files = [...selectedFiles.values()];
    const folder = destination.value || null;
    batchReceipts.replaceChildren();
    batchReceipts.hidden = false;
    receipt.hidden = true;
    let copied = 0;
    for (const [index, file] of files.entries()) {
        status.textContent = `Importing ${index + 1} of ${files.length} · ${file.name}…`;
        const result = document.createElement("li");
        try {
            const saved = await importFile(file, folder);
            selectedFiles.delete(file.id);
            result.append(videoLink(saved.id, file.name), ` (${saved.visibility})`);
            copied += 1;
        }
        catch (error) {
            result.textContent = `${file.name}: ${error instanceof Error ? error.message : String(error)}`;
        }
        batchReceipts.append(result);
        updateSelection();
    }
    const failed = files.length - copied;
    status.textContent = failed ? `Imported ${copied} of ${files.length}. ${failed} selected for retry.`
        : `Imported ${copied} of ${files.length} selected videos.`;
});
enableWatch.onclick = () => void run(async () => {
    renderWatch(await api(pathFor(active, "watch"), {
        enabled: true, source: source.at(-1)?.id || "", destination: destination.value || null,
    }));
});
disableWatch.onclick = () => void run(async () => {
    renderWatch(await api(pathFor(active, "watch"), { enabled: false }));
});
void loadDirectory();
