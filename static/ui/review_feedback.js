import { renderCommentText } from "./review_comment_text.js";
const endpoint = document.querySelector("#reviewFeedback").dataset.feedback;
const upload = document.querySelector("#uploadReviewVersion");
if (upload) {
    upload.onsubmit = async (event) => {
        event.preventDefault();
        const button = upload.querySelector("button");
        const message = document.querySelector("#reviewUploadMessage");
        button.disabled = true;
        message.textContent = "Preparing new version…";
        try {
            const response = await fetch(upload.action, {
                method: "POST",
                headers: { "X-CuePorch-Preview": upload.dataset.token },
                body: new FormData(upload),
            });
            if (!response.ok) {
                const page = new DOMParser().parseFromString(await response.text(), "text/html");
                throw new Error(page.querySelector("p")?.textContent || "Unable to upload this version.");
            }
            window.location.assign(location.pathname);
        }
        catch (error) {
            message.textContent = String(error);
            button.disabled = false;
        }
    };
}
const player = document.querySelector("video");
const loop = document.querySelector("#reviewLoop");
const speed = document.querySelector("#reviewSpeed");
const previousFrame = document.querySelector("#previousFrame");
const nextFrame = document.querySelector("#nextFrame");
let frameRate = Number(player.dataset.frameRate);
const step = (direction) => {
    if (!(frameRate > 0) || !Number.isFinite(player.duration))
        return;
    player.pause();
    const next = Math.round(player.currentTime * frameRate) + direction;
    player.currentTime = Math.max(0, Math.min(next / frameRate, player.duration));
};
previousFrame.onclick = () => step(-1);
nextFrame.onclick = () => step(1);
loop.onchange = () => { player.loop = loop.checked; };
speed.onchange = () => { player.playbackRate = Number(speed.value); };
const quality = document.querySelector("#reviewQuality");
if (quality) {
    quality.onchange = () => {
        const position = player.currentTime;
        const wasPlaying = !player.paused;
        const captionModes = Array.from(player.textTracks, (track) => track.mode);
        frameRate = Number(quality.selectedOptions[0].dataset.rate);
        previousFrame.hidden = nextFrame.hidden = !(frameRate > 0);
        quality.disabled = true;
        const listeners = new AbortController();
        player.addEventListener("loadedmetadata", () => {
            listeners.abort();
            player.currentTime = Math.min(position, player.duration);
            player.playbackRate = Number(speed.value);
            for (let index = 0; index < player.textTracks.length; index++) {
                player.textTracks[index].mode = captionModes[index] || "disabled";
            }
            quality.disabled = false;
            if (wasPlaying)
                player.play().catch(() => { });
        }, { signal: listeners.signal });
        player.addEventListener("error", () => {
            listeners.abort();
            quality.disabled = false;
        }, { signal: listeners.signal });
        player.src = quality.value;
    };
}
const recommendations = document.querySelector("#reviewRecommendations");
player.addEventListener("ended", () => { if (recommendations)
    recommendations.hidden = false; });
player.addEventListener("play", () => { if (recommendations)
    recommendations.hidden = true; });
