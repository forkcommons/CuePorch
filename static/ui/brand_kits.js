import { sessionMarker } from "./team_api.js";
const path = "/api/library/brand-kits";
const fields = ["primary", "accent", "icon", "text", "background", "font"];
const access = document.querySelector("#brandKitAccess");
const key = access.querySelector("input");
const status = document.querySelector("#brandKitStatus");
const editor = document.querySelector("#brandKitsWorkspace");
const list = document.querySelector("#brandKitList");
const form = document.querySelector("#brandKitForm");
const remove = document.querySelector("#deleteBrandKit");
const copyLogo = document.querySelector("#copyWorkspaceLogo");
const removeLogo = document.querySelector("#removeKitLogo");
const preview = document.querySelector("#kitLogoPreview");
const field = (name) => form.elements.namedItem(name);
const fileInput = field("logo_file");
let library = [];
let shelf;
let kit;
let logo = null;
let localLogo = null;
let busy = false;
const signedInTeam = document.body.dataset.brandKitTeam === "yes";
key.value = signedInTeam ? sessionMarker : sessionStorage.getItem("cueporchOperator") || "";
async function call(url, method = "GET", data) {
    const headers = { Authorization: "Bearer " + key.value };
    if (data && !(data instanceof FormData))
        headers["Content-Type"] = "application/json";
    const response = await fetch(url, { method, headers, body: data instanceof FormData ? data : data ? JSON.stringify(data) : undefined });
    if (!response.ok) {
        const error = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
        throw new Error(error?.textContent || `Request failed (${response.status}).`);
    }
    return response.status === 204 ? undefined : response.json();
}
async function perform(action) {
    if (busy)
        return;
    busy = true;
    status.textContent = "";
    try {
        await action();
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        busy = false;
    }
}
function setLocalLogo(source) {
    if (localLogo)
        URL.revokeObjectURL(localLogo);
    localLogo = source;
}
function renderLogo() {
    const url = localLogo || (logo && shelf.logos[logo]);
    preview.hidden = !url;
    if (url)
        preview.src = url;
    else
        preview.removeAttribute("src");
    copyLogo.hidden = !shelf.workspace_logo;
    removeLogo.hidden = !localLogo && !logo;
}
function selectClip(name, clip) {
    const select = form.elements.namedItem(name);
    select.replaceChildren(new Option("None", ""));
    for (const item of library)
        select.add(new Option(item.title, item.id));
    if (clip && !library.some(item => item.id === clip.work)) {
        select.add(new Option("Unavailable clip", clip.work, false, true));
    }
    else
        select.value = clip?.work || "";
}
function show(value) {
    kit = value;
    form.reset();
    setLocalLogo(null);
    logo = kit?.style.logo || null;
    if (kit) {
        field("name").value = kit.name;
        for (const setting of fields)
            field(setting).value = kit.style[setting];
    }
    selectClip("intro", kit?.intro || null);
    selectClip("outro", kit?.outro || null);
    renderLogo();
    remove.hidden = !kit;
    form.hidden = false;
}
async function refresh() {
    shelf = await call(path);
    list.replaceChildren();
    for (const item of shelf.kits) {
        const row = document.createElement("li");
        const open = document.createElement("button");
        open.type = "button";
        open.textContent = item.name;
        open.onclick = () => { if (!busy)
            show(item); };
        row.append(open);
        list.append(row);
    }
}
async function openEditor() {
    const items = await call("/api/library");
    library = items.filter(item => !item.asset_type || item.asset_type === "video");
    await refresh();
    if (!signedInTeam)
        sessionStorage.setItem("cueporchOperator", key.value);
    editor.hidden = false;
    access.hidden = true;
}
access.onsubmit = event => { event.preventDefault(); void perform(openEditor); };
document.querySelector("#newBrandKit").onclick = () => { if (!busy)
    show(); };
fileInput.onchange = () => {
    const file = fileInput.files?.[0];
    setLocalLogo(file ? URL.createObjectURL(file) : null);
    renderLogo();
};
copyLogo.onclick = () => {
    logo = shelf.workspace_logo;
    fileInput.value = "";
    setLocalLogo(null);
    renderLogo();
};
removeLogo.onclick = () => {
    logo = null;
    fileInput.value = "";
    setLocalLogo(null);
    renderLogo();
};
form.onsubmit = event => {
    event.preventDefault();
    void perform(async () => {
        const file = fileInput.files?.[0];
        if (file) {
            const data = new FormData();
            data.append("logo", file);
            logo = (await call("/api/review-logos", "POST", data)).logo;
        }
        const style = { ...Object.fromEntries(fields.map(name => [name, field(name).value])), logo };
        const payload = { name: field("name").value, style, intro: field("intro").value || null,
            outro: field("outro").value || null, revision: kit?.revision };
        const saved = kit ? await call(path + "/" + kit.id, "PUT", payload)
            : await call(path, "POST", payload);
        await refresh();
        show(saved);
        status.textContent = "Brand kit saved.";
    });
};
remove.onclick = () => {
    const current = kit;
    if (!current)
        return;
    void perform(async () => {
        await call(path + "/" + current.id, "DELETE", { revision: current.revision });
        await refresh();
        form.hidden = true;
        status.textContent = "Brand kit removed.";
    });
};
if (key.value)
    void perform(openEditor);
