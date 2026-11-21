const identify = document.querySelector("#identify");
const credential = document.querySelector("#appearanceKey");
const editor = document.querySelector("#appearanceEditor");
const surface = document.querySelector("#appearanceSurface");
const form = document.querySelector("#appearanceForm");
const submit = form.querySelector("[type=submit]");
const choices = document.querySelector("#selectedPreset");
const removePreset = document.querySelector("#removePreset");
const reset = document.querySelector("#resetAppearance");
const message = document.querySelector("#appearanceMessage");
const sample = document.querySelector(".appearance-preview");
const label = document.querySelector("#previewTitle");
const catalog = document.querySelector("#previewCatalog");
const image = document.querySelector("#previewLogo");
const video = document.querySelector("#appearanceVideo");
const clear = document.querySelector("#clearLogo");
const kitPicker = document.querySelector("#appearanceBrandKit");
const useKit = document.querySelector("#applyAppearanceKit");
const input = (key) => form.elements.namedItem(key);
const work = "/api/library/work/" + encodeURIComponent(document.body.dataset.work || "");
let stored;
let logo = null;
let temporaryLogo = null;
let loading = 0;
let kitShelf;
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
    return response.status === 204 ? undefined : response.json();
}
function endpoint() {
    return work + "/appearance/" + surface.value;
}
function discardLocalLogo() {
    if (temporaryLogo)
        URL.revokeObjectURL(temporaryLogo);
    temporaryLogo = null;
    input("logo_file").value = "";
}
function live() {
    for (const name of ["primary", "accent", "icon", "text", "background"]) {
        sample.style.setProperty("--viewer-" + name, input(name).value);
    }
    sample.dataset.theme = input("theme").value;
    sample.dataset.font = input("font").value;
    sample.dataset.crop = input("crop").checked ? "yes" : "no";
    sample.dataset.ambient = input("ambient").checked ? "yes" : "no";
    label.hidden = !input("show_title").checked;
    catalog.hidden = surface.value !== "watch" || !input("show_chrome").checked;
    const logoUrl = temporaryLogo || (logo && stored.logos[logo]);
    image.hidden = clear.hidden = !logoUrl;
    if (logoUrl)
        image.src = logoUrl;
    else
        image.removeAttribute("src");
}
function fill(settings) {
    discardLocalLogo();
    logo = typeof settings.logo === "string" ? settings.logo : null;
    for (const [key, value] of Object.entries(settings)) {
        const field = input(key);
        if (field) {
            if (typeof value === "boolean")
                field.checked = value;
            else
                field.value = value || "";
        }
    }
    input("preset_name").value = "";
    input("apply_page_to_library").checked = false;
    live();
}
function render(value) {
    stored = value;
    label.textContent = value.title;
    document.querySelector("#appearanceTitle").textContent = "Appearance for " + value.title;
    choices.replaceChildren(new Option("Current settings", ""), ...value.presets.map((preset) => new Option(preset.name, preset.name)));
    removePreset.disabled = true;
    for (const field of form.querySelectorAll("[data-watch-only]")) {
        field.hidden = surface.value !== "watch";
    }
    fill(value.settings);
    reset.disabled = value.is_default;
}
async function load() {
    const current = ++loading;
    const value = await request(endpoint());
    if (current !== loading)
        return;
    render(value);
}
function error(value) {
    message.textContent = value instanceof Error ? value.message : String(value);
}
async function signIn() {
    try {
        await load();
        kitShelf = await request("/api/library/brand-kits");
        for (const kit of kitShelf.kits)
            kitPicker.add(new Option(kit.name, kit.id));
        sessionStorage.setItem("cueporchOperator", credential.value);
        identify.hidden = true;
        editor.hidden = false;
        const info = await request(work);
        if (info.preview && !video.getAttribute("src")) {
            const ticket = await request(work + "/ticket", "POST", { sha256: info.preview });
            video.src = ticket.preview;
            video.hidden = false;
        }
    }
    catch (value) {
        error(value);
    }
}
async function change(action, success) {
    surface.disabled = submit.disabled = choices.disabled = true;
    try {
        await action();
        message.textContent = success;
    }
    catch (value) {
        error(value);
    }
    finally {
        surface.disabled = submit.disabled = choices.disabled = false;
    }
}
identify.onsubmit = (event) => { event.preventDefault(); void signIn(); };
surface.onchange = () => { void load().catch(error); };
choices.onchange = () => {
    const preset = stored.presets.find((entry) => entry.name === choices.value);
    removePreset.disabled = !preset;
    fill(preset?.settings || stored.settings);
};
removePreset.onclick = () => void change(async () => {
    const path = "/api/library/appearance-preset/" + surface.value + "/" + encodeURIComponent(choices.value);
    await request(path, "DELETE");
    await load();
}, "Preset removed.");
reset.onclick = () => void change(async () => {
    render(await request(endpoint(), "DELETE"));
}, "Default appearance restored.");
input("logo_file").onchange = () => {
    const file = input("logo_file").files?.[0];
    if (temporaryLogo)
        URL.revokeObjectURL(temporaryLogo);
    temporaryLogo = file ? URL.createObjectURL(file) : null;
    live();
};
kitPicker.onchange = () => { useKit.disabled = !kitPicker.value; };
useKit.onclick = () => {
    const kit = kitShelf.kits.find(option => option.id === kitPicker.value);
    if (!kit)
        return;
    discardLocalLogo();
    Object.assign(stored.logos, kitShelf.logos);
    logo = kit.style.logo;
    for (const [name, value] of Object.entries(kit.style)) {
        if (name !== "logo" && value)
            input(name).value = value;
    }
    live();
};
clear.onclick = () => { discardLocalLogo(); logo = null; live(); };
input("theme").addEventListener("change", () => {
    const dark = input("theme").value === "dark";
    if (["#112024", "#f5f5ee"].includes(input("background").value))
        input("background").value = dark ? "#112024" : "#f5f5ee";
    if (["#e5efef", "#152e33"].includes(input("text").value))
        input("text").value = dark ? "#e5efef" : "#152e33";
    live();
});
form.addEventListener("input", live);
form.onsubmit = (event) => {
    event.preventDefault();
    void change(async () => {
        const settings = {};
        for (const [key, value] of Object.entries(stored.settings)) {
            const field = input(key);
            settings[key] = field ? (typeof value === "boolean" ? field.checked : field.value) : value;
        }
        const file = input("logo_file").files?.[0];
        if (file) {
            const upload = new FormData();
            upload.append("logo", file);
            logo = (await request("/api/review-logos", "POST", upload)).logo;
        }
        settings.logo = logo;
        if (input("preset_name").value.trim())
            settings.preset_name = input("preset_name").value.trim();
        settings.apply_page_to_library = input("apply_page_to_library").checked;
        render(await request(endpoint(), "PUT", settings));
    }, "Appearance saved.");
};
if (credential.value)
    void signIn();
export {};
