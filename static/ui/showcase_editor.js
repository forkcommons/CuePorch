import { setupRegistration } from "./video_registration.js";
const identify = document.querySelector("#showcaseIdentify");
const credential = identify.elements.namedItem("credential");
const workspace = document.querySelector("#showcaseWorkspace");
const editor = document.querySelector("#showcaseSetup");
const form = document.querySelector("#showcaseEdit");
const field = (name) => form.elements.namedItem(name);
const save = form.querySelector("[type=submit]");
const message = document.querySelector("#showcaseStatus");
const picker = document.querySelector("#showcaseList");
const video = document.querySelector("#showcaseVideo");
const members = document.querySelector("#showcaseMembers");
const add = document.querySelector("#showcaseAdd");
const frame = document.querySelector("#showcaseFrame");
const sharing = document.querySelector("#showcaseSharing");
const link = document.querySelector("#showcaseLink");
const code = document.querySelector("#showcaseCode");
const rokuFeed = document.querySelector("#showcaseRokuFeed");
const searchStatus = document.querySelector("#showcaseSearchStatus");
const searchTitle = document.querySelector("#showcaseSearchTitle");
const searchAddress = document.querySelector("#showcaseSearchAddress");
const searchDescription = document.querySelector("#showcaseSearchDescription");
const reportPanel = document.querySelector("#showcaseReportPanel");
const reportForm = document.querySelector("#showcaseReport");
const reportFrom = reportForm.elements.namedItem("from");
const reportTo = reportForm.elements.namedItem("to");
const reportTotals = document.querySelector("#showcaseReportTotals");
const base = document.body.dataset.publicUrl || location.origin;
const assets = [
    { key: "logo", input: field("logo_file"),
        clear: document.querySelector("#showcaseClearLogo") },
    { key: "feature_image", input: field("feature_file"),
        clear: document.querySelector("#showcaseClearFeature") },
];
const images = { logo: null, feature_image: null };
let current;
let showcases = [];
let library = [];
let selected = [];
let busy = false;
credential.value = sessionStorage.getItem("cueporchOperator") || "";
async function fetchResult(path, method = "GET", body) {
    const headers = { Authorization: "Bearer " + credential.value };
    const payload = body instanceof FormData ? body : body ? JSON.stringify(body) : undefined;
    if (body && !(body instanceof FormData))
        headers["Content-Type"] = "application/json";
    const response = await fetch(path, { method, headers, body: payload });
    if (!response.ok) {
        const explanation = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
        throw new Error(explanation?.textContent || `Request failed (${response.status})`);
    }
    return response;
}
async function request(path, method = "GET", body) {
    const response = await fetchResult(path, method, body);
    return response.status === 204 ? undefined : response.json();
}
function endpoint() { return "/api/showcases/" + encodeURIComponent(current.id); }
const registration = document.querySelector("#registrationEditor");
const openRegistration = setupRegistration((suffix, method, values) => request(endpoint() + suffix, method, values), async () => {
    const result = await fetchResult(endpoint() + "/registration/export");
    const url = URL.createObjectURL(await result.blob());
    const download = document.createElement("a");
    download.href = url;
    download.download = "cueporch-showcase-registrants.csv";
    download.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}, () => credential.value, () => {
    const selectedId = current.id;
    void request(endpoint()).then((updated) => {
        showcases = showcases.map((value) => value.id === selectedId ? { ...value, roku: updated.roku } : value);
        if (current.id === selectedId)
            renderRokuLink(updated.roku);
    }).catch(() => {
        if (current.id === selectedId)
            renderRokuLink(null);
    });
}, () => endpoint() + "/registration/mailchimp/outbound");
function renderRokuLink(path) {
    rokuFeed.value = path || "";
    rokuFeed.parentElement.hidden = !path;
}
function renderSearchPreview() {
    const visible = field("visibility").value === "public";
    const indexable = field("indexable").checked;
    searchStatus.textContent = !visible ? "Indexing requires a public showcase."
        : indexable ? "Preview of the metadata saved for search engines." : "Search indexing is off.";
    const title = field("seo_title").value.trim() || field("title").value.trim();
    searchTitle.textContent = title ? title + " · CuePorch" : "";
    searchAddress.textContent = new URL("/showcases/" + encodeURIComponent(field("slug").value), base).href;
    searchDescription.textContent = field("seo_description").value.trim() || field("description").value.trim();
}
function membership() {
    const included = new Set(selected);
    const titles = new Map(library.map((work) => [work.id, work.title]));
    video.replaceChildren(...library.filter((work) => !included.has(work.id)).map((work) => new Option(work.title, work.id)));
    add.disabled = video.options.length === 0;
    members.replaceChildren(...selected.map((id, index) => {
        const row = document.createElement("li");
        const title = titles.get(id) || "Unavailable video";
        row.append(document.createTextNode(title + " "));
        function button(text, disabled, handle) {
            const control = document.createElement("button");
            control.type = "button";
            control.textContent = text;
            control.setAttribute("aria-label", text + " " + title);
            control.disabled = disabled;
            control.onclick = () => { handle(); membership(); };
            row.append(control);
        }
        button("Move up", index === 0, () => { selected.splice(index, 1); selected.splice(index - 1, 0, id); });
        button("Move down", index === selected.length - 1, () => { selected.splice(index, 1); selected.splice(index + 1, 0, id); });
        button("Remove", false, () => { selected.splice(index, 1); });
        return row;
    }));
}
function visibility() {
    const mode = field("visibility").value;
    document.querySelector("#showcasePassword").hidden = mode !== "password";
    document.querySelector("#showcaseDomains").hidden = mode === "private";
}
function select(value) {
    if (current?.id !== value.id) {
        registration.hidden = true;
        reportPanel.hidden = true;
    }
    current = value;
    picker.value = value.id;
    for (const [name, setting] of Object.entries(value.settings)) {
        const input = field(name);
        if (!input)
            continue;
        if (typeof setting === "boolean")
            input.checked = setting;
        else
            input.value = Array.isArray(setting) ? setting.join("\n") : setting || "";
    }
    field("slug").value = value.slug;
    field("password").value = "";
    field("password").placeholder = value.has_password ? "Keep current password" : "";
    for (const { key, input, clear } of assets) {
        input.value = "";
        images[key] = value.settings[key];
        clear.hidden = !images[key];
    }
    selected = [...value.settings.videos];
    frame.hidden = true;
    frame.removeAttribute("src");
    sharing.hidden = !value.watch && !value.embed;
    link.value = value.watch ? new URL(value.watch, base).href : "";
    code.value = value.embed ? `<iframe src="${new URL(value.embed, base).href}" title="Video showcase" width="960" height="640" allowfullscreen></iframe>` : "";
    link.parentElement.hidden = !value.watch;
    code.parentElement.hidden = !value.embed;
    renderRokuLink(value.roku);
    visibility();
    renderSearchPreview();
    membership();
    editor.hidden = false;
}
function updateList(focus) {
    picker.replaceChildren(...showcases.map((value) => new Option(value.settings.title, value.id)));
    const value = showcases.find((entry) => entry.id === focus) || showcases[0];
    if (value)
        select(value);
    else
        editor.hidden = true;
}
async function action(work, success = "") {
    if (busy)
        return;
    busy = true;
    message.textContent = "";
    picker.disabled = save.disabled = true;
    const actions = Array.from(document.querySelectorAll("[data-showcase-action]"));
    for (const button of actions)
        button.disabled = true;
    try {
        await work();
        message.textContent = success;
    }
    catch (error) {
        message.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        busy = picker.disabled = save.disabled = false;
        for (const button of actions)
            button.disabled = false;
    }
}
async function signIn() {
    showcases = await request("/api/showcases");
    library = (await request("/api/library")).filter((work) => work.asset_type === "video");
    sessionStorage.setItem("cueporchOperator", credential.value);
    updateList(new URLSearchParams(location.search).get("select") || undefined);
    workspace.hidden = false;
    identify.hidden = true;
}
identify.onsubmit = (event) => { event.preventDefault(); void action(signIn); };
if (credential.value)
    void action(signIn);
