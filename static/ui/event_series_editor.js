import { checked, sessionMarker } from "./team_api.js";
import { setupSeriesPresentation } from "./event_series_presentation.js";
const byId = (id) => document.getElementById(id);
const identify = byId("seriesIdentify");
const workspace = byId("seriesWorkspace");
const status = byId("seriesStatus");
const list = byId("seriesList");
const eventChoices = byId("seriesEventChoices");
const form = byId("seriesEdit");
const editorHeading = byId("seriesEditHeading");
const previewLink = byId("seriesPreviewLink");
const watchLink = byId("seriesWatchLink");
let credential = sessionStorage.getItem("cueporchOperator") || sessionMarker;
const presentation = setupSeriesPresentation(document.querySelector("#seriesPresentation"), async (file) => {
    const data = new FormData();
    data.set("logo", file);
    const response = await checked(await fetch("/api/review-logos", {
        method: "POST",
        headers: { Authorization: "Bearer " + credential },
        body: data,
    }));
    return (await response.json()).logo;
});
async function api(path, method = "GET", body) {
    const headers = {};
    if (credential)
        headers.Authorization = `Bearer ${credential}`;
    if (body !== undefined)
        headers["Content-Type"] = "application/json";
    const response = await fetch(`/api/event-series${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    const checkedResponse = await checked(response);
    if (checkedResponse.status === 204)
        return null;
    return (await checkedResponse.json());
}
let current = null;
let choices = [];
let entries = [];
function field(name) {
    return form.elements.namedItem(name);
}
function renderEditor(entry) {
    current = entry;
    form.hidden = !entry;
    if (!entry)
        return;
    presentation.populate(entry.settings.presentation);
    editorHeading.textContent = entry.settings.title;
    field("title").value = entry.settings.title;
    field("description").value =
        entry.settings.description;
    field("slug").value = entry.slug;
    const visibilitySelect = field("visibility");
    visibilitySelect.value = entry.settings.visibility;
    const passwordRow = byId("seriesPasswordRow");
    const passwordInput = field("view_password");
    passwordInput.value = "";
    passwordInput.required =
        entry.settings.visibility === "password" && !entry.has_password;
    passwordRow.hidden = entry.settings.visibility !== "password";
    const selected = new Set(entry.settings.events);
    const ordered = [
        ...entry.settings.events.flatMap((id) => choices.filter((choice) => choice.id === id)),
        ...choices.filter((c) => !selected.has(c.id)),
    ];
    eventChoices.querySelectorAll("label").forEach((label) => label.remove());
    for (const choice of ordered) {
        const label = document.createElement("label");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.name = "events";
        input.value = choice.id;
        input.checked = selected.has(choice.id);
        const text = document.createElement("span");
        text.textContent = choice.title;
        label.append(input, text);
        eventChoices.append(label);
    }
    previewLink.hidden = true;
    previewLink.removeAttribute("href");
    watchLink.hidden = !entry.watch;
    if (entry.watch)
        watchLink.href = entry.watch;
    else
        watchLink.removeAttribute("href");
}
function renderList() {
    list.replaceChildren(...entries.map((entry) => {
        const li = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = entry.settings.title || entry.slug;
        if (current?.id === entry.id)
            button.setAttribute("aria-current", "true");
        button.onclick = () => {
            renderEditor(entry);
            list.querySelectorAll("button").forEach((b) => {
                if (b !== button)
                    b.removeAttribute("aria-current");
            });
            button.setAttribute("aria-current", "true");
        };
        li.append(button);
        return li;
    }));
}
let busy = false;
const setStatus = (msg, err = false) => {
    status.textContent = msg;
    status.classList.toggle("error", err);
};
const seriesNew = document.querySelector("#seriesNew");
const seriesDelete = document.querySelector("#seriesDelete");
const seriesPreview = document.querySelector("#seriesPreview");
{
    const visibilitySelect = field("visibility");
    const passwordRow = byId("seriesPasswordRow");
    const passwordInput = field("view_password");
    visibilitySelect.addEventListener("change", () => {
        passwordInput.value = "";
        passwordRow.hidden = visibilitySelect.value !== "password";
        passwordInput.required =
            visibilitySelect.value === "password" && !current?.has_password;
    });
}
const setBusy = (v) => {
    busy = v;
    form.inert = v;
    seriesNew.disabled = v;
    list.inert = identify.inert = v;
};
async function withBusy(fn) {
    if (busy)
        return;
    setBusy(true);
    try {
        await fn();
    }
    catch (e) {
        setStatus(e.message || "Request failed.", true);
    }
    finally {
        setBusy(false);
    }
}
async function load() {
    const { series, events } = await api("");
    workspace.hidden = false;
    identify.hidden = true;
    entries = series;
    choices = events;
    if (current)
        current = entries.find(s => s.id === current.id) ?? null;
    renderList();
    renderEditor(current);
}
void load().catch(() => { });
identify.addEventListener("submit", e => {
    e.preventDefault();
    const input = identify.querySelector('[name="credential"]');
    credential = (input?.value ?? "").trim();
    if (!credential)
        return;
    sessionStorage.setItem("cueporchOperator", credential);
    void withBusy(load);
});
form.addEventListener("submit", e => {
    e.preventDefault();
    if (!current) {
        setStatus("Select a draft first.", true);
        return;
    }
    const target = current;
    void withBusy(async () => {
        const payload = {
            slug: field("slug").value,
            view_password: field("view_password").value || undefined,
            settings: {
                title: field("title").value.trim(),
                description: field("description").value.trim(),
                visibility: field("visibility").value,
                events: Array.from(eventChoices.querySelectorAll('input[type="checkbox"]:checked')).map(el => el.value),
                presentation: await presentation.payload(),
            },
        };
        current = await api(`/${target.id}`, "PUT", {
            revision: target.revision,
            ...payload,
        });
        setStatus(current.publication === "eligible"
            ? "Event series published."
            : current.publication === "waiting"
                ? "Waiting for publication approval."
                : "Private draft saved.");
        await load();
    });
});
seriesNew.addEventListener("click", () => {
    void withBusy(async () => {
        current = await api("", "POST", {});
        setStatus("Private draft saved.");
        await load();
    });
});
seriesDelete.addEventListener("click", e => {
    e.preventDefault();
    if (busy || !current)
        return;
    const target = current;
    void withBusy(async () => {
        await api(`/${target.id}`, "DELETE", { revision: target.revision });
        current = null;
        renderEditor(null);
        await load();
    });
});
seriesPreview.addEventListener("click", e => {
    e.preventDefault();
    if (busy || !current)
        return;
    const target = current;
    void withBusy(async () => {
        const { preview } = await api(`/${target.id}/preview`, "POST");
        previewLink.href = preview;
        previewLink.hidden = false;
    });
});
