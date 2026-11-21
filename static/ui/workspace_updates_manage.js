import { checked, sessionMarker, teamRequest } from "./team_api.js";
const access = document.querySelector("#updateAccess");
const key = access.querySelector("input");
const editor = document.querySelector("#announcementEditor");
const status = document.querySelector("#updateSaveStatus");
const list = document.querySelector("#editAnnouncements");
const add = document.querySelector("#newAnnouncement");
const form = document.querySelector("#editAnnouncement");
const feedback = document.querySelector("#updateFeedbackCounts");
const signedInTeam = document.body.dataset.updatesTeam === "yes";
let current;
let busy = false;
key.value = signedInTeam ? "" : sessionStorage.getItem("cueporchOperator") || "";
const credential = () => signedInTeam ? sessionMarker : key.value;
const field = (name) => form.elements.namedItem(name);
async function checkedRequest(path, method = "GET", body = {}) {
    return (await checked(await teamRequest(path, method, body, credential()))).json();
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
function show(entry) {
    current = entry;
    form.reset();
    field("slug").readOnly = Boolean(entry);
    if (entry) {
        for (const name of ["slug", "title", "description", "area", "category", "state", "date"]) {
            field(name).value = entry[name];
        }
    }
    else {
        field("date").value = new Date().toISOString().slice(0, 10);
    }
    feedback.hidden = !entry?.feedback;
    feedback.textContent = entry?.feedback
        ? `Thumbs up ${entry.feedback.up || 0} · Thumbs down ${entry.feedback.down || 0}` : "";
    form.hidden = false;
}
async function openList() {
    const { entries } = await checkedRequest("updates");
    list.replaceChildren();
    for (const entry of entries) {
        const row = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = entry.title;
        button.onclick = () => {
            void perform(async () => show(await checkedRequest("updates/" + encodeURIComponent(entry.slug))));
        };
        const state = entry.state === "released" ? "Released" : entry.state === "up-next" ? "Up next" : "Draft";
        row.append(button, ` · ${state}`);
        list.append(row);
    }
    editor.hidden = false;
    access.hidden = true;
}
add.onclick = () => { if (!busy)
    show(); };
access.onsubmit = event => { event.preventDefault(); void perform(openList); };
form.onsubmit = event => {
    event.preventDefault();
    const payload = Object.fromEntries(new FormData(form));
    void perform(async () => {
        const saved = current
            ? await checkedRequest("updates/" + encodeURIComponent(current.slug), "PUT", { ...payload, revision: current.revision })
            : await checkedRequest("updates", "POST", payload);
        show(saved);
        await openList();
        status.textContent = "Announcement saved.";
    });
};
if (credential())
    void perform(openList);
