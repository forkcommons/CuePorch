import { editFolderSlack as showFolderSlack } from "./folder_slack.js";
import { editFolderReviews } from "./folder_reviews.js";
import { renderCommentText } from "./review_comment_text.js";
import { folderChoices, libraryUploader, readUploadPrivacy } from "./library_upload.js";
import { saveEmbedMap } from "./embed_map.js";
import { openPrivacy, privateSettings, sharingFields } from "./library_privacy.js";
const key = document.querySelector("#key");
const deleteFolder = document.querySelector("#deleteFolder");
const deleteWorks = document.querySelector("#deleteWorks");
const privacySelected = document.querySelector("#privacySelected");
const showcaseSelected = document.querySelector("#showcaseSelected");
const ratingSelected = document.querySelector("#ratingSelected");
const translateSelected = document.querySelector("#translateSelected");
const translationBatches = document.querySelector("#translationBatches");
const uploadDefaults = document.querySelector("#uploadDefaults");
const workBrowse = document.querySelector("#workBrowse");
const trashBrowse = document.querySelector("#trashBrowse");
const trashType = document.querySelector("#trashType");
const trashActor = document.querySelector("#trashActor");
const trashItems = document.querySelector("#trashItems");
const trashEmpty = document.querySelector("#trashEmpty");
const trashTable = document.querySelector("#trashTable");
const result = document.querySelector("#result");
const creator = document.querySelector("#creator");
const creators = document.querySelector("#creators");
const starFolder = document.querySelector("#starFolder");
const library = document.querySelector("#library");
const tree = document.querySelector("#folderTree");
const heading = document.querySelector("#folderTitle");
const folderBrowse = document.querySelector("#folderBrowse");
const folderResults = document.querySelector("#folderResults");
const items = document.querySelector("#items");
const empty = document.querySelector("#empty");
const table = document.querySelector("#workTable");
const search = document.querySelector("#search");
const privacy = document.querySelector("#privacy");
const assetType = document.querySelector("#assetType");
const sort = document.querySelector("#sort");
const view = document.querySelector("#view");
const editFolder = document.querySelector("#editFolder");
const folderPrivacy = document.querySelector("#folderPrivacy");
const folderSlack = document.querySelector("#folderSlack");
const folderReviews = document.querySelector("#folderReviews");
const folderHubSpot = document.querySelector("#folderHubSpot");
const translateFolder = document.querySelector("#translateFolder");
const destination = document.querySelector("#destination");
const move = document.querySelector("#move");
const editor = document.querySelector("#folderEditor");
const form = document.querySelector("#folderForm");
const folderName = document.querySelector("#folderName");
const folderColor = document.querySelector("#folderColor");
const folderParent = document.querySelector("#folderParent");
const folderError = document.querySelector("#folderError");
const saveFolder = document.querySelector("#saveFolder");
key.value = sessionStorage.getItem("cueporchOperator") || "";
let works = [];
let folders = [];
let current = null;
let editing;
let section = "Library";
let deletions = [];
let trashTimer;
let translationTimer;
const selected = new Set();
let closeCardPreview;
function request(path, method = "GET", body, headers = {}) {
    return fetch(path, {
        method,
        headers: {
            Authorization: "Bearer " + key.value,
            "Content-Type": "application/json",
            ...headers,
        },
        body: body ? JSON.stringify(body) : undefined,
    });
}
async function completed(response) {
    if (!response.ok) {
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        throw new Error(page.querySelector("p")?.textContent ||
            `Request failed (${response.status})`);
    }
    return response;
}
function action(button, run) {
    button.onclick = async () => {
        button.disabled = true;
        try {
            await run();
        }
        catch (error) {
            result.textContent = String(error);
        }
        finally {
            button.disabled = false;
            updateMove();
        }
    };
}
const timelineProjectCreate = document.querySelector("#timelineProjectCreate");
const timelineProjectTitle = document.querySelector("#timelineProjectTitle");
const timelineProjectSubmit = document.querySelector("#timelineProjectSubmit");
const timelineProjectDrafts = document.querySelector("#timelineProjectDrafts");
const timelineProjectList = document.querySelector("#timelineProjectList");
timelineProjectCreate.onsubmit = async (event) => {
    event.preventDefault();
    timelineProjectSubmit.disabled = true;
    try {
        const response = await request("/api/library/timeline-projects", "POST", {
            title: timelineProjectTitle.value.trim(),
        }).then(completed);
        const { id } = await response.json();
        location.assign("/operator/videos/" + encodeURIComponent(id) + "/timeline");
    }
    catch (error) {
        result.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        timelineProjectSubmit.disabled = false;
    }
};
async function loadTimelineProjects() {
    const credential = key.value;
    const { projects } = await request("/api/library/timeline-projects").then(completed).then(response => response.json());
    if (credential !== key.value)
        return;
    timelineProjectList.replaceChildren();
    for (const project of projects) {
        const item = document.createElement("li");
        const link = document.createElement("a");
        link.href = `/operator/videos/${encodeURIComponent(project.id)}/timeline`;
        link.textContent = project.title;
        item.appendChild(link);
        timelineProjectList.appendChild(item);
    }
    timelineProjectDrafts.hidden = projects.length === 0;
}
function folderOptions(select) {
    folderChoices(select, folders, select === folderParent ? editing?.id : undefined);
}
function updateMove() {
    destination.disabled = !selected.size;
    deleteWorks.disabled = move.disabled = !selected.size;
    privacySelected.disabled = !selected.size || ![...selected].every((uid) => works.some((work) => work.id === uid && work.editable_privacy));
    showcaseSelected.disabled = ratingSelected.disabled = !selected.size || selected.size > 50 ||
        ![...selected].every(uid => works.some(work => work.id === uid && work.asset_type === "video"));
    translateSelected.disabled = !selected.size || selected.size > 500 ||
        ![...selected].some(uid => works.some(work => work.id === uid && work.asset_type === "video"));
}
function renderTree() {
    function branch(parent) {
        const list = document.createElement("ul");
        for (const folder of folders.filter((entry) => entry.parent === parent)) {
            const li = document.createElement("li");
            li.append(folderButton(folder.name, folder.id, folder.color));
            const children = branch(folder.id);
            if (children.childElementCount)
                li.append(children);
            list.append(li);
        }
        return list;
    }
    const starredFolders = document.createElement("ul");
    if (section === "Starred") {
        for (const folder of folders.filter((entry) => entry.starred)) {
            const item = document.createElement("li");
            item.append(folderButton(folder.name, folder.id, folder.color));
            starredFolders.append(item);
        }
    }
    tree.replaceChildren(folderButton("Library", null), branch(null), sectionButton("Starred"), starredFolders, sectionButton("Watch later"), sectionButton("Recently deleted"));
    workBrowse.hidden = section === "Recently deleted";
    document.querySelector("#uploadHint").hidden = section !== "Library";
    document.querySelector("#uploadLibrary").hidden = section !== "Library";
    uploadDefaults.hidden = section !== "Library";
    trashBrowse.hidden = section !== "Recently deleted";
    for (const link of document.querySelectorAll(".operator-navigation a")) {
        if (!["/operator/library", "/operator/library?view=deleted"].includes(link.getAttribute("href")))
            continue;
        const isDeletedView = link.getAttribute("href").includes("?view=deleted");
        if (isDeletedView === (section === "Recently deleted"))
            link.setAttribute("aria-current", "page");
        else
            link.removeAttribute("aria-current");
    }
    const folder = folders.find((entry) => entry.id === current);
    heading.textContent =
        section === "Library" ? folder?.name || "Library" : section;
    deleteFolder.hidden = editFolder.hidden = starFolder.hidden = !folder;
    folderPrivacy.hidden = !folder;
    folderSlack.hidden = folderReviews.hidden = !folder;
    folderHubSpot.hidden = !folder;
    translateFolder.hidden = !folder;
    starFolder.textContent = folder?.starred ? "Unstar folder" : "Star folder";
    if (folder) {
        action(starFolder, () => setStar("folder", folder.id, !folder.starred));
        action(deleteFolder, () => removeItems("folder", [folder.id]));
        action(folderPrivacy, () => editFolderPrivacy(folder));
        action(folderSlack, () => editFolderSlack(folder));
        action(folderReviews, () => editFolderReviews(folder, {
            request, checked: completed, openDialog: openWorkDialog, status: workDialogStatus,
            publicBase: document.body.dataset.publicUrl || location.origin,
        }));
        action(folderHubSpot, () => editFolderHubSpot(folder));
        action(translateFolder, async () => requestTranslations([], folder));
    }
    folderOptions(destination);
    destination.value = current || "";
}
async function navigate(next, folder = null) {
    const leavingTrash = section === "Recently deleted";
    section = next;
    current = folder;
    selected.clear();
    renderTree();
    renderWorks();
    tree.querySelector('[aria-current="true"]')?.focus();
    if (section === "Recently deleted")
        await loadTrash();
    else if (leavingTrash)
        await load();
}
function sectionButton(label) {
    const button = document.createElement("button");
    button.textContent = label;
    if (section === label)
        button.setAttribute("aria-current", "true");
    action(button, () => navigate(label));
    return button;
}
function folderButton(label, id, color) {
    const button = document.createElement("button");
    button.textContent = label;
    if (section === "Library" && id === current)
        button.setAttribute("aria-current", "true");
    if (color) {
        const dot = document.createElement("span");
        dot.className = "folder-dot";
        dot.style.backgroundColor = color;
        dot.setAttribute("aria-hidden", "true");
        button.prepend(dot);
    }
    action(button, () => navigate("Library", id));
    return button;
}
async function setStar(kind, id, starred) {
    await completed(await request("/api/library/star/" + kind + "/" + encodeURIComponent(id), "PUT", { starred }));
    await load();
    result.textContent = starred ? "Added to Starred." : "Removed from Starred.";
}
function numericValue(work, order) {
    if (order === "long" || order === "short")
        return work.duration_seconds;
    const field = order.startsWith("added") ? work.added_at : work.modified_at;
    const date = field === null ? NaN : Date.parse(field);
    return Number.isFinite(date) ? date : null;
}
function libraryDate(value) {
    return value && Number.isFinite(Date.parse(value))
        ? new Date(value).toLocaleDateString()
        : "";
}
function durationText(seconds) {
    return seconds === null
        ? ""
        : Math.floor(seconds / 60) +
            ":" +
            String(Math.floor(seconds % 60)).padStart(2, "0");
}
function renderFolderResults(query, creatorFilter) {
    const matching = !creatorFilter && !privacy.value && (assetType.value === "" || assetType.value === "folders")
        ? folders.filter(folder => (section === "Library" ? folder.parent === current : section === "Starred" && folder.starred)
            && folder.name.toLocaleLowerCase().includes(query)) : [];
    if (sort.value === "title-desc")
        matching.reverse();
    folderBrowse.hidden = !matching.length;
    folderResults.dataset.view = view.value;
    folderResults.replaceChildren();
    for (const folder of matching) {
        const row = document.createElement("li");
        row.style.setProperty("--folder-color", folder.color);
        const open = document.createElement("button");
        open.type = "button";
        open.textContent = folder.name;
        action(open, async () => {
            if (assetType.value === "folders")
                assetType.value = "";
            search.value = "";
            await navigate("Library", folder.id);
        });
        row.append(open);
        folderResults.append(row);
    }
}
function cardPreview(work) {
    const area = document.createElement("div");
    area.className = "work-quick-preview";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Preview";
    button.setAttribute("aria-label", "Preview " + work.title);
    button.setAttribute("aria-pressed", "false");
    const status = document.createElement("span");
    status.setAttribute("aria-live", "polite");
    area.append(button, status);
    let loading;
    let player;
    let revision = 0;
    function close() {
        revision += 1;
        window.clearTimeout(loading);
        loading = undefined;
        if (closeCardPreview === close)
            closeCardPreview = undefined;
        status.textContent = "";
        button.setAttribute("aria-pressed", "false");
        player?.pause();
        player?.removeAttribute("src");
        player?.load();
        player?.remove();
        player = undefined;
    }
    async function play() {
        if (closeCardPreview === close || !area.isConnected)
            return;
        closeCardPreview?.();
        closeCardPreview = close;
        const attempt = ++revision;
        status.textContent = "Loading preview…";
        try {
            const info = await getWorkInfo(work);
            if (!info.preview)
                throw new Error("No retained video to preview.");
            if (revision !== attempt || !area.isConnected)
                return;
            const ticket = await mediaTicket(work, info.preview);
            if (revision !== attempt || !area.isConnected)
                return;
            const video = document.createElement("video");
            video.muted = true;
            video.playsInline = true;
            video.loop = true;
            video.setAttribute("aria-label", work.title + " preview");
            video.src = ticket.preview;
            player = video;
            area.append(video);
            await video.play();
            if (player !== video)
                return;
            button.setAttribute("aria-pressed", "true");
            status.textContent = "";
        }
        catch (error) {
            if (revision !== attempt)
                return;
            close();
            status.textContent = error instanceof Error ? error.message : String(error);
        }
    }
    button.onclick = () => {
        if (closeCardPreview === close)
            close();
        else
            void play();
    };
    area.onpointerenter = event => {
        if (event.pointerType === "mouse")
            loading = window.setTimeout(() => void play(), 180);
    };
    area.onpointerleave = close;
    area.onkeydown = event => { if (event.key === "Escape")
        close(); };
    return area;
}
function renderWorks() {
    closeCardPreview?.();
    const query = search.value.trim().toLocaleLowerCase();
    const author = creator.value.trim().toLocaleLowerCase();
    renderFolderResults(query, author);
    const visible = assetType.value === "folders" ? [] : works.filter((work) => (section === "Starred"
        ? work.starred
        : section === "Watch later"
            ? work.watch_later
            : (workspaceSearch && Boolean(query) && current === null) || work.folder === current) &&
        (!author || work.creator?.toLocaleLowerCase().includes(author)) &&
        work.title.toLocaleLowerCase().includes(query) &&
        (!assetType.value || (assetType.value === "video") === (work.asset_type === "video")) &&
        (!privacy.value || workPrivacy(work) === privacy.value));
    visible.sort((a, b) => {
        const title = a.title.localeCompare(b.title);
        const stable = title || a.id.localeCompare(b.id);
        const order = sort.value;
        if (order === "title")
            return stable;
        if (order === "title-desc")
            return -title || a.id.localeCompare(b.id);
        const first = numericValue(a, order);
        const second = numericValue(b, order);
        if (first === null)
            return second === null ? stable : 1;
        if (second === null)
            return -1;
        const direction = order === "long" || order.endsWith("-desc") ? -1 : 1;
        return (first - second) * direction || stable;
    });
    items.replaceChildren();
    table.hidden = !visible.length;
    table.dataset.view = view.value;
    empty.hidden = Boolean(visible.length) || !folderBrowse.hidden;
    for (const work of visible) {
        const row = items.insertRow();
        row.dataset.workId = work.id;
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = selected.has(work.id);
        checkbox.setAttribute("aria-label", "Select " + work.title);
        checkbox.onchange = () => {
            if (checkbox.checked)
                selected.add(work.id);
            else
                selected.delete(work.id);
            updateMove();
        };
        row.insertCell().append(checkbox);
        const title = row.insertCell();
        const name = document.createElement("span");
        name.className = "work-title";
        name.textContent = work.title;
        title.append(name);
        const info = document.createElement("small");
        info.className = "work-info";
        info.textContent = [
            work.asset_type === "video" ? "" : work.asset_type,
            work.creator,
            durationText(work.duration_seconds),
            work.version_count > 1 ? work.version_count + " retained versions" : "",
            libraryDate(work.added_at),
        ]
            .filter(Boolean)
            .join(" · ");
        title.append(info);
        if (work.asset_type === "video" && work.quality !== "owner file needed")
            title.append(cardPreview(work));
        const sharing = row.insertCell();
        if (work.editable_privacy) {
            const button = document.createElement("button");
            const visibility = work.visibility || "private";
            button.textContent = visibility === "embed_only"
                ? "Embed only" : visibility[0].toUpperCase() + visibility.slice(1);
            button.setAttribute("aria-label", "Change privacy for " + work.title);
            action(button, () => editPrivacy(work));
            sharing.append(button);
        }
        else
            sharing.textContent = work.source_privacy || "Unknown";
        row.insertCell().textContent = [
            work.quality,
            ...work.exceptions.map((item) => typeof item === "string" ? item : JSON.stringify(item)),
        ].join(" · ");
        row.insertCell().textContent =
            [work.delivery?.status, work.delivery?.error]
                .filter(Boolean)
                .join(" · ") || "Preserved locally";
        const actions = row.insertCell();
        actions.append(cardMenu(work));
        if (work.title_sync_error) {
            const sync = document.createElement("p");
            sync.textContent = "Syncing title with the original video host.";
            actions.append(sync);
        }
        const star = document.createElement("button");
        star.textContent = work.starred ? "Unstar work" : "Star work";
        star.setAttribute("aria-label", (work.starred ? "Unstar " : "Star ") + work.title);
        action(star, () => setStar("work", work.id, !work.starred));
        actions.append(star);
        const remove = document.createElement("button");
        remove.textContent = "Delete work";
        remove.setAttribute("aria-label", "Delete " + work.title);
        action(remove, () => removeItems("work", [work.id]));
        actions.append(remove);
        const destinations = work.asset_type !== "video" ? [] : work.public_eligible ? [false, true] : [false];
        for (const pub of destinations) {
            const button = document.createElement("button");
            button.textContent = pub ? "Publish publicly" : "Deliver privately";
            action(button, async () => {
                await completed(await request("/api/delivery/" + encodeURIComponent(work.id), "POST", { public: pub }));
                result.textContent = "Queued " + work.title;
            });
            actions.append(button);
        }
        if (work.delivery?.route_kind) {
            const link = document.createElement("a");
            link.href = "/watch/" + work.delivery.route_kind + "/" + work.id;
            link.textContent = "Watch";
            actions.append(link);
        }
    }
    updateMove();
}
const workDialog = document.querySelector("#workDialog");
const workDialogTitle = document.querySelector("#workDialogTitle");
const workDialogBody = document.querySelector("#workDialogBody");
const workDialogStatus = document.querySelector("#workDialogStatus");
const titleEditor = document.querySelector("#titleEditor");
const titleForm = document.querySelector("#titleForm");
const workTitle = document.querySelector("#workTitle");
const titleError = document.querySelector("#titleError");
const saveTitle = document.querySelector("#saveTitle");
function workEndpoint(work) {
    return "/api/library/work/" + encodeURIComponent(work.id);
}
async function getWorkInfo(work) {
    return (await completed(await request(workEndpoint(work)))).json();
}
function cardMenu(work) {
    const menu = document.createElement("details");
    menu.className = "work-menu";
    const summary = document.createElement("summary");
    summary.textContent = "Actions";
    summary.setAttribute("aria-label", "Actions for " + work.title);
    menu.append(summary);
    const video = work.asset_type === "video" ? [
        ["Share", () => shareDialog(work)],
        ["Copy link", () => shareDialog(work, "watch")],
        ["Review link", () => reviewLinksDialog(work)],
        ["Copy embed code", () => shareDialog(work, "embed")],
        ["Add to showcases", () => addShowcaseMembers([work.id])],
        ["Set video rating", () => editRatings([work.id])],
        [
            work.watch_later ? "Remove from Watch later" : "Watch later",
            () => saveForLater(work),
        ],
        ["Change thumbnail", () => thumbnailDialog(work)],
        ["Replace", () => versionsDialog(work)],
    ] : [];
    const entries = [
        ...video,
        ["Rename", () => editTitle(work, false)],
        ["Create a copy", () => editTitle(work, true)],
        [work.asset_type === "video" ? "Video info" : "File info", () => infoDialog(work, false)],
        ["Download", () => infoDialog(work, true)],
    ];
    if (work.asset_type === "video") {
        entries.unshift(["Edit video details", () => { location.href = "/operator/videos/" + encodeURIComponent(work.id); }]);
    }
    if (work.vimeo_source)
        entries.push(["Restore Vimeo privacy", () => restoreVimeoPrivacy(work)]);
    for (const [label, run] of entries) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        action(button, async () => {
            menu.open = false;
            summary.focus();
            await run();
        });
        menu.append(button);
    }
    return menu;
}
function openWorkDialog(title) {
    clearTimeout(translationTimer);
    delete workDialogBody.dataset.translationBatch;
    workDialogTitle.textContent = title;
    workDialogBody.replaceChildren();
    workDialogStatus.textContent = "";
    if (!workDialog.open)
        workDialog.showModal();
    return workDialogBody;
}
function videoBatch(title, label, count, submit) {
    const body = openWorkDialog(title);
    const summary = document.createElement("p");
    summary.textContent = typeof count === "string" ? count : `${count} selected ${count === 1 ? "video" : "videos"}`;
    const form = document.createElement("form");
    form.className = "library-batch-form";
    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = label;
    body.append(summary, form);
    form.append(save);
    form.onsubmit = async (event) => {
        event.preventDefault();
        save.disabled = true;
        workDialogStatus.textContent = "";
        try {
            const message = await submit(new FormData(form));
            if (form.isConnected)
                workDialogStatus.textContent = message;
        }
        catch (error) {
            if (form.isConnected)
                workDialogStatus.textContent = error instanceof Error ? error.message : String(error);
        }
        finally {
            save.disabled = false;
        }
    };
    return { form, save };
}
async function addShowcaseMembers(works) {
    const response = await completed(await request("/api/showcases"));
    const destinations = await response.json();
    const { form, save } = videoBatch("Add to showcases", "Add videos", works.length, async (fields) => {
        const showcases = fields.getAll("showcase");
        if (!showcases.length)
            throw new Error("Choose at least one showcase.");
        const response = await completed(await request("/api/showcases/selected-videos", "POST", { works, showcases }));
        const { added, showcases: count } = await response.json();
        return added ? `Added ${added} entries across ${count} ${count === 1 ? "showcase" : "showcases"}.`
            : "These videos are already in the selected showcases.";
    });
    if (!destinations.length) {
        save.hidden = true;
        const link = document.createElement("a");
        link.href = "/operator/showcases";
        link.textContent = "Create a showcase first";
        form.prepend(link);
        return;
    }
    const choices = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = "Showcases";
    choices.append(legend);
    for (const destination of destinations) {
        const label = document.createElement("label");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.name = "showcase";
        input.value = destination.id;
        label.append(input, ` ${destination.settings.title} (${destination.settings.visibility.replace("_", " ")})`);
        choices.append(label);
    }
    form.prepend(choices);
}
function editRatings(works) {
    const { form } = videoBatch("Set video rating", "Apply rating", works.length, async (fields) => {
        const response = await completed(await request("/api/library/ratings", "PUT", {
            works, mode: fields.get("mode"), prompt: fields.get("prompt"), start: Number(fields.get("start")),
        }));
        const { updated } = await response.json();
        return updated ? `Saved rating settings for ${updated} ${updated === 1 ? "video" : "videos"}.`
            : "The selected videos already use these rating settings.";
    });
    const template = document.querySelector("#batchRatingFields");
    form.prepend(template.content.cloneNode(true));
    const mode = form.querySelector("[name=mode]");
    const fields = form.querySelector("fieldset");
    mode.onchange = () => { fields.disabled = fields.hidden = mode.value === "none"; };
}
const translationEndpoint = "/api/library/subtitle-translations";
const translationLabels = {
    queued: "Queued", running: "Translating", waiting: "Waiting for approval", complete: "Complete",
    rejected: "Rejected", superseded: "Source or destination changed", failed: "Failed", skipped: "Skipped",
};
function requestTranslations(works, folder) {
    let requestId = crypto.randomUUID();
    let languages = "";
    const scope = folder ? `${folder.name} and its subfolders` : works.length;
    const { form } = videoBatch("Translate subtitles", "Queue translations", scope, async (fields) => {
        const source = String(fields.get("source") || "").trim();
        const target = String(fields.get("target") || "").trim();
        const pair = source + ":" + target;
        if (languages && languages !== pair)
            requestId = crypto.randomUUID();
        languages = pair;
        const response = await completed(await request(translationEndpoint, "POST", {
            id: requestId, works, folder: folder?.id, source_language: source, target_language: target,
        }));
        if (form.isConnected)
            showTranslations(await response.json());
        return "";
    });
    const fields = document.querySelector("#batchTranslationFields");
    form.prepend(fields.content.cloneNode(true));
}
async function translationReport(id, method = "GET") {
    return (await completed(await request(`${translationEndpoint}/${id}`, method))).json();
}
function showTranslations(batch) {
    const body = openWorkDialog(`Subtitle translations · ${batch.source} → ${batch.target}`);
    body.dataset.translationBatch = batch.id;
    const list = document.createElement("ul");
    for (const item of batch.items) {
        const row = document.createElement("li");
        const link = document.createElement("a");
        link.href = `/operator/videos/${encodeURIComponent(item.work)}/languages`;
        link.textContent = item.title;
        row.append(link, ` · ${translationLabels[item.state] || item.state}`);
        if (item.reason)
            row.append(` · ${item.reason}`);
        list.append(row);
    }
    const finished = batch.items.filter(item => item.state === "complete").length;
    workDialogStatus.textContent = `${finished} of ${batch.items.length} complete.`;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "Retry failed or skipped";
    retry.disabled = !batch.items.some(item => ["blocked", "failed", "superseded", "rejected", "skipped"].includes(item.state));
    const pending = batch.items.some(item => ["queued", "running", "waiting"].includes(item.state));
    let generation = 0;
    function schedule() {
        if (pending)
            translationTimer = window.setTimeout(() => { if (workDialog.open)
                void refresh("GET"); }, 1200);
    }
    async function refresh(method) {
        clearTimeout(translationTimer);
        const attempt = ++generation;
        try {
            const next = await translationReport(batch.id, method);
            if (attempt !== generation || !workDialog.open || body.dataset.translationBatch !== batch.id)
                return;
            if (method === "GET" && JSON.stringify(next.items) === JSON.stringify(batch.items))
                schedule();
            else
                showTranslations(next);
        }
        catch (error) {
            if (workDialog.open && body.dataset.translationBatch === batch.id) {
                retry.disabled = false;
                workDialogStatus.textContent = error instanceof Error ? error.message : String(error);
            }
        }
    }
    retry.onclick = () => { retry.disabled = true; void refresh("PUT"); };
    body.append(retry, list);
    schedule();
}
async function recentTranslations() {
    const { batches } = await (await completed(await request(translationEndpoint))).json();
    const body = openWorkDialog("Subtitle translation batches");
    if (!batches.length) {
        workDialogStatus.textContent = "No subtitle translation batches yet.";
        return;
    }
    for (const batch of batches) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = `${batch.source} → ${batch.target} · ${batch.items} items · ${new Date(batch.created * 1000).toLocaleString()}`;
        action(button, async () => showTranslations(await translationReport(batch.id)));
        const row = document.createElement("p");
        row.append(button);
        body.append(row);
    }
}
action(showcaseSelected, () => addShowcaseMembers([...selected]));
action(ratingSelected, async () => editRatings([...selected]));
action(translateSelected, async () => requestTranslations([...selected]));
action(translationBatches, recentTranslations);
function publicAddress(path) {
    return new URL(path, document.body.dataset.publicUrl || location.origin).href;
}
function editPrivacy(work) {
    const endpoint = workEndpoint(work) + "/privacy";
    return openPrivacy({
        open: openWorkDialog,
        read: async () => (await completed(await request(endpoint))).json(),
        save: async (settings) => (await completed(await request(endpoint, "PUT", settings))).json(),
        reload: load,
        absolute: publicAddress,
        status: workDialogStatus,
    });
}
async function restoreVimeoPrivacy(work) {
    const endpoint = workEndpoint(work) + "/source-privacy";
    const read = async () => (await completed(await request(endpoint))).json();
    const state = await read();
    const body = openWorkDialog("Restore Vimeo privacy");
    const detail = document.createElement("p");
    const mode = state.settings.visibility.replace("_", " ");
    const sites = state.settings.embed === "domains" ? state.settings.domains.join(", ") : state.settings.embed;
    detail.textContent = `Recorded visibility: ${mode}. Embedding: ${sites}. Downloads: ${state.settings.download ? "on" : "off"}.`;
    body.append(detail);
    if (state.issue || !state.eligible) {
        const status = document.createElement("p");
        status.textContent = state.issue || "A local MP4 and a CuePorch-managed work route are required to switch a public hosted copy.";
        body.append(status);
    }
    if (!state.eligible)
        return;
    if (state.hosted) {
        const handoff = document.createElement("p");
        handoff.textContent = "CuePorch will make the hosted copy private before enabling these viewer links. Existing CuePorch work addresses are retained.";
        body.append(handoff);
    }
    const form = document.createElement("form");
    const password = document.createElement("input");
    if (state.settings.visibility === "password") {
        const label = document.createElement("label");
        label.textContent = "Replacement viewer password";
        password.type = "password";
        password.maxLength = 256;
        password.required = !state.has_password;
        label.append(password);
        form.append(label);
    }
    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = "Apply recorded privacy";
    form.append(save);
    body.append(form);
    function status(current, saved) {
        save.disabled = current.handoff === "pending";
        if (current.handoff === "pending") {
            workDialogStatus.textContent = current.handoff_error || "Confirming the hosted copy is private…";
            window.setTimeout(async () => {
                if (!workDialog.open || !body.contains(form))
                    return;
                try {
                    const latest = await read();
                    if (workDialog.open && body.contains(form))
                        status(latest, true);
                    if (latest.handoff !== "pending")
                        await load();
                }
                catch (error) {
                    workDialogStatus.textContent = String(error);
                    save.disabled = false;
                }
            }, 1500);
        }
        else if (current.pending) {
            workDialogStatus.textContent = "Waiting for the configured publication approval.";
        }
        else if (saved) {
            workDialogStatus.textContent = "Source privacy applied. Open Share to copy the available CuePorch links.";
        }
    }
    status(state, false);
    form.onsubmit = async (event) => {
        event.preventDefault();
        save.disabled = true;
        try {
            const updated = await (await completed(await request(endpoint, "PUT", { password: password.value }))).json();
            password.value = "";
            password.required = state.settings.visibility === "password" && !updated.has_password;
            status(updated, true);
            await load();
        }
        catch (error) {
            workDialogStatus.textContent = error instanceof Error ? error.message : String(error);
            save.disabled = false;
        }
    };
}
async function defaultPrivacy() {
    const response = await completed(await request("/api/library/upload-defaults"));
    return { ...privateSettings, ...await response.json() };
}
function workPrivacy(work) {
    return work.visibility || work.source_privacy || "Unknown";
}
async function folderPolicy(id) {
    const response = await completed(await request(`/api/library/folders/${id}/privacy`));
    return response.json();
}
async function editFolderPrivacy(folder) {
    const existing = await folderPolicy(folder.id);
    const body = openWorkDialog("Folder privacy");
    const summary = document.createElement("p");
    summary.textContent = existing.inherited ? "This folder currently inherits its parent’s privacy settings."
        : "Apply these settings to videos in this folder and its subfolders. Subfolders with their own override keep it.";
    body.append(summary);
    const fields = sharingFields(body);
    fields.populate({ ...privateSettings, ...existing.settings, has_password: existing.enabled && existing.has_password,
        allowed: existing.allowed });
    fields.save.textContent = "Apply folder override";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove folder override";
    remove.hidden = !existing.enabled;
    body.append(remove);
    async function save(enabled) {
        fields.save.disabled = remove.disabled = true;
        try {
            const response = await completed(await request(`/api/library/folders/${folder.id}/privacy`, "PUT", {
                ...fields.values(), enabled,
            }));
            const policy = await response.json();
            fields.populate({ ...privateSettings, ...policy.settings, has_password: policy.enabled && policy.has_password,
                allowed: policy.allowed });
            remove.hidden = !policy.enabled;
            workDialogStatus.textContent = enabled ? `Folder override saved for ${policy.affected} video(s).`
                : "Folder override removed. Existing video settings were kept.";
            await load();
        }
        catch (error) {
            workDialogStatus.textContent = error instanceof Error ? error.message : String(error);
        }
        finally {
            fields.save.disabled = remove.disabled = false;
        }
    }
    fields.form.onsubmit = (event) => { event.preventDefault(); void save(true); };
    remove.onclick = () => { void save(false); };
}
async function editFolderHubSpot(folder) {
    const path = "/api/library/hubspot-media-bridge/folder";
    const response = await request(path);
    const bridge = await (await completed(response)).json();
    const localSync = bridge.active && bridge.folder === folder.id;
    openWorkDialog("HubSpot Media Bridge");
    const status = document.createElement("p");
    status.setAttribute("role", "status");
    status.textContent = !bridge.configured
        ? "Configure HubSpot Media Bridge to sync library folders."
        : !bridge.connected
            ? "Connect HubSpot to sync this folder."
            : localSync
                ? `HubSpot sync is active for ${folder.name}.`
                : bridge.active
                    ? bridge.folder
                        ? `HubSpot sync is active for a different folder. Syncing ${folder.name} will replace it as the HubSpot folder.`
                        : "HubSpot sync is active."
                    : bridge.folder
                        ? "The saved folder uses a different HubSpot connection."
                        : `${folder.name} will become the HubSpot folder.`;
    workDialogBody.append(status);
    if (bridge.configured && bridge.connected && (!bridge.folder || bridge.active) && !localSync) {
        const sync = document.createElement("button");
        sync.textContent = "Sync this folder to HubSpot";
        action(sync, async () => {
            await completed(await request(path, "PUT", { folder: folder.id }, { "X-CuePorch-Integration": "1" }));
            await editFolderHubSpot(folder);
        });
        workDialogBody.append(sync);
    }
    else if (localSync) {
        const stop = document.createElement("button");
        stop.textContent = "Stop HubSpot folder sync";
        action(stop, async () => {
            await completed(await request(path, "DELETE", {}, { "X-CuePorch-Integration": "1" }));
            await editFolderHubSpot(folder);
        });
        workDialogBody.append(stop);
    }
}
const editFolderSlack = (folder) => showFolderSlack(folder, {
    request, checked: completed, openDialog: openWorkDialog, status: workDialogStatus,
});
const editUploadDefaults = () => openPrivacy({
    title: "Your upload defaults",
    success: "Upload defaults saved.",
    open: openWorkDialog,
    read: defaultPrivacy,
    save: async (settings) => {
        const response = await completed(await request("/api/library/upload-defaults", "PUT", settings));
        return { ...privateSettings, ...await response.json() };
    },
    reload: async () => { },
    absolute: publicAddress,
    status: workDialogStatus,
});
action(uploadDefaults, editUploadDefaults);
action(privacySelected, async () => {
    const selection = [...selected];
    await openPrivacy({
        title: "Apply privacy to selected videos",
        open: openWorkDialog,
        read: async () => privateSettings,
        save: async ({ password, ...settings }) => {
            const response = await completed(await request("/api/library/privacy", "PUT", {
                ...settings, password, works: selection,
            }));
            const outcome = await response.json();
            return {
                ...privateSettings, ...settings,
                pending: outcome.pending ? settings : null,
            };
        },
        reload: load,
        absolute: publicAddress,
        status: workDialogStatus,
    });
});
async function copyText(field) {
    try {
        await navigator.clipboard.writeText(field.value);
        workDialogStatus.textContent = "Copied to clipboard.";
    }
    catch {
        field.focus();
        field.select();
        workDialogStatus.textContent = "Select and copy the highlighted text.";
    }
}
async function mediaBridgeControls(work) {
    let receipt;
    const path = workEndpoint(work) + "/hubspot-media-bridge";
    try {
        const response = await request(path);
        const done = await completed(response);
        receipt = await done.json();
    }
    catch {
        return null;
    }
    const { configured, connected, eligible, state } = receipt;
    if (!configured)
        return null;
    if (state === "off" && (!connected || !eligible))
        return null;
    const group = document.createElement("div");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "HubSpot Media Bridge");
    const title = document.createElement("h3");
    title.textContent = "HubSpot Media Bridge";
    const status = document.createElement("p");
    status.setAttribute("role", "status");
    status.textContent = bridgeStatusText(receipt);
    group.append(title, status);
    const addButton = document.createElement("button");
    addButton.textContent = state === "published" ? "Update HubSpot video" : "Add video to HubSpot";
    const removeButton = document.createElement("button");
    removeButton.textContent = "Remove video from HubSpot";
    const refreshButton = document.createElement("button");
    refreshButton.textContent = "Refresh HubSpot status";
    if (!eligible || !connected || state === "queued" || state === "waiting") {
        addButton.hidden = true;
    }
    if (state === "off" || state === "archived" || !connected || receipt.managed) {
        removeButton.hidden = true;
    }
    action(addButton, async () => {
        const response = await request(path, "PUT", {}, { "X-CuePorch-Integration": "1" });
        await completed(response);
        const updated = await mediaBridgeControls(work);
        if (updated && group.isConnected)
            group.replaceWith(updated);
    });
    action(removeButton, async () => {
        const response = await request(path, "DELETE", {}, { "X-CuePorch-Integration": "1" });
        await completed(response);
        const updated = await mediaBridgeControls(work);
        if (updated && group.isConnected)
            group.replaceWith(updated);
    });
    action(refreshButton, async () => {
        const updated = await mediaBridgeControls(work);
        if (!group.isConnected)
            return;
        if (updated)
            group.replaceWith(updated);
        else
            group.remove();
    });
    group.append(addButton, removeButton, refreshButton);
    return group;
}
function bridgeStatusText(receipt) {
    if (receipt.problem)
        return receipt.problem;
    switch (receipt.state) {
        case "published": return "Published to HubSpot";
        case "waiting": return "Waiting for publication approval";
        case "queued": return "Queued for HubSpot";
        case "unconfirmed": return "Unconfirmed by HubSpot";
        case "archived": return "Archived in HubSpot";
        case "canceled": return "Canceled";
        default: return receipt.state || "Ready";
    }
}
async function shareDialog(work, copy) {
    const info = await getWorkInfo(work);
    openWorkDialog("Share " + info.title);
    if (!work.asset_type || work.asset_type === "video") {
        for (const [label, destination] of [["YouTube", "youtube"], ["LinkedIn", "linkedin"], ["Facebook", "facebook"], ["X", "x"]]) {
            const social = document.createElement("a");
            social.href = `/operator/videos/${work.id}/social?destination=${destination}`;
            social.textContent = `Publish to ${label}`;
            workDialogBody.append(social);
        }
    }
    if (!copy && (!work.asset_type || work.asset_type === "video")) {
        const placeholder = document.createElement("div");
        workDialogBody.append(placeholder);
        void mediaBridgeControls(work).then(controls => {
            if (!placeholder.isConnected)
                return;
            if (controls)
                placeholder.replaceWith(controls);
            else
                placeholder.remove();
        });
    }
    if (!info.watch && !info.embed) {
        const link = document.createElement("a");
        link.href = "/operator/exhibitions";
        link.textContent = "Manage private screening grants";
        workDialogBody.append("No viewer link has been created. ", link);
        return;
    }
    const watch = info.watch && document.createElement("input");
    const embed = info.embed && document.createElement("textarea");
    if (watch && info.watch)
        watch.value = publicAddress(info.watch);
    if (embed && info.embed) {
        const iframe = document.createElement("iframe");
        iframe.src = publicAddress(info.embed);
        iframe.title = info.title;
        iframe.width = "640";
        iframe.height = "360";
        iframe.allowFullscreen = true;
        iframe.referrerPolicy = "origin";
        iframe.setAttribute("allow", "autoplay; fullscreen; picture-in-picture");
        embed.value = iframe.outerHTML;
    }
    for (const [label, field] of [
        ["Watch link", watch],
        ["Embed code", embed],
    ]) {
        if (!field)
            continue;
        const labelElement = document.createElement("label");
        labelElement.textContent = label;
        field.readOnly = true;
        field.setAttribute("aria-label", label);
        labelElement.append(field);
        const button = document.createElement("button");
        button.textContent = "Copy " + label.toLowerCase();
        action(button, () => copyText(field));
        workDialogBody.append(labelElement, button);
    }
    if (copy) {
        const field = copy === "watch" ? watch : embed;
        if (field)
            await copyText(field);
        else
            workDialogStatus.textContent = copy === "watch"
                ? "This video is shared through its embed."
                : "Embedding is disabled for this video.";
    }
}
async function mediaTicket(work, sha256) {
    const response = await completed(await request(workEndpoint(work) + "/ticket", "POST", { sha256 }));
    const urls = await response.json();
    return urls;
}
async function reviewLinksDialog(work) {
    const endpoint = workEndpoint(work) + "/review-links";
    const sourcesEndpoint = workEndpoint(work) + "/vimeo-review-links";
    const [linkResponse, defaultResponse, reviewSourceResponse] = await Promise.all([
        request(endpoint).then(completed),
        request("/api/review-defaults").then(completed),
        request(sourcesEndpoint).then(completed),
    ]);
    const reviewSources = await reviewSourceResponse.json();
    const links = await linkResponse.json();
    const defaults = await defaultResponse.json();
    openWorkDialog("Review links");
    const editor = document.createElement("form");
    const fields = {
        name: reviewField("Review link name", "text"),
        expires: reviewField("Expires", "datetime-local"),
        password: reviewField("Review password", "password"),
        removePassword: reviewField("Remove saved password", "checkbox"),
        allowDownload: reviewField("Allow download", "checkbox"),
        allowVersions: reviewField("Allow previous versions", "checkbox"),
        allowComment: reviewField("Allow comments", "checkbox"),
        allowStatus: reviewField("Allow status changes", "checkbox"),
        allowResolved: reviewField("Allow resolved comments", "checkbox"),
        dark: reviewField("Dark mode", "checkbox"),
        showOthers: reviewField("Show other videos after playback", "checkbox"),
        logo: reviewField("Review logo", "file"),
        removeLogo: reviewField("Remove saved logo", "checkbox"),
        autoCreate: reviewField("Automatically create review links for new uploads", "checkbox"),
    };
    fields.name.input.required = true;
    fields.name.input.maxLength = 128;
    fields.expires.input.required = true;
    fields.password.input.maxLength = 256;
    fields.password.input.autocomplete = "new-password";
    fields.logo.input.accept = "image/png,image/jpeg,image/webp";
    fields.autoCreate.input.checked = defaults.auto_create;
    const save = document.createElement("button");
    const saveDefaults = document.createElement("button");
    saveDefaults.type = "button";
    saveDefaults.textContent = "Save as default settings";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "New review link";
    let logo = null;
    const currentChoices = async () => {
        const newLogo = fields.logo.input.files?.[0];
        let reviewLogo = fields.removeLogo.input.checked ? null : logo;
        if (newLogo) {
            const form = new FormData();
            form.append("logo", newLogo);
            const response = await completed(await fetch("/api/review-logos", {
                method: "POST",
                headers: { Authorization: "Bearer " + key.value },
                body: form,
            }));
            reviewLogo = (await response.json()).logo;
        }
        const password = fields.password.input.value;
        return {
            name: fields.name.input.value,
            expires: new Date(fields.expires.input.value).getTime() / 1000,
            allow_download: fields.allowDownload.input.checked,
            allow_versions: fields.allowVersions.input.checked,
            allow_comment: fields.allowComment.input.checked,
            allow_status: fields.allowStatus.input.checked,
            allow_resolved: fields.allowResolved.input.checked,
            dark: fields.dark.input.checked,
            show_other_videos: fields.showOthers.input.checked,
            logo: reviewLogo,
            ...(password || fields.removePassword.input.checked
                ? { password: fields.removePassword.input.checked ? "" : password }
                : {}),
        };
    };
    function populate(link) {
        const settings = link || defaults;
        const due = new Date(link ? link.expires * 1000 : Date.now() + defaults.lifetime_seconds * 1000);
        const localDate = new Date(due.getTime() - due.getTimezoneOffset() * 60000);
        fields.name.input.value = link?.name || "";
        fields.expires.input.value = localDate.toISOString().slice(0, 16);
        fields.password.input.value = "";
        fields.removePassword.label.hidden = !settings.password;
        fields.removePassword.input.checked = false;
        fields.allowDownload.input.checked = settings.allow_download;
        fields.allowVersions.input.checked = settings.allow_versions;
        fields.allowComment.input.checked = settings.allow_comment;
        fields.allowStatus.input.checked = settings.allow_status;
        fields.allowResolved.input.checked = settings.allow_resolved;
        fields.dark.input.checked = settings.dark;
        fields.showOthers.input.checked = settings.show_other_videos;
        logo = settings.logo;
        fields.logo.input.value = "";
        fields.removeLogo.label.hidden = !logo;
        fields.removeLogo.input.checked = false;
        save.textContent = link ? "Save review settings" : "Create review link";
        cancel.hidden = !link;
        editor.onsubmit = async (event) => {
            event.preventDefault();
            save.disabled = true;
            try {
                const body = await currentChoices();
                await completed(await request(link ? endpoint + "/" + link.id : endpoint, link ? "PUT" : "POST", body));
                await reopen();
                workDialogStatus.textContent = link ? "Review link saved." : "Review link created.";
            }
            catch (error) {
                workDialogStatus.textContent = String(error);
            }
            finally {
                save.disabled = false;
            }
        };
    }
    const reopen = () => reviewLinksDialog(work);
    action(saveDefaults, async () => {
        try {
            const choices = await currentChoices();
            await completed(await request("/api/review-defaults", "PUT", {
                ...choices,
                lifetime_seconds: choices.expires - Date.now() / 1000,
                auto_create: fields.autoCreate.input.checked,
            }));
            await reopen();
            workDialogStatus.textContent = "Default review settings saved.";
        }
        catch (error) {
            workDialogStatus.textContent = String(error);
        }
    });
    cancel.onclick = () => populate();
    editor.append(...Object.values(fields).map(({ label }) => label), save, saveDefaults, cancel);
    populate();
    const list = document.createElement("ul");
    for (const link of links) {
        const row = document.createElement("li");
        const title = document.createElement("strong");
        title.textContent = link.name;
        const state = link.expires <= Date.now() / 1000 ? "Expired" : link.enabled ? "Enabled" : "Disabled";
        row.append(title, " · " + state + " · " + new Date(link.expires * 1000).toLocaleString());
        const address = new URL(link.path, document.body.dataset.publicUrl || location.origin);
        const field = document.createElement("input");
        field.value = address.href;
        field.readOnly = true;
        field.setAttribute("aria-label", "Link for " + link.name);
        const open = document.createElement("a");
        open.href = address.href;
        open.target = "_blank";
        open.rel = "noopener noreferrer";
        open.textContent = "Review page";
        row.append(field, open);
        const update = async (path, method, data) => {
            try {
                await completed(await request(endpoint + "/" + link.id + path, method, data));
                await reopen();
            }
            catch (error) {
                workDialogStatus.textContent = String(error);
            }
        };
        const buttons = [
            ["Copy review link", () => copyText(field)],
            ["Preview as reviewer", async () => {
                    const preview = window.open("about:blank", "_blank");
                    if (!preview) {
                        workDialogStatus.textContent = "Allow a new tab to open the preview.";
                        return;
                    }
                    try {
                        preview.opener = null;
                        const response = await completed(await request(endpoint + "/" + link.id + "/preview", "POST"));
                        const receipt = await response.json();
                        preview.location.replace(new URL(receipt.path, location.origin));
                    }
                    catch (error) {
                        preview.close();
                        workDialogStatus.textContent = String(error);
                    }
                }],
            ["Manage", async () => { populate(link); fields.name.input.focus(); }],
            ["Comments", () => manageReviewFeedback(work, link, reopen)],
            [link.enabled ? "Disable" : "Enable", () => update("", "PUT", { enabled: !link.enabled })],
            ["Make a copy", () => update("/copy", "POST")],
            ["Delete", () => update("", "DELETE")],
        ];
        for (const [label, click] of buttons) {
            const button = document.createElement("button");
            button.textContent = label;
            action(button, click);
            row.append(button);
        }
        list.append(row);
    }
    if (reviewSources.links.length > 0) {
        const section = document.createElement('section');
        const heading = document.createElement('h2');
        heading.textContent = 'Vimeo review settings';
        section.append(heading);
        for (const link of reviewSources.links) {
            const row = document.createElement('div');
            const name = document.createElement('div');
            name.textContent = link.name;
            const status = document.createElement('div');
            const now = Date.now() / 1000;
            const eligible = link.enabled &&
                link.expires !== null &&
                link.expires > now &&
                link.replacement === undefined;
            if (link.replacement === null) {
                status.textContent = 'Locally removed';
            }
            else if (link.replacement !== undefined) {
                status.textContent = 'Previously replaced';
            }
            else if (!link.enabled) {
                status.textContent = 'Disabled';
            }
            else if (link.expires === null) {
                status.textContent = 'Expiration unavailable';
            }
            else if (link.expires <= now) {
                status.textContent = 'Expired';
            }
            if (link.logo_required) {
                status.textContent = [status.textContent, 'Add the review logo in CuePorch'].filter(Boolean).join(' · ');
            }
            row.append(name, status);
            if (eligible) {
                let passwordInput;
                if (link.needs_password) {
                    const passwordField = reviewField('Password for replacement of ' + link.name, 'password');
                    passwordField.input.maxLength = 256;
                    passwordField.input.autocomplete = 'new-password';
                    passwordInput = passwordField.input;
                    row.append(passwordField.label);
                }
                const button = document.createElement('button');
                button.type = 'button';
                button.textContent = 'Create replacement for ' + link.name;
                button.addEventListener('click', async () => {
                    button.disabled = true;
                    try {
                        await completed(await request(sourcesEndpoint + '/' + encodeURIComponent(link.id), 'POST', { password: passwordInput ? passwordInput.value : '' }));
                        await reopen();
                        workDialogStatus.textContent = `Replacement created for ${link.name}.`;
                    }
                    catch (error) {
                        workDialogStatus.textContent = error instanceof Error ? error.message : String(error);
                    }
                    finally {
                        button.disabled = false;
                    }
                });
                row.append(button);
            }
            section.append(row);
        }
        workDialogBody.append(section);
    }
    workDialogBody.append(list, editor);
}
function reviewField(text, type) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = type;
    label.append(text, input);
    return { label, input };
}
async function manageReviewFeedback(work, link, back) {
    const versions = await (await completed(await request(workEndpoint(work) + "/versions"))).json();
    openWorkDialog("Comments for " + link.name);
    const backButton = document.createElement("button");
    backButton.textContent = "Back to review links";
    action(backButton, back);
    const picker = document.createElement("select");
    const label = document.createElement("label");
    label.textContent = "Video version";
    label.append(picker);
    versions.forEach((version, index) => {
        if (version.video_files.some((file) => file.format === "mp4")) {
            picker.append(new Option(version.current ? "Current version" : "Previous version " + index, version.key));
        }
    });
    const list = document.createElement("ol");
    const download = document.createElement("button");
    download.textContent = "Download comments CSV";
    const markers = document.createElement("button");
    markers.textContent = "Download Premiere markers";
    const url = () => link.path + "/feedback/" + picker.value;
    const render = async () => {
        if (!picker.value) {
            workDialogStatus.textContent = "This review has no playable version.";
            download.disabled = true;
            markers.disabled = true;
            return;
        }
        const data = await (await completed(await request(url()))).json();
        workDialogStatus.textContent = data.status;
        list.replaceChildren();
        for (const comment of data.comments) {
            const row = document.createElement("li");
            const author = document.createElement("strong");
            author.textContent = comment.author;
            const text = document.createElement("p");
            renderCommentText(text, comment.body);
            row.append(author, " · " + comment.seconds.toFixed(2) + " seconds", text);
            if (!comment.parent) {
                const control = document.createElement("button");
                control.textContent = comment.resolved ? "Reopen thread" : "Resolve thread";
                action(control, async () => {
                    await completed(await request(url() + "/" + comment.id + "/resolved", "PUT", {
                        resolved: !comment.resolved,
                    }));
                    await render();
                });
                row.append(control);
            }
            list.append(row);
        }
        if (!list.children.length) {
            const row = document.createElement("li");
            row.textContent = "No comments yet.";
            list.append(row);
        }
    };
    picker.onchange = () => render().catch((error) => { workDialogStatus.textContent = String(error); });
    const downloadEntries = [
        { button: download, path: "/comments.csv", name: "review-comments.csv" },
        { button: markers, path: "/markers.json", name: "cueporch-review-markers.json" },
    ];
    for (const { button, path, name } of downloadEntries) {
        action(button, async () => {
            const response = await completed(await request(url() + path));
            const object = URL.createObjectURL(await response.blob());
            const file = document.createElement("a");
            file.href = object;
            file.download = name;
            file.click();
            window.setTimeout(() => URL.revokeObjectURL(object), 1000);
        });
    }
    const form = document.createElement('form');
    form.hidden = !picker.value;
    const csvLabel = document.createElement('label');
    csvLabel.textContent = 'Vimeo review CSV';
    const fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = '.csv,text/csv';
    fileInput.required = true;
    csvLabel.append(fileInput);
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.textContent = 'Import Vimeo review notes';
    const sourceCutLabel = document.createElement('label');
    sourceCutLabel.textContent = 'Source cut in CSV';
    sourceCutLabel.hidden = true;
    const sourceCut = document.createElement('select');
    sourceCutLabel.append(sourceCut);
    form.append(csvLabel, submit, sourceCutLabel);
    const clearSourceCut = () => {
        sourceCut.replaceChildren();
        sourceCutLabel.hidden = true;
    };
    fileInput.addEventListener('change', clearSourceCut);
    picker.addEventListener('change', clearSourceCut);
    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const file = fileInput.files?.[0];
        if (!file)
            return;
        const target = url();
        const cut = sourceCutLabel.hidden ? '' : sourceCut.value;
        submit.disabled = true;
        fileInput.disabled = true;
        picker.disabled = true;
        try {
            const formData = new FormData();
            formData.append('file', file);
            if (cut)
                formData.append('source_cut', cut);
            const response = await fetch(target + '/vimeo-comments.csv', {
                method: 'POST',
                headers: { Authorization: 'Bearer ' + key.value },
                body: formData,
            });
            if (response.status === 409 &&
                response.headers.get('content-type')?.includes('application/json')) {
                const data = await response.clone().json();
                if (Array.isArray(data.versions) &&
                    data.versions.every((version) => typeof version === 'string')) {
                    sourceCut.replaceChildren(...data.versions.map((label) => new Option(label, label)));
                    sourceCutLabel.hidden = false;
                    sourceCut.focus();
                    workDialogStatus.textContent =
                        'Choose the source cut from the CSV and import again.';
                    return;
                }
            }
            const result = await (await completed(response)).json();
            await render();
            workDialogStatus.textContent =
                `Imported ${result.imported} notes · ${result.existing} already present.`;
            form.reset();
            clearSourceCut();
        }
        catch (error) {
            workDialogStatus.textContent =
                error instanceof Error ? error.message : String(error);
        }
        finally {
            submit.disabled = false;
            fileInput.disabled = false;
            picker.disabled = false;
        }
    });
    workDialogBody.append(backButton, label, download, markers, form, list);
    await render();
}
let thumbnailUrl;
async function thumbnailDialog(work) {
    const info = await getWorkInfo(work);
    const endpoint = workEndpoint(work) + "/thumbnail";
    const existing = info.thumbnail ? await request(endpoint) : null;
    const blob = existing?.ok ? await existing.blob() : null;
    openWorkDialog("Change thumbnail");
    if (blob) {
        thumbnailUrl = URL.createObjectURL(blob);
        const current = document.createElement("img");
        current.alt = "Current thumbnail";
        current.src = thumbnailUrl;
        current.className = "thumbnail-preview";
        workDialogBody.append(current);
    }
    const save = async (response) => {
        try {
            await completed(response);
            workDialog.close();
            result.textContent = "Thumbnail saved.";
        }
        catch (error) {
            workDialogStatus.textContent = String(error);
        }
    };
    const uploadLabel = document.createElement("label");
    const upload = document.createElement("input");
    upload.type = "file";
    upload.accept = "image/jpeg,image/png,image/webp";
    uploadLabel.textContent = "Upload thumbnail";
    uploadLabel.append(upload);
    const uploadButton = document.createElement("button");
    uploadButton.textContent = "Use uploaded image";
    action(uploadButton, async () => {
        const image = upload.files?.[0];
        if (!image) {
            workDialogStatus.textContent = "Choose a thumbnail image.";
            return;
        }
        const data = new FormData();
        data.append("image", image);
        await save(await fetch(endpoint, {
            method: "POST",
            headers: { Authorization: "Bearer " + key.value },
            body: data,
        }));
    });
    workDialogBody.append(uploadLabel, uploadButton);
    if (info.preview) {
        const hash = info.preview;
        const video = document.createElement("video");
        video.muted = true;
        video.preload = "metadata";
        const slider = document.createElement("input");
        slider.type = "range";
        slider.min = "0";
        slider.step = "0.01";
        slider.value = "0";
        slider.disabled = true;
        const at = document.createElement("output");
        const label = document.createElement("label");
        label.textContent = "Frame time";
        label.append(slider, at);
        const frameButton = document.createElement("button");
        frameButton.textContent = "Use selected frame";
        frameButton.disabled = true;
        slider.oninput = () => {
            video.currentTime = Number(slider.value);
            at.textContent = slider.value + " seconds";
        };
        video.onloadedmetadata = () => {
            if (!Number.isFinite(video.duration) || video.duration <= 0)
                return;
            slider.max = Math.max(0, video.duration - 0.1).toFixed(2);
            slider.value = "0";
            slider.disabled = false;
            frameButton.disabled = false;
            at.textContent = "0 seconds";
        };
        action(frameButton, async () => {
            await save(await request(endpoint, "POST", {
                seconds: Number(slider.value),
                media: hash,
            }));
        });
        workDialogBody.append(video, label, frameButton);
        video.src = (await mediaTicket(work, hash)).preview;
    }
}
async function versionsDialog(work) {
    const endpoint = workEndpoint(work) + "/versions";
    const versions = await (await completed(await request(endpoint))).json();
    const current = versions[0].key;
    openWorkDialog("Replace video");
    const save = async (response, label) => {
        try {
            workDialogStatus.textContent = "Preparing the video version…";
            await completed(await response);
            workDialog.close();
            await load();
            result.textContent = label;
        }
        catch (error) {
            (workDialog.open ? workDialogStatus : result).textContent = String(error);
        }
    };
    const uploadLabel = document.createElement("label");
    const upload = document.createElement("input");
    upload.type = "file";
    upload.accept = ".mp4,.mov,.webm,.mkv";
    uploadLabel.textContent = "Replacement video file";
    uploadLabel.append(upload);
    const replace = document.createElement("button");
    replace.textContent = "Save as new version";
    action(replace, async () => {
        const video = upload.files?.[0];
        if (!video) {
            workDialogStatus.textContent = "Choose a replacement video file.";
            return;
        }
        const inputs = new FormData();
        inputs.append("video", video);
        inputs.append("current", current);
        await save(fetch(endpoint, {
            method: "POST",
            headers: { Authorization: "Bearer " + key.value },
            body: inputs,
        }), "New version saved.");
    });
    const heading = document.createElement("h3");
    heading.textContent = "Version history";
    const list = document.createElement("ol");
    for (const [index, version] of versions.entries()) {
        const entry = document.createElement("li");
        const media = version.video_files[0];
        entry.textContent = [
            version.current ? "Current version" : "Previous version " + index,
            libraryDate(version.saved_at),
            durationText(version.duration_seconds),
            media ? media.format.toUpperCase() : "Video file needed",
        ].filter(Boolean).join(" · ");
        if (!version.current && media) {
            const restore = document.createElement("button");
            restore.textContent = "Restore previous version " + index;
            action(restore, () => save(request(endpoint + "/restore", "POST", {
                current,
                restore: version.key,
            }), "Previous version restored."));
            entry.append(" ", restore);
        }
        list.append(entry);
    }
    workDialogBody.append(uploadLabel, replace, heading, list);
}
async function infoDialog(work, download) {
    const info = await getWorkInfo(work);
    openWorkDialog(download ? "Download " + info.title : info.asset_type === "video" ? "Video info" : "File info");
    if (!download) {
        const facts = document.createElement("dl");
        const fields = {
            Title: info.title,
            "Imported title": info.source_title === info.title ? "" : info.source_title,
            Description: info.description,
            Creator: info.creator,
            Source: info.source,
            "Source privacy": info.source_privacy,
            Duration: info.duration_seconds === null
                ? ""
                : info.duration_seconds + " seconds",
            Recovery: info.disposition,
        };
        for (const [label, value] of Object.entries(fields)) {
            if (!value)
                continue;
            const key = document.createElement("dt");
            key.textContent = label;
            const field = document.createElement("dd");
            field.textContent = value;
            facts.append(key, field);
        }
        workDialogBody.append(facts);
        if (info.preview) {
            const hash = info.preview;
            const preview = document.createElement("button");
            preview.textContent = `Load ${info.asset_type} preview`;
            action(preview, async () => {
                const url = (await mediaTicket(work, hash)).preview;
                if (info.asset_type === "image") {
                    const image = document.createElement("img");
                    image.alt = info.title;
                    image.src = url;
                    preview.replaceWith(image);
                }
                else {
                    const player = document.createElement(info.asset_type === "audio" ? "audio" : "video");
                    player.src = url;
                    player.controls = true;
                    player.preload = "metadata";
                    preview.replaceWith(player);
                }
            });
            workDialogBody.append(preview);
        }
    }
    const list = document.createElement("ul");
    for (const file of info.files) {
        const playable = file.role === "original" || file.role === "playback";
        if (download && !playable)
            continue;
        const item = document.createElement("li");
        item.textContent = [
            file.role,
            file.format.toUpperCase(),
            file.name,
            file.language,
            file.bytes.toLocaleString() + " bytes",
        ]
            .filter(Boolean)
            .join(" · ");
        if (playable) {
            const button = document.createElement("button");
            button.textContent = "Download " + file.role;
            action(button, async () => {
                const link = document.createElement("a");
                link.href = (await mediaTicket(work, file.sha256)).download;
                link.download = "";
                link.referrerPolicy = "no-referrer";
                link.click();
            });
            item.append(" ", button);
        }
        list.append(item);
    }
    if (list.childElementCount)
        workDialogBody.append(list);
    else
        workDialogBody.append("No file is available.");
}
workDialog.onclose = () => {
    workDialogBody.replaceChildren();
    if (thumbnailUrl)
        URL.revokeObjectURL(thumbnailUrl);
    thumbnailUrl = undefined;
};
async function saveForLater(work) {
    await completed(await request(workEndpoint(work) + "/watch-later", "PUT", {
        saved: !work.watch_later,
    }));
    await load();
    result.textContent = work.watch_later
        ? "Removed from Watch later."
        : "Added to Watch later.";
}
function editTitle(work, copy) {
    document.querySelector("#titleEditorHeading").textContent = copy
        ? "Create a copy"
        : "Rename work";
    workTitle.value = copy ? work.title.slice(0, 121) + " (copy)" : work.title;
    titleError.textContent = "";
    saveTitle.textContent = copy ? "Create copy" : "Save title";
    titleForm.onsubmit = async (event) => {
        event.preventDefault();
        saveTitle.disabled = true;
        try {
            await completed(await request(workEndpoint(work) + (copy ? "/copy" : "/title"), copy ? "POST" : "PUT", { title: workTitle.value }));
            titleEditor.close();
            if (copy) {
                section = "Library";
                current = null;
                search.value = "";
                creator.value = "";
                privacy.value = "";
            }
            await load();
            result.textContent = copy
                ? "Private copy added to Library."
                : "Title saved.";
        }
        catch (error) {
            (titleEditor.open ? titleError : result).textContent = String(error);
        }
        finally {
            saveTitle.disabled = false;
        }
    };
    titleEditor.showModal();
    workTitle.select();
}
document.querySelector("#cancelTitle").onclick = () => titleEditor.close();
const trashPath = "/api/library/recently-deleted";
function renderTrash() {
    const actor = trashActor.value.trim().toLocaleLowerCase();
    const visible = deletions.filter((item) => (!trashType.value || item.kind === trashType.value) &&
        item.actor.toLocaleLowerCase().includes(actor));
    trashItems.replaceChildren();
    trashEmpty.hidden = !!visible.length;
    trashTable.hidden = !visible.length;
    for (const item of visible) {
        const row = trashItems.insertRow();
        row.insertCell().textContent = item.title;
        row.insertCell().textContent = item.kind === "work" ? "Work" : "Folder";
        row.insertCell().textContent = item.actor;
        row.insertCell().textContent = new Date(item.deleted_at * 1000).toLocaleString();
        row.insertCell().textContent = item.error
            ? "Waiting for the original video host. Retrying."
            : {
                withdrawing: "Withdrawing",
                restoring: "Restoring",
                deleted: "Ready to restore",
            }[item.state];
        const actions = row.insertCell();
        if (item.state === "deleted") {
            const button = document.createElement("button");
            button.textContent = "Restore";
            button.setAttribute("aria-label", "Restore " + item.title);
            action(button, async () => {
                await completed(await request(trashPath + "/" + item.id + "/restore", "POST"));
                result.textContent = "Restoration queued.";
                await loadTrash();
            });
            actions.append(button);
        }
    }
}
async function loadTrash() {
    window.clearTimeout(trashTimer);
    const previous = deletions;
    deletions = await (await completed(await request(trashPath))).json();
    if (section !== "Recently deleted")
        return;
    const currentIds = new Set(deletions.map((item) => item.id));
    if (previous.some((item) => !currentIds.has(item.id)))
        await load(false);
    renderTrash();
    if (deletions.some((item) => item.state !== "deleted")) {
        trashTimer = window.setTimeout(() => {
            if (section === "Recently deleted")
                void loadTrash().catch((error) => {
                    result.textContent = String(error);
                });
        }, 1200);
    }
}
async function removeItems(kind, ids) {
    await completed(await request(trashPath, "POST", { kind, items: ids }));
    result.textContent =
        "Items moved to Recently deleted. Hosted withdrawal may still be in progress.";
    await load();
}
action(deleteWorks, () => removeItems("work", [...selected]));
action(document.querySelector("#refreshTrash"), loadTrash);
trashType.onchange = trashActor.oninput = renderTrash;
async function load(refreshTrash = true) {
    const [libraryResponse, folderResponse] = await Promise.all([
        request("/api/library").then(completed),
        request("/api/library/folders").then(completed),
    ]);
    works = await libraryResponse.json();
    folders = await folderResponse.json();
    const names = [
        ...new Set(works.flatMap((work) => (work.creator ? [work.creator] : []))),
    ].sort();
    creators.replaceChildren(...names.map((name) => new Option(name, name)));
    if (!folders.some((entry) => entry.id === current))
        current = null;
    selected.clear();
    const previous = privacy.value;
    const modes = [
        ...new Set(works.map(workPrivacy)),
    ].sort();
    privacy.replaceChildren(new Option("All", ""), ...modes.map((mode) => new Option(mode, mode)));
    privacy.value = modes.includes(previous) ? previous : "";
    library.hidden = false;
    renderTree();
    renderWorks();
    timelineProjectDrafts.hidden = true;
    void loadTimelineProjects().catch(error => { result.textContent = String(error); });
    if (section === "Recently deleted" && refreshTrash)
        await loadTrash();
}
function openEditor(folder) {
    editing = folder;
    document.querySelector("#folderEditorTitle").textContent = folder
        ? "Edit folder"
        : "New folder";
    folderOptions(folderParent);
    folderParent.value = (folder ? folder.parent : current) || "";
    folderName.value = folder?.name || "";
    folderColor.value = folder?.color || "#164e5a";
    folderError.textContent = "";
    editor.showModal();
    folderName.focus();
}
form.onsubmit = async (event) => {
    event.preventDefault();
    saveFolder.disabled = true;
    try {
        const prefix = "/api/library/folders";
        await completed(await request(editing ? prefix + "/" + editing.id : prefix, editing ? "PUT" : "POST", {
            name: folderName.value,
            color: folderColor.value,
            parent: folderParent.value || null,
        }));
        editor.close();
        await load();
        result.textContent = "Folder saved.";
    }
    catch (error) {
        if (editor.open)
            folderError.textContent = String(error);
        else
            result.textContent = String(error);
    }
    finally {
        saveFolder.disabled = false;
    }
};
action(move, async () => {
    const count = selected.size;
    await completed(await request("/api/library/placement", "PUT", {
        works: [...selected],
        folder: destination.value || null,
    }));
    await load();
    result.textContent =
        count === 1 ? "Moved one work." : `Moved ${count} works.`;
});
document.querySelector("#newFolder").onclick = () => openEditor();
editFolder.onclick = () => openEditor(folders.find((folder) => folder.id === current));
document.querySelector("#cancelFolder").onclick = () => editor.close();
search.oninput =
    creator.oninput =
        assetType.onchange =
            privacy.onchange =
                sort.onchange =
                    view.onchange =
                        () => {
                            selected.clear();
                            renderWorks();
                        };