picker.onchange = () => updateList(picker.value);
field("visibility").onchange = visibility;
form.addEventListener("input", renderSearchPreview);
add.onclick = () => { selected.push(video.value); membership(); };
for (const { key, input, clear } of assets) {
    input.onchange = () => { clear.hidden = !images[key] && !input.files?.length; };
    clear.onclick = () => { images[key] = null; input.value = ""; clear.hidden = true; };
}
const on = (id, work, success = "") => {
    document.querySelector("#" + id).onclick = () => void action(work, success);
};
async function addShowcase(path) {
    const value = await request(path, "POST");
    showcases.unshift(value);
    updateList(value.id);
}
on("showcaseNew", () => addShowcase("/api/showcases"), "Showcase created.");
on("showcaseDuplicate", () => addShowcase(endpoint()), "A private copy was created.");
on("showcaseDelete", async () => {
    await request(endpoint(), "DELETE");
    showcases = showcases.filter((value) => value.id !== current.id);
    updateList();
}, "Showcase deleted. Its videos remain in the library.");
on("showcaseRegistration", openRegistration);
function reportPath(filters) {
    return endpoint() + "/analytics" + (filters ? "?" + filters.toString() : "");
}
function reportDates() {
    return new URLSearchParams({ from: reportFrom.value, to: reportTo.value });
}
async function showReport(filters) {
    const stats = await request(reportPath(filters));
    reportFrom.value = stats.date_from;
    reportTo.value = stats.date_to;
    const totals = stats.totals;
    reportTotals.textContent = `${totals.impressions.toLocaleString()} impressions · ${totals.views.toLocaleString()} plays · `
        + `${totals.finishes.toLocaleString()} finishes · ${totals.watched_seconds.toLocaleString()} seconds watched`;
    reportPanel.hidden = false;
}
on("showcaseAnalytics", () => showReport());
reportForm.onsubmit = (event) => {
    event.preventDefault();
    void action(() => showReport(reportDates()));
};
on("showcaseAnalyticsCsv", async () => {
    const filters = reportDates();
    filters.set("format", "csv");
    const response = await fetchResult(reportPath(filters));
    const url = URL.createObjectURL(await response.blob());
    const download = document.createElement("a");
    download.href = url;
    download.download = "cueporch-showcase-analytics.csv";
    download.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
});
on("showcasePreview", async () => {
    const value = await request(endpoint() + "/preview", "POST");
    frame.hidden = false;
    frame.src = value.preview;
});
const viewport = document.querySelector("#showcaseViewport");
viewport.onchange = () => { frame.dataset.width = viewport.value; };
form.onsubmit = (event) => {
    event.preventDefault();
    void action(async () => {
        for (const { key, input } of assets) {
            if (!input.files?.length)
                continue;
            const file = new FormData();
            file.set("logo", input.files[0]);
            images[key] = (await request("/api/review-logos", "POST", file)).logo;
        }
        const settings = { ...current.settings, ...images, videos: selected,
            domains: field("domains").value.split(/\s+/).filter(Boolean) };
        for (const [name, old] of Object.entries(settings)) {
            if (name === "domains" || name === "videos" || name === "logo" || name === "feature_image")
                continue;
            const control = field(name);
            settings[name] = typeof old === "boolean" ? control.checked : control.value;
        }
        const updated = await request(endpoint(), "PUT", { revision: current.revision, slug: field("slug").value,
            settings, password: field("password").value });
        showcases = showcases.map((value) => value.id === updated.id ? updated : value);
        updateList(updated.id);
    }, "Showcase saved.");
};
