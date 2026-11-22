import { checked, sessionMarker, teamRequest } from "./team_api.js";
const editor = document.querySelector("#creatorEditor");
const form = document.querySelector("#creatorForm");
const status = document.querySelector("#creatorStatus");
const available = document.querySelector("#creatorVideo");
const add = document.querySelector("#creatorAdd");
const list = document.querySelector("#creatorFeatured");
const link = document.querySelector("#publicCreatorLink");
const name = form.elements.namedItem("name");
const handle = form.elements.namedItem("handle");
const bio = form.elements.namedItem("bio");
const links = form.elements.namedItem("links");
const visible = form.elements.namedItem("visible");
const cover = form.elements.namedItem("cover");
const save = form.querySelector("button[type=submit]");
let draft;
function control(label, title, disabled, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.setAttribute("aria-label", `${label} ${title}`);
    button.disabled = disabled;
    button.onclick = () => { action(); showFeatured(); };
    return button;
}
function move(index, delta) {
    const [video] = draft.featured.splice(index, 1);
    draft.featured.splice(index + delta, 0, video);
}
function showFeatured() {
    const videos = new Map(draft.videos.map(video => [video.id, video.title]));
    const options = draft.videos.filter(video => !draft.featured.includes(video.id));
    available.replaceChildren(...options.map(video => new Option(video.title, video.id)));
    add.disabled = options.length === 0 || draft.featured.length >= 24;
    list.replaceChildren(...draft.featured.map((uid, index) => {
        const item = document.createElement("li");
        const title = videos.get(uid) || uid;
        item.append(title + " ", control("Move up", title, index === 0, () => move(index, -1)), control("Move down", title, index === draft.featured.length - 1, () => move(index, 1)), control("Remove", title, false, () => { draft.featured.splice(index, 1); }));
        return item;
    }));
}
function show(settings) {
    draft = settings;
    name.value = draft.name;
    handle.value = draft.handle;
    bio.value = draft.bio;
    links.value = draft.links.join("\n");
    visible.checked = draft.visible;
    cover.replaceChildren(new Option("No cover", ""), ...draft.videos.map(video => new Option(video.title, video.id)));
    cover.value = draft.cover || "";
    link.hidden = !draft.visible || !draft.url;
    if (draft.url)
        link.href = draft.url;
    showFeatured();
    editor.hidden = false;
}
async function task(run) {
    status.textContent = "";
    try {
        await run();
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
}
add.onclick = () => {
    if (!available.value)
        return;
    draft.featured.push(available.value);
    showFeatured();
};
form.onsubmit = event => {
    event.preventDefault();
    save.disabled = true;
    void task(async () => {
        const response = await checked(await teamRequest("profile", "PUT", {
            name: name.value, handle: handle.value, bio: bio.value, visible: visible.checked, cover: cover.value || null,
            links: links.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
            revision: draft.revision, featured: draft.featured,
        }));
        show(await response.json());
        status.textContent = "Creator profile saved.";
    }).finally(() => { save.disabled = false; });
};
void task(async () => {
    let settings = await (await checked(await teamRequest("profile"))).json();
    let notice = "";
    if (new URLSearchParams(location.search).get("fromVimeo") === "1") {
        if (settings.revision) {
            notice = "Your existing creator profile was left as saved.";
        }
        else {
            try {
                const response = await checked(await fetch("/api/migration/profile", {
                    headers: { Authorization: "Bearer " + sessionMarker },
                }));
                const source = await response.json();
                settings = { ...settings, name: source.name, bio: source.bio, links: source.links };
                notice = "Vimeo profile loaded. Choose a handle and save when ready.";
            }
            catch {
                notice = "To load a Vimeo draft, preview the transfer and sign in as a workspace Owner or Administrator.";
            }
        }
    }
    show(settings);
    if (notice)
        status.textContent = notice;
});
