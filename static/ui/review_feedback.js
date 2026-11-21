import { renderCommentText } from "./review_comment_text.js";
import { configureReviewPush } from "./review_push.js";
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
let stepping = false;
const frameCache = new Map();
async function loadReviewFrames(source, position) {
    const endpoint = player.dataset.frames;
    if (!endpoint)
        return undefined;
    const url = new URL(endpoint, location.href);
    const rendition = new URL(source).searchParams.get("rendition");
    if (rendition)
        url.searchParams.set("rendition", rendition);
    url.searchParams.set("at", String(Math.floor(position / 10) * 10));
    const cached = frameCache.get(url.href);
    if (cached)
        return cached;
    try {
        const response = await fetch(url);
        if (!response.ok)
            return undefined;
        const { frames } = await response.json();
        if (!Array.isArray(frames) || !frames.every((value, i) => Number.isFinite(value) && value >= 0 && (i === 0 || value > frames[i - 1])))
            return undefined;
        if (frameCache.size >= 4)
            frameCache.delete(frameCache.keys().next().value);
        frameCache.set(url.href, frames);
        return frames;
    }
    catch {
        return undefined;
    }
}
async function step(direction) {
    if (stepping || !(frameRate > 0) || !Number.isFinite(player.duration))
        return;
    stepping = true;
    player.pause();
    const source = player.src;
    const position = player.currentTime;
    try {
        const frames = await loadReviewFrames(source, position);
        if (player.src !== source || !player.paused || Math.abs(player.currentTime - position) > .002)
            return;
        previousFrame.title = nextFrame.title = frames?.length ? "" : "Approximate frame step";
        if (!frames?.length) {
            const next = Math.round(position * frameRate) + direction;
            player.currentTime = Math.max(0, Math.min(next / frameRate, player.duration));
            return;
        }
        let index = -1;
        while (index + 1 < frames.length && frames[index + 1] <= position + .000001)
            index++;
        const target = index + direction;
        if (target < 0 || target >= frames.length)
            return;
        // Seek just inside the frame so browser timestamp rounding does not land on its predecessor.
        const gap = target + 1 < frames.length ? frames[target + 1] - frames[target] : Infinity;
        player.currentTime = Math.min(player.duration, frames[target] + Math.min(.001, gap / 3));
    }
    finally {
        stepping = false;
    }
}
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
const endCard = document.querySelector("#reviewEndCard");
const replay = document.querySelector("#reviewReplay");
player.addEventListener("ended", () => { endCard.hidden = false; });
player.addEventListener("play", () => { endCard.hidden = true; });
replay.onclick = () => {
    player.currentTime = 0;
    player.play().catch(() => { });
};
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
const mentions = document.querySelector("#reviewMentions");
const noticeList = document.querySelector("#reviewNoticeList");
const noticeMode = document.querySelector("#reviewNoticeMode");
const noticeCount = document.querySelector("#reviewNoticeCount");
const mentionIds = new Set();
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
    node.id = "comment-" + entry.id;
    node.tabIndex = -1;
    const name = document.createElement("strong");
    name.textContent = entry.author;
    const text = document.createElement("p");
    renderCommentText(text, entry.body);
    node.append(name, text, renderReactions(entry, canReact));
    return node;
}
function renderMentions() {
    const end = message.selectionStart;
    const match = /(?:^|\s)@([^\s@]+)$/.exec(message.value.slice(0, end));
    const options = (match && data)
        ? data.mention_options.filter((item) => item.name.toLocaleLowerCase().startsWith(match[1].toLocaleLowerCase())).slice(0, 8)
        : [];
    mentions.replaceChildren();
    mentions.hidden = !options.length;
    if (!match)
        return;
    for (const option of options) {
        const select = document.createElement("button");
        select.type = "button";
        select.textContent = "Mention " + option.name;
        select.onclick = () => {
            message.setRangeText("@" + option.name + " ", end - match[1].length - 1, end, "end");
            mentionIds.add(option.id);
            mentions.hidden = true;
            message.focus();
        };
        mentions.append(select);
    }
}
function renderNotices(inbox, refresh = false) {
    const label = inbox.items.length ? `Notifications (${inbox.items.length})` : "Notifications";
    if (noticeCount.textContent !== label)
        noticeCount.textContent = label;
    if (!noticeMode.disabled)
        noticeMode.value = inbox.mode;
    if (!refresh && noticeList.contains(document.activeElement))
        return;
    noticeList.replaceChildren();
    for (const item of inbox.items) {
        const entry = document.createElement("li");
        const linked = document.createElement("a");
        linked.href = "#comment-" + item.comment;
        const label = item.reason === "mention" ? " mentioned you" : item.reason === "reply" ? " replied to you" : " posted a comment";
        linked.textContent = item.author + label;
        linked.onclick = async (event) => {
            event.preventDefault();
            try {
                await call("/notices", "PUT", { read: item.comment });
                search.value = "";
                showResolved.checked = true;
                await load(true);
                window.location.hash = linked.hash;
                focusComment();
            }
            catch (error) {
                feedbackMessage.textContent = String(error);
            }
        };
        entry.append(linked);
        noticeList.append(entry);
    }
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
function focusComment() {
    const hash = location.hash;
    if (!/^#comment-[a-f0-9]{32}$/.test(hash))
        return;
    const target = document.getElementById(hash.slice(1));
    target?.scrollIntoView({ block: "center" });
    target?.focus({ preventScroll: true });
}
async function load(refreshNotices = false) {
    const updated = await (await call()).json();
    data = updated;
    editor.hidden = !updated.permissions.allow_comment;
    resolved.hidden = !updated.permissions.allow_resolved;
    decision.hidden = !updated.permissions.allow_status;
    decisionStatus.value = updated.status;
    status.textContent = updated.status;
    render();
    renderNotices(updated.notices, refreshNotices);
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
message.oninput = renderMentions;
noticeMode.onchange = async () => {
    noticeMode.disabled = true;
    try {
        await call("/notices", "PUT", { mode: noticeMode.value });
    }
    catch (error) {
        feedbackMessage.textContent = String(error);
    }
    finally {
        noticeMode.disabled = false;
    }
};
editor.onsubmit = async (event) => {
    event.preventDefault();
    action.disabled = true;
    try {
        await call("", "POST", {
            author: author.value,
            body: message.value,
            seconds: pinned ?? player.currentTime,
            parent,
            mentions: [...mentionIds],
        });
        message.value = "";
        mentionIds.clear();
        mentions.hidden = true;
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
load().then(focusComment).catch((error) => { feedbackMessage.textContent = String(error); });
configureReviewPush(endpoint, feedbackMessage).catch((error) => { feedbackMessage.textContent = String(error); });
window.setInterval(async () => {
    if (!data)
        return;
    try {
        renderNotices(await (await call("/notices")).json());
    }
    catch {
        // The next navigation or user action reports any expired access.
    }
}, 5000);
