interface CommentEntry {
  id: string;
  parent: string | null;
  author: string;
  body: string;
  seconds: number;
  created: number;
  resolved: boolean;
}

interface Feedback {
  permissions: {
    allow_comment: boolean;
    allow_resolved: boolean;
    allow_status: boolean;
  };
  status: string;
  comments: CommentEntry[];
}

const endpoint = document.querySelector<HTMLElement>("#reviewFeedback")!.dataset.feedback!;
const player = document.querySelector<HTMLVideoElement>("video")!;
const recommendations = document.querySelector<HTMLElement>("#reviewRecommendations");
player.addEventListener("ended", () => { if (recommendations) recommendations.hidden = false; });
player.addEventListener("play", () => { if (recommendations) recommendations.hidden = true; });
const comments = document.querySelector<HTMLOListElement>("#reviewComments")!;
const editor = document.querySelector<HTMLFormElement>("#writeFeedback")!;
const author = editor.elements.namedItem("author") as HTMLInputElement;
const message = editor.elements.namedItem("body") as HTMLTextAreaElement;
const action = document.querySelector<HTMLButtonElement>("#submitComment")!;
const pin = document.querySelector<HTMLElement>("#pin")!;
const cancel = document.querySelector<HTMLButtonElement>("#cancelReply")!;
const status = document.querySelector<HTMLElement>("#reviewStatus")!;
const decision = document.querySelector<HTMLFormElement>("#reviewDecision")!;
const decisionStatus = decision.elements.namedItem("status") as HTMLSelectElement;
const feedbackMessage = document.querySelector<HTMLElement>("#feedbackMessage")!;
const search = document.querySelector<HTMLInputElement>("#searchFeedback")!;
const sort = document.querySelector<HTMLSelectElement>("#sortFeedback")!;
const resolved = document.querySelector<HTMLElement>("#resolvedFilter")!;
const showResolved = resolved.querySelector<HTMLInputElement>("input")!;

let data: Feedback | null = null;
let parent: string | null = null;
let pinned: number | null = null;

function displayTime(seconds: number) {
  return Math.floor(seconds / 60) + ":" + (seconds % 60).toFixed(2).padStart(5, "0");
}

async function call(path = "", method = "GET", body?: object) {
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

function renderEntry(entry: CommentEntry) {
  const node = document.createElement("li");
  const name = document.createElement("strong");
  name.textContent = entry.author;
  const text = document.createElement("p");
  text.textContent = entry.body;
  node.append(name, text);
  return node;
}

function render() {
  if (!data) return;
  const filter = search.value.trim().toLocaleLowerCase();
  const includes = (entry: CommentEntry) =>
    (entry.author + " " + entry.body).toLocaleLowerCase().includes(filter);
  const threads = data.comments.filter((entry) => !entry.parent && (!entry.resolved || showResolved.checked));
  if (sort.value === "newest") threads.sort((a, b) => b.created - a.created);
  comments.replaceChildren();
  for (const thread of threads) {
    const replies = data.comments.filter((entry) => entry.parent === thread.id);
    if (filter && !includes(thread) && !replies.some(includes)) continue;
    const item = renderEntry(thread);
    const jump = document.createElement("button");
    jump.type = "button";
    jump.textContent = displayTime(thread.seconds);
    jump.setAttribute("aria-label", "Go to " + displayTime(thread.seconds));
    jump.onclick = () => { player.currentTime = thread.seconds; };
    item.prepend(jump, " ");
    if (thread.resolved) item.append("Resolved");
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
  const updated: Feedback = await (await call()).json();
  data = updated;
  editor.hidden = !updated.permissions.allow_comment;
  resolved.hidden = !updated.permissions.allow_resolved;
  decision.hidden = !updated.permissions.allow_status;
  decisionStatus.value = updated.status;
  status.textContent = updated.status;
  render();
}

document.querySelector<HTMLButtonElement>("#markFrame")!.onclick = () => {
  if (parent) resetPin();
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
  } catch (error) {
    feedbackMessage.textContent = String(error);
  } finally {
    action.disabled = false;
  }
};

decision.onsubmit = async (event) => {
  event.preventDefault();
  const save = decision.querySelector<HTMLButtonElement>("button")!;
  save.disabled = true;
  try {
    await call("/status", "PUT", { status: decisionStatus.value });
    await load();
    feedbackMessage.textContent = "Status saved.";
  } catch (error) {
    feedbackMessage.textContent = String(error);
  } finally {
    save.disabled = false;
  }
};

load().catch((error) => { feedbackMessage.textContent = String(error); });
