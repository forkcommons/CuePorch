import { checked, sessionMarker, teamRequest } from "./team_api.js";
const element = (id) => document.getElementById(id);
const credentialInput = element("credential");
const identify = element("identify");
const status = element("status");
const reload = element("reload");
const channel = element("channel");
const curation = element("curation");
const moderation = element("moderation");
const viewerAccess = element("viewerAccess");
const viewerEmails = element("viewerEmails");
const videoList = element("videos");
const choices = element("choices");
const addVideo = element("addVideo");
const memberList = element("members");
let credential = sessionStorage.getItem("cueporchOperator") || sessionMarker;
let manage = false;
let editor;
let roster;
let viewers;
let busy = false;
const endpoint = () => "channels/" + encodeURIComponent(channel.value);
async function run(task) {
    if (busy)
        return;
    busy = true;
    for (const control of [channel, curation, moderation, viewerAccess, reload, credentialInput, identify.querySelector("button")]) {
        control.disabled = true;
    }
    status.textContent = "";
    try {
        await task();
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        for (const control of [channel, curation, moderation, viewerAccess, reload, credentialInput, identify.querySelector("button")]) {
            control.disabled = false;
        }
        busy = false;
    }
}
function action(text, label, disabled, update) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.setAttribute("aria-label", label);
    button.disabled = disabled;
    button.addEventListener("click", () => {
        update();
        renderVideos();
    });
    return button;
}
function renderVideos() {
    if (!editor)
        return;
    const videos = editor.videos;
    videoList.replaceChildren(...videos.map((video, index) => {
        const row = document.createElement("li");
        row.append(video.title, " ", action("Move up", "Move up " + video.title, index === 0, () => {
            [videos[index - 1], videos[index]] = [videos[index], videos[index - 1]];
        }), action("Move down", "Move down " + video.title, index + 1 === videos.length, () => {
            [videos[index + 1], videos[index]] = [videos[index], videos[index + 1]];
        }), action("Remove", "Remove " + video.title, false, () => videos.splice(index, 1)));
        return row;
    }));
    const included = new Set(videos.map(video => video.id));
    choices.replaceChildren(...editor.choices.filter(video => !included.has(video.id))
        .map(video => new Option(video.title, video.id)));
    addVideo.disabled = choices.options.length === 0;
}
async function loadChannel() {
    curation.hidden = moderation.hidden = viewerAccess.hidden = true;
    viewerEmails.value = "";
    editor = roster = viewers = undefined;
    if (!channel.value)
        return;
    const response = await teamRequest(endpoint(), "GET", {}, credential);
    if (response.ok) {
        editor = await response.json();
        renderVideos();
        curation.hidden = false;
    }
    else if (!manage || response.status !== 404) {
        await checked(response);
    }
    if (!manage)
        return;
    roster = await (await checked(await teamRequest(endpoint() + "/moderators", "GET", {}, credential))).json();
    memberList.replaceChildren(...roster.members.map(member => {
        const label = document.createElement("label");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = member.id;
        input.checked = member.assigned;
        label.append(input, " ", member.name);
        return label;
    }));
    if (!roster.members.length)
        memberList.textContent = "No eligible contributors.";
    moderation.hidden = false;
    const viewerResponse = await teamRequest(endpoint() + "/viewers", "GET", {}, credential);
    if (viewerResponse.ok) {
        viewers = await viewerResponse.json();
        viewerEmails.value = viewers.emails.join("\n");
        viewerAccess.hidden = false;
    }
    else if (viewerResponse.status !== 404) {
        await checked(viewerResponse);
    }
}
async function loadChannels() {
    const preferred = channel.value || new URLSearchParams(location.search).get("select");
    identify.hidden = false;
    channel.replaceChildren();
    curation.hidden = moderation.hidden = viewerAccess.hidden = true;
    viewerEmails.value = "";
    const result = await (await checked(await teamRequest("channels", "GET", {}, credential))).json();
    manage = result.manage;
    identify.hidden = true;
    channel.replaceChildren(...result.channels.map(item => new Option(item.title, item.id)));
    if (result.channels.some(item => item.id === preferred))
        channel.value = preferred;
    if (!channel.value)
        status.textContent = "No channels available.";
    await loadChannel();
}
async function save(path, values, message) {
    await run(async () => {
        await checked(await teamRequest(path, "PUT", values, credential));
        await loadChannel();
        status.textContent = message;
    });
}
identify.addEventListener("submit", event => {
    event.preventDefault();
    credential = credentialInput.value.trim();
    void run(loadChannels);
});
reload.addEventListener("click", () => void run(loadChannels));
channel.addEventListener("change", () => void run(loadChannel));
addVideo.addEventListener("click", () => {
    const item = editor?.choices.find(video => video.id === choices.value);
    if (item && editor) {
        editor.videos.push(item);
        renderVideos();
    }
});
element("saveVideos").addEventListener("click", () => {
    if (editor)
        void save(endpoint(), { revision: editor.revision, videos: editor.videos.map(video => video.id) }, "Channel videos saved.");
});
element("saveModerators").addEventListener("click", () => {
    const members = Array.from(memberList.querySelectorAll("input:checked")).map(input => input.value);
    if (roster)
        void save(endpoint() + "/moderators", { revision: roster.revision, members }, "Moderators saved.");
});
element("saveViewers").addEventListener("click", () => {
    const emails = viewerEmails.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    if (viewers)
        void save(endpoint() + "/viewers", { revision: viewers.revision, emails }, "Invited viewers saved.");
});
void run(loadChannels);
