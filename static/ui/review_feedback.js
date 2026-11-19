const endpoint = document.querySelector("#reviewFeedback").dataset.feedback;
const player = document.querySelector("video");
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
function renderEntry(entry) {
    const node = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = entry.author;
    const text = document.createElement("p");
    text.textContent = entry.body;
    node.append(name, text);
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
        const item = renderEntry(thread);
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
            list.append(...replies.map(renderEntry));
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
export {};
