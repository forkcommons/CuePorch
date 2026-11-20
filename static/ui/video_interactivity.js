import { setupPresentation } from "./video_presentation.js";
import { setupRatings } from "./video_ratings.js";
import { setupRegistration } from "./video_registration.js";
import { sessionMarker } from "./team_api.js";
const identity = document.querySelector("#identify");
const key = document.querySelector("#chapterKey");
const status = document.querySelector("#chapterStatus");
const editor = document.querySelector("#interactivityEditor");
const title = document.querySelector("#interactivityTitle");
const preview = document.querySelector("#chapterPreview");
const form = document.querySelector("#chapterEditor");
const list = document.querySelector("#chapterRows");
const rowTemplate = document.querySelector("#chapterRow");
const add = document.querySelector("#addChapter");
const endpoint = "/api/library/work/" + encodeURIComponent(document.body.dataset.work || "");
const teamPreview = document.body.dataset.teamPreview;
let state;
key.value = sessionStorage.getItem("cueporchOperator") || "";
async function call(suffix, method = "GET", body) {
    const headers = { Authorization: "Bearer " + (teamPreview ? sessionMarker : key.value) };
    if (teamPreview && body)
        headers["X-CuePorch-Team"] = "1";
    if (body)
        headers["Content-Type"] = "application/json";
    const response = await fetch(endpoint + suffix, { method, headers, body: body && JSON.stringify(body) });
    if (!response.ok) {
        const message = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
        throw new Error(message?.textContent || `Request failed (${response.status})`);
    }
    return response.json();
}
async function addPicture(file) {
    const body = new FormData();
    body.set("logo", file);
    const response = await fetch("/api/review-logos", {
        method: "POST", body, headers: { Authorization: "Bearer " + key.value },
    });
    if (!response.ok) {
        const message = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
        throw new Error(message?.textContent || `Image upload failed (${response.status})`);
    }
    const result = await response.json();
    return result.logo;
}
function timestamp(value) {
    const match = /^(?:(\d{1,6}):)?([0-5]?\d):([0-5]\d)$/.exec(value.trim());
    if (!match)
        throw new Error("Use start times such as 00:01:25.");
    return Number(match[1] || 0) * 3600 + Number(match[2]) * 60 + Number(match[3]);
}
function displayTime(seconds) {
    return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
        .map((value) => String(value).padStart(2, "0")).join(":");
}
const showPresentation = setupPresentation(call, addPicture, timestamp, displayTime, preview);
async function exportCsv(suffix, filename) {
    const response = await fetch(endpoint + suffix, { headers: { Authorization: "Bearer " + key.value } });
    if (!response.ok)
        throw new Error("The CSV could not be exported.");
    const uri = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = uri;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(uri), 1000);
}
const showRatings = teamPreview ? undefined : setupRatings(call, timestamp, displayTime, () => exportCsv("/rating/export", "cueporch-video-rating.csv"));
const showRegistration = teamPreview ? undefined : setupRegistration(call, () => exportCsv("/registration/export", "cueporch-registrants.csv"));
function appendChapter(chapter) {
    const row = rowTemplate.content.firstElementChild.cloneNode(true);
    const name = row.querySelector("[name=title]");
    const time = row.querySelector("[name=timecode]");
    name.value = chapter.title;
    time.value = displayTime(chapter.timecode);
    const watch = row.querySelector("[data-chapter-action=preview]");
    watch.hidden = preview.hidden;
    watch.onclick = () => {
        try {
            preview.currentTime = timestamp(time.value);
            status.textContent = "";
        }
        catch (error) {
            status.textContent = String(error);
        }
    };
    row.querySelector("[data-chapter-action=remove]").onclick = () => {
        row.remove();
        add.disabled = false;
    };
    list.append(row);
    add.disabled = list.children.length >= 80;
    return name;
}
function present(value) {
    state = value;
    title.textContent = "Interactivity for " + value.title;
    list.replaceChildren();
    add.disabled = false;
    value.chapters.forEach(appendChapter);
}
async function show() {
    try {
        const settings = await call("/chapters");
        if (teamPreview) {
            preview.src = teamPreview;
            preview.hidden = false;
        }
        else {
            const work = await call("");
            if (work.preview) {
                const ticket = await call("/ticket", "POST", { sha256: work.preview });
                preview.src = ticket.preview;
                preview.hidden = false;
            }
            sessionStorage.setItem("cueporchOperator", key.value);
        }
        present(settings);
        identity.hidden = true;
        editor.hidden = false;
        status.textContent = "";
        await Promise.all([showPresentation(), showRatings?.(), showRegistration?.()]);
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
}
identity.onsubmit = (event) => { event.preventDefault(); void show(); };
if (teamPreview || key.value)
    void show();
add.onclick = () => appendChapter({ title: "", timecode: Math.floor(preview.currentTime || 0) }).focus();
form.onsubmit = async (event) => {
    event.preventDefault();
    try {
        const chapters = [...list.children].map((row) => ({
            title: row.querySelector("[name=title]").value,
            timecode: timestamp(row.querySelector("[name=timecode]").value),
        }));
        present(await call("/chapters", "PUT", { version: state.version, revision: state.revision, chapters }));
        status.textContent = "Chapters saved.";
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
};
