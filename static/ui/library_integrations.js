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
const driveSearch = document.querySelector("#driveSearch");
const videoName = driveSearch.querySelector("input");
const destination = document.querySelector("#driveDestination");
const videos = document.querySelector("#driveVideos");
const noVideos = document.querySelector("#driveEmpty");
const more = document.querySelector("#driveMore");
const receipt = document.querySelector("#driveReceipt");
const params = new URLSearchParams(location.search);
let credential = sessionStorage.getItem("cueporchOperator") || sessionMarker;
if (params.get("team") === "1" || (params.has("drive") && sessionStorage.getItem("cueporchDriveTeam"))) {
    credential = sessionMarker;
}
sessionStorage.removeItem("cueporchDriveTeam");
let integrations = [];
let onlyConnected = false;
let cursor = null;
let query = "";
let busy = false;
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
    const controls = [...list.querySelectorAll("button"), ...picker.querySelectorAll("button")];
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
        if (item.connected) {
            if (item.configured)
                card.append(button("Browse videos", openPicker));
            card.append(button("Disconnect Google Drive", async () => {
                const result = await api("/api/integrations/google-drive/disconnect", {});
                picker.hidden = true;
                item.connected = false;
                render();
                status.textContent = result.provider_revoked
                    ? "Google Drive disconnected."
                    : "Disconnected here. You can remove CuePorch access in your Google Account.";
            }));
        }
        else if (item.configured) {
            card.append(button("Connect Google Drive", async () => {
                const result = await api("/api/integrations/google-drive/connect", {});
                if (credential === sessionMarker)
                    sessionStorage.setItem("cueporchDriveTeam", "1");
                location.assign(result.authorization_url);
            }));
        }
        else {
            const tip = document.createElement("p");
            tip.textContent = "Configure the workspace’s Google Drive OAuth app to connect.";
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
async function openPicker() {
    const folders = await api("/api/library/folders");
    const lookup = new Map(folders.map(folder => [folder.id, folder]));
    destination.replaceChildren(new Option("Library root", ""), ...folders.map(folder => new Option(folderName(folder, lookup), folder.id)));
    picker.hidden = false;
    receipt.hidden = true;
    query = videoName.value;
    await loadPage(false);
    videoName.focus();
}
async function loadPage(append) {
    const args = new URLSearchParams({ q: query });
    if (append && cursor)
        args.set("cursor", cursor);
    const page = await api(`/api/integrations/google-drive/files?${args}`);
    cursor = page.cursor;
    more.hidden = !cursor;
    if (!append)
        videos.replaceChildren();
    for (const file of page.files) {
        const row = document.createElement("li");
        row.append(button(`Import ${file.name}`, async () => {
            receipt.hidden = true;
            status.textContent = `Importing ${file.name}…`;
            const saved = await api("/api/integrations/google-drive/import", { file: file.id, folder: destination.value || null });
            const link = document.createElement("a");
            const slug = encodeURIComponent(saved.id);
            link.href = credential === sessionMarker ? `/team/video/${slug}/details` : `/operator/videos/${slug}`;
            link.textContent = file.name;
            receipt.replaceChildren("Library video: ", link, ` (${saved.visibility})`);
            receipt.hidden = false;
            status.textContent = "Import ready.";
        }));
        videos.append(row);
    }
    noVideos.hidden = videos.childElementCount > 0;
}
async function loadDirectory() {
    try {
        const result = await api("/api/integrations");
        integrations = result.integrations;
        category.replaceChildren(new Option("All categories", ""), ...[...new Set(integrations.map(item => item.category))].map(name => new Option(name)));
        directory.hidden = false;
        access.hidden = true;
        if (credential !== sessionMarker)
            sessionStorage.setItem("cueporchOperator", credential);
        render();
        if (params.get("drive") === "connected")
            status.textContent = "Google Drive is connected.";
        if (params.get("drive") === "cancelled")
            status.textContent = "Google Drive connection was cancelled.";
        if (params.has("drive"))
            history.replaceState(null, "", location.pathname);
    }
    catch (error) {
        directory.hidden = true;
        access.hidden = false;
        message(error);
    }
}
access.onsubmit = event => {
    event.preventDefault();
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
void loadDirectory();