const comments = document.querySelector("#reviewComments");
const editor = document.querySelector("#writeFeedback");
const author = editor.elements.namedItem("author");
const message = editor.elements.namedItem("body");
const action = document.querySelector("#submitComment");
const pin = document.querySelector("#pin");
const cancel = document.querySelector("#cancelReply");
const status = document.querySelector("#reviewStatus");
const decision = document.querySelector("#reviewDecision");
const decisionStatus = decision.elements.namedItem("status");
const feedbackMessage = document.querySelector("#feedbackMessage");
const search = document.querySelector("#searchFeedback");
const sort = document.querySelector("#sortFeedback");
const resolved = document.querySelector("#resolvedFilter");
const showResolved = resolved.querySelector("input");
let data = null;
let parent = null;
let pinned = null;
function displayTime(seconds) {
    return Math.floor(seconds / 60) + ":" + (seconds % 60).toFixed(2).padStart(5, "0");
}
async function call(path = "", method = "GET", body) {
    const response = await fetch(endpoint + path, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
        const detail = new DOMParser().parseFromString(await response.text(), "text/html");
        throw new Error(detail.querySelector("p")?.textContent || "Unable to update feedback.");
    }
    return response;
}
function resetPin() {
    parent = null;
    pinned = null;
    pin.textContent = "";
    action.textContent = "Post comment";
    cancel.hidden = true;
}
const reactionOptions = [
    ["👍", "Like"], ["❤️", "Love"], ["🎉", "Celebrate"], ["💡", "Good idea"],
];
function renderReactions(entry, canReact) {
    const controls = document.createElement("span");
    controls.className = "review-reactions";
    for (const [index, [emoji, label]] of reactionOptions.entries()) {
        const reaction = entry.reactions.find((item) => item.emoji === emoji);
        const count = reaction?.count ?? 0;
        if (!canReact) {
            if (count)
                controls.append(` ${emoji} ${count}`);
            continue;
        }
        const button = document.createElement("button");
        button.id = `reaction-${entry.id}-${index}`;
        button.type = "button";
        button.textContent = count ? `${emoji} ${count}` : emoji;
        button.setAttribute("aria-label", label);
        button.setAttribute("aria-pressed", String(reaction?.selected ?? false));
        button.onclick = async () => {
            button.disabled = true;
            try {
                await call(`/${entry.id}/reaction`, "PUT", { emoji, selected: !reaction?.selected });
                await load();
                document.getElementById(button.id)?.focus();
            }
            catch (error) {
                feedbackMessage.textContent = String(error);
                button.disabled = false;
            }
        };
        controls.append(button);
    }
    return controls;
}
function renderEntry(entry, canReact) {
    const node = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = entry.author;
    const text = document.createElement("p");
    renderCommentText(text, entry.body);
    node.append(name, text, renderReactions(entry, canReact));
    return node;
}
function render() {
    if (!data)
        return;
    const filter = search.value.trim().toLocaleLowerCase();
    const includes = (entry) => (entry.author + " " + entry.body).toLocaleLowerCase().includes(filter);
    const threads = data.comments.filter((entry) => !entry.parent && (!entry.resolved || showResolved.checked));
    if (sort.value === "newest")
        threads.sort((a, b) => b.created - a.created);
    comments.replaceChildren();
    for (const thread of threads) {
        const replies = data.comments.filter((entry) => entry.parent === thread.id);
        if (filter && !includes(thread) && !replies.some(includes))
            continue;
        const canReact = data.permissions.allow_comment && !thread.resolved;
        const item = renderEntry(thread, canReact);
        const jump = document.createElement("button");
        jump.type = "button";
        jump.textContent = displayTime(thread.seconds);
        jump.setAttribute("aria-label", "Go to " + displayTime(thread.seconds));
        jump.onclick = () => { player.currentTime = thread.seconds; };
        item.prepend(jump, " ");
        if (thread.resolved)
            item.append("Resolved");
        if (data.permissions.allow_comment && !thread.resolved) {
            const reply = document.createElement("button");
            reply.type = "button";
            reply.textContent = "Reply";
            reply.onclick = () => {
                parent = thread.id;
                pinned = thread.seconds;
                pin.textContent = "Replying to " + thread.author + " at " + displayTime(thread.seconds);
                action.textContent = "Post reply";
                cancel.hidden = false;
                message.focus();
            };
            item.append(reply);
        }
        if (replies.length) {
            const list = document.createElement("ul");
            list.append(...replies.map((reply) => renderEntry(reply, canReact)));
            item.append(list);
        }
        comments.append(item);
    }
    if (!comments.children.length) {
        const empty = document.createElement("li");
        empty.textContent = filter ? "No matching comments." : "No comments yet.";
        comments.append(empty);
    }
}
async function load() {
    const updated = await (await call()).json();
    data = updated;
    editor.hidden = !updated.permissions.allow_comment;
    resolved.hidden = !updated.permissions.allow_resolved;
    decision.hidden = !updated.permissions.allow_status;
    decisionStatus.value = updated.status;
    status.textContent = updated.status;
    render();
}
document.querySelector("#markFrame").onclick = () => {
    if (parent)
        resetPin();
    player.pause();
    pinned = player.currentTime;
    pin.textContent = "Comment at " + displayTime(pinned);
    message.focus();
};
cancel.onclick = resetPin;
search.oninput = render;
sort.onchange = render;
showResolved.onchange = render;
editor.onsubmit = async (event) => {
    event.preventDefault();
    action.disabled = true;
    try {
        await call("", "POST", {
            author: author.value,
            body: message.value,
            seconds: pinned ?? player.currentTime,
            parent,
        });
        message.value = "";
        resetPin();
        await load();
        feedbackMessage.textContent = "Comment posted.";
    }
    catch (error) {
        feedbackMessage.textContent = String(error);
    }
    finally {
        action.disabled = false;
    }
};
decision.onsubmit = async (event) => {
    event.preventDefault();
    const save = decision.querySelector("button");
    save.disabled = true;
    try {
        await call("/status", "PUT", { status: decisionStatus.value });
        await load();
        feedbackMessage.textContent = "Status saved.";
    }
    catch (error) {
        feedbackMessage.textContent = String(error);
    }
    finally {
        save.disabled = false;
    }
};
load().catch((error) => { feedbackMessage.textContent = String(error); });
