import { embedBuilder } from "./embed_builder.js";
import { sessionMarker } from "./team_api.js";
import { sharingFields } from "./library_privacy.js";
import { videoAIDrafts } from "./video_ai_drafts.js";
const identify = document.querySelector("#identify");
const key = document.querySelector("#operatorKey");
const status = document.querySelector("#detailsStatus");
const pane = document.querySelector("#videoDetails");
const form = document.querySelector("#videoMeta");
const submit = form.querySelector("button[type=submit]");
const facts = document.querySelector("#videoFacts");
const heading = document.querySelector("#videoHeading");
const descriptors = document.querySelector("#matureReasons");
const input = (name) => form.elements.namedItem(name);
const rating = form.elements.namedItem("rating");
const preview = document.querySelector("#workspacePreview");
const previewMessage = document.querySelector("#previewMessage");
const editorial = document.querySelector("#editorialState");
const viewer = document.querySelector("#viewerPage");
const links = document.querySelector("#workspaceLinks");
const start = document.querySelector("#shareStart");
const capture = document.querySelector("#captureTime");
const viewerLink = document.querySelector("#workspaceWatch");
const embedCode = document.querySelector("#workspaceEmbed");
const copyEmbed = document.querySelector("#copyEmbed");
const reviewLink = document.querySelector("#workspaceReview");
const copyReview = document.querySelector("#copyReview");
const custom = document.querySelector("#customLink");
const expires = document.querySelector("#customExpires");
const clearCustom = document.querySelector("#clearCustom");
const sharingMessage = document.querySelector("#sharingMessage");
const privacy = document.querySelector("#editVideoPrivacy");
const sourcePrivacy = document.querySelector("#restoreSourcePrivacy");
const builder = embedBuilder();
const teamPreview = document.body.dataset.teamPreview;
key.value = teamPreview ? sessionMarker : sessionStorage.getItem("cueporchOperator") || "";
const endpoint = "/api/library/work/" + encodeURIComponent(document.body.dataset.work || "");
let info;
let sharing;
let editorialState;
async function call(resource = "", method = "GET", body) {
    const headers = { Authorization: "Bearer " + key.value, "Content-Type": "application/json",
        ...(teamPreview ? { "X-CuePorch-Team": "1" } : {}) };
    const response = await fetch(endpoint + resource, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
        const html = new DOMParser().parseFromString(await response.text(), "text/html");
        throw new Error(html.querySelector("p")?.textContent || `Request failed (${response.status})`);
    }
    return response.status === 204 ? undefined : response.json();
}
const loadAIDrafts = videoAIDrafts(call, ({ title, description, tags }) => {
    heading.textContent = title;
    input("title").value = title;
    input("description").value = description;
    input("tags").value = tags.join("\n");
});
function contentVisibility() {
    descriptors.hidden = descriptors.disabled = rating.value !== "mature";
}
rating.onchange = contentVisibility;
function show(details) {
    heading.textContent = details.title;
    for (const name of ["title", "description", "license", "rating", "panorama"]) {
        input(name).value = details[name] || "";
    }
    for (const name of ["tags", "categories"])
        input(name).value = details[name].join("\n");
    input("embed_pages").value = details.embed_pages.join("\n");
    input("credits").value = details.credits.map(({ name, role }) => role ? `${name} | ${role}` : name).join("\n");
    input("realistic_ai").checked = details.realistic_ai;
    input("advertisement").checked = details.advertisement;
    for (const control of form.querySelectorAll("[name=content]")) {
        control.checked = details.content.includes(control.value);
    }
    contentVisibility();
    const media = details.media;
    const values = {
        Format: media?.format.toUpperCase(),
        Resolution: media?.width && media.height ? `${media.width} × ${media.height}` : "",
        "File size": media ? `${media.bytes.toLocaleString()} bytes` : "",
        "Frame rate": media?.fps ? `${media.fps} fps` : "",
        Audio: media?.audio,
        "Imported projection": details.projection,
        Duration: details.duration_seconds === null ? "" : `${details.duration_seconds} seconds`,
        Added: details.added_at ? new Date(details.added_at).toLocaleDateString() : "",
    };
    facts.replaceChildren();
    for (const [label, value] of Object.entries(values)) {
        if (!value)
            continue;
        const term = document.createElement("dt");
        term.textContent = label;
        const description = document.createElement("dd");
        description.textContent = value;
        facts.append(term, description);
    }
    if (!media) {
        const empty = document.createElement("dd");
        empty.textContent = "A media file has not been recovered for this work.";
        facts.append(empty);
    }
}
function address(path) {
    return new URL(path, document.body.dataset.publicUrl || location.origin);
}
function renderLinks() {
    const watch = sharing.alias?.path || sharing.watch || info.watch;
    const embed = sharing.embed_url || info.embed;
    const target = watch ? address(watch) : null;
    if (target && sharing.watch && start.validity.valid && start.valueAsNumber > 0) {
        target.searchParams.set("start", start.value);
    }
    viewer.hidden = !target;
    if (target)
        viewer.href = target.href;
    viewerLink.value = target?.href || "";
    links.hidden = !target && !embed;
    viewerLink.parentElement.hidden = !target;
    start.parentElement.hidden = !sharing.watch;
    document.querySelector("#copyWatch").hidden = !target;
    capture.hidden = !sharing.watch || preview.hidden;
    builder.show(embed ? address(embed) : null, heading.textContent || "Video player", Boolean(sharing.embed_url));
    custom.hidden = !sharing.watch;
    const alias = sharing.alias;
    clearCustom.hidden = !alias;
    expires.hidden = !alias;
    expires.textContent = alias ? (alias.expires
        ? (alias.path ? "Expires " : "Expired ") + new Date(alias.expires * 1000).toLocaleString()
        : "This link has no expiration.") : "";
    document.querySelector("#shareUnavailable").hidden = Boolean(target || embed);
}
function populateCustom() {
    custom.elements.namedItem("slug").value = sharing.alias?.slug || "";
    custom.elements.namedItem("days").value = String(sharing.alias?.days || 0);
    renderLinks();
}
async function loadTeamSharing() {
    const container = document.querySelector("#teamVideoPrivacy");
    if (!container)
        return;
    const fields = sharingFields(container);
    const notice = document.querySelector("#teamVideoPrivacyStatus");
    const watch = document.querySelector("#teamVideoWatch");
    function render(settings) {
        fields.populate(settings);
        watch.value = settings.watch ? address(settings.watch).href : "";
        watch.parentElement.hidden = !settings.watch;
        notice.textContent = settings.pending ? "Waiting for the configured publication approval." : "";
    }
    try {
        render(await call("/privacy"));
    }
    catch (error) {
        container.hidden = true;
        notice.textContent = String(error);
        return;
    }
    fields.form.onsubmit = async (event) => {
        event.preventDefault();
        fields.save.disabled = true;
        try {
            const updated = await call("/privacy", "PUT", fields.values());
            render(updated);
            if (!updated.pending)
                notice.textContent = "Video sharing saved.";
        }
        catch (error) {
            notice.textContent = String(error);
        }
        finally {
            fields.save.disabled = false;
        }
    };
}
async function loadWorkspace() {
    if (teamPreview) {
        editorialState = await call("/editorial-status");
        editorial.value = editorialState.status;
        editorial.disabled = false;
        preview.src = teamPreview;
        preview.hidden = false;
        await loadTeamSharing();
        return;
    }
    const [work, viewerSharing, state, reviews] = await Promise.all([
        call(), call("/share-link"), call("/editorial-status"),
        call("/review-links"),
    ]);
    info = work;
    sharing = viewerSharing;
    editorialState = state;
    editorial.value = editorialState.status;
    editorial.disabled = false;
    sourcePrivacy.hidden = !info.vimeo_source;
    privacy.hidden = !info.editable_privacy;
    const activeReview = reviews.find((link) => link.enabled && link.expires * 1000 > Date.now());
    copyReview.hidden = reviewLink.parentElement.hidden = !activeReview;
    reviewLink.value = activeReview ? address(activeReview.path).href : "";
    preview.hidden = !info.preview;
    previewMessage.hidden = Boolean(info.preview);
    if (info.preview) {
        const ticket = await call("/ticket", "POST", { sha256: info.preview });
        preview.src = ticket.preview;
    }
    populateCustom();
}
async function load() {
    try {
        show(await call("/details"));
        if (!teamPreview)
            sessionStorage.setItem("cueporchOperator", key.value);
        identify.hidden = true;
        pane.hidden = false;
        status.textContent = "";
        void loadAIDrafts();
        await loadWorkspace();
    }
    catch (error) {
        status.textContent = String(error);
    }
}
function lines(name) {
    return input(name).value.split("\n").map((value) => value.trim()).filter(Boolean);
}
identify.onsubmit = (event) => { event.preventDefault(); void load(); };
form.onsubmit = async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
        const body = {
            title: input("title").value,
            description: input("description").value,
            embed_pages: lines("embed_pages"),
            tags: lines("tags"),
            categories: lines("categories"),
            credits: lines("credits").map((value) => {
                const [name, ...role] = value.split("|");
                return { name: name.trim(), role: role.join("|").trim() };
            }),
            license: input("license").value || null,
            rating: rating.value,
            content: new FormData(form).getAll("content"),
            realistic_ai: input("realistic_ai").checked,
            advertisement: input("advertisement").checked,
            panorama: input("panorama").value,
        };
        show(await call("/details", "PUT", body));
        status.textContent = "Video details saved.";
    }
    catch (error) {
        status.textContent = String(error);
    }
    finally {
        submit.disabled = false;
    }
};
editorial.onchange = async () => {
    editorial.disabled = true;
    try {
        editorialState = await call("/editorial-status", "PUT", {
            status: editorial.value, version: editorialState.version,
        });
        status.textContent = "Editorial status saved.";
    }
    catch (error) {
        editorial.value = editorialState.status;
        status.textContent = String(error);
    }
    finally {
        editorial.disabled = false;
    }
};
start.oninput = renderLinks;
capture.onclick = () => { start.value = preview.currentTime.toFixed(3); renderLinks(); };
function copy(button, field, message) {
    button.onclick = async () => {
        try {
            await navigator.clipboard.writeText(field.value);
            sharingMessage.textContent = message;
        }
        catch {
            field.select();
            sharingMessage.textContent = "Selected for copying.";
        }
    };
}
copy(document.querySelector("#copyWatch"), viewerLink, "Viewer link copied.");
copy(copyEmbed, embedCode, "Embed code copied.");
copy(copyReview, reviewLink, "Review link copied.");
custom.onsubmit = async (event) => {
    event.preventDefault();
    const values = new FormData(custom);
    try {
        sharing = await call("/share-link", "PUT", { slug: values.get("slug"), days: Number(values.get("days")) });
        populateCustom();
        sharingMessage.textContent = "Custom link saved.";
    }
    catch (error) {
        sharingMessage.textContent = String(error);
    }
};
clearCustom.onclick = async () => {
    try {
        await call("/share-link", "DELETE");
        sharing = await call("/share-link");
        populateCustom();
        sharingMessage.textContent = "Custom link removed.";
    }
    catch (error) {
        sharingMessage.textContent = String(error);
    }
};
if (key.value)
    void load();