libraryUploader({
    available: () => !library.hidden && section === "Library",
    folder: () => current,
    folderOptions,
    credential: () => key.value,
    defaults: folder => readUploadPrivacy(folder, path => request(path).then(completed)),
    reload: () => load(),
});
const loadButton = document.querySelector("#load");
const linked = new URL(location.href).searchParams;
const workspaceSearch = linked.has("q");
if (workspaceSearch)
    search.value = (linked.get("q") ?? "").slice(0, 120);
if (linked.get("view") === "deleted")
    section = "Recently deleted";
let workspaceAction = linked.get("action");
let slackResult = linked.get("slack");
action(loadButton, async () => {
    await load();
    sessionStorage.setItem("cueporchOperator", key.value);
    result.textContent = "Library loaded.";
    if (slackResult) {
        const folder = folders.find(item => item.id === linked.get("folder"));
        const connected = slackResult === "connected";
        slackResult = null;
        history.replaceState(null, "", "/operator/library");
        if (folder) {
            await navigate("Library", folder.id);
            await editFolderSlack(folder);
            workDialogStatus.textContent = connected ? "Slack channel connected." : "Slack channel selection was canceled.";
        }
    }
    const uid = linked.get("work");
    if (uid && workspaceAction) {
        const work = works.find((item) => item.id === uid);
        if (!work)
            throw new Error("This work is no longer in the library.");
        const next = workspaceAction;
        workspaceAction = null;
        await fromWorkspace(work, next);
    }
});
if (key.value && (workspaceSearch || section === "Recently deleted" || slackResult || linked.has("work") && workspaceAction)) {
    loadButton.click();
}
async function fromWorkspace(work, next) {
    const actions = {
        versions: () => versionsDialog(work),
        reviews: () => reviewLinksDialog(work),
        defaults: editUploadDefaults,
        privacy: () => editPrivacy(work),
        "source-privacy": () => restoreVimeoPrivacy(work),
        download: () => infoDialog(work, true),
        copy: () => editTitle(work, true),
        thumbnail: () => thumbnailDialog(work),
        move: () => {
            current = work.folder;
            selected.add(work.id);
            renderTree();
            renderWorks();
            const outcome = document.getElementById("destination");
            outcome.focus();
            result.textContent = "Choose a folder and move the selected video.";
        },
        delete: () => {
            current = work.folder;
            selected.add(work.id);
            renderTree();
            renderWorks();
            document.getElementById("deleteWorks")?.focus();
            result.textContent = "Use Delete selected to move the video to Recently deleted.";
        },
    };
    if (Object.hasOwn(actions, next))
        await actions[next]();
}
action(document.querySelector("#publishImports"), async () => {
    const response = await completed(await request("/api/migration/publish-eligible", "POST"));
    result.textContent =
        "Queued eligible imports: " + (await response.json()).queued;
});
action(document.querySelector("#exportMap"), async () => {
    await saveEmbedMap(await completed(await request("/api/embed-map.csv")));
});
