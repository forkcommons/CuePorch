import {renderCommentText} from "./review_comment_text.js";

export interface ChatMessage {id: string; name: string; message: string; sent: number; mine: boolean;}
export interface ChatState {enabled: boolean; interval: number; banned: boolean; messages: ChatMessage[];}
interface GuestChat extends ChatState {active: boolean; session: string;}

export function renderChat(list: HTMLOListElement, messages: ChatMessage[], actions: (message: ChatMessage) => HTMLElement[], roomOpen = true) {
  const revision = `${roomOpen}:` + messages.map((message) => `${message.id}:${Number(message.mine)}`).join(",");
  if (list.dataset.revision === revision) return;
  const focused = document.activeElement instanceof HTMLElement ? document.activeElement.dataset.chatAction : undefined;
  const next = messages.map((message) => {
    const item = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = message.name;
    item.append(name, " ");
    renderCommentText(item, message.message);
    item.append(...actions(message));
    return item;
  });
  list.replaceChildren(...next);
  list.dataset.revision = revision;
  if (focused) list.querySelector<HTMLElement>(`[data-chat-action="${CSS.escape(focused)}"]`)?.focus();
}

export function messageAction(label: string, message: ChatMessage, perform: () => void) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.dataset.chatAction = label + message.id;
  button.setAttribute("aria-label", `${label} (${message.name})`);
  button.onclick = perform;
  return button;
}

const audience = document.querySelector<HTMLElement>("#audienceChat");
if (audience) {
  const section = audience;
  const path = section.dataset.endpoint!;
  const lobby = document.querySelector<HTMLElement>("#eventLobby");
  const viewer = lobby?.dataset.viewerGrant || "";
  const form = section.querySelector<HTMLFormElement>("#sendChatMessage")!;
  const history = section.querySelector<HTMLOListElement>("#chatMessages")!;
  const notice = section.querySelector<HTMLElement>("#chatStatus")!;
  const identity = form.elements.namedItem("name") as HTMLInputElement;
  const message = form.elements.namedItem("message") as HTMLInputElement;
  let session = readStorage(path);
  let pending: Promise<boolean> | undefined;

  function readStorage(key: string) {
    try { return sessionStorage.getItem(key) || ""; } catch { return ""; }
  }

  function saveStorage(key: string, value: string) {
    try { sessionStorage.setItem(key, value); } catch { /* Third-party frames may disable storage. */ }
  }

  identity.value = readStorage(path + ":name");

  function render(state: GuestChat) {
    session = state.session;
    saveStorage(path, session);
    section.hidden = !state.enabled;
    form.hidden = !state.active || state.banned;
    renderChat(history, state.messages, (entry) => entry.mine && state.active
      ? [messageAction("Remove message", entry, () => void send({type: "remove", id: entry.id}))] : [], state.active);
    if (state.banned) notice.textContent = "You cannot send messages in this event.";
    else if (!state.active) notice.textContent = "Chat has ended.";
  }

  async function fetchChat(body?: object) {
    try {
      const headers: Record<string, string> = {"X-CuePorch-Chat-Embed": section.dataset.embed || "no"};
      if (viewer) headers["X-CuePorch-Event-Viewer"] = viewer;
      if (lobby?.dataset.audienceAccess) headers["X-CuePorch-Event-Access"] = lobby.dataset.audienceAccess;
      if (session) headers["X-CuePorch-Chat-Session"] = session;
      if (body) headers["Content-Type"] = "application/json";
      const response = await fetch(path, {method: body ? "POST" : "GET", headers,
        body: body ? JSON.stringify(body) : undefined});
      if (response.status === 404) {
        section.hidden = true;
        return false;
      }
      if (!response.ok) {
        const detail = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p")?.textContent;
        throw new Error(detail || "Chat is unavailable.");
      }
      if (body) notice.textContent = "";
      render(await response.json() as GuestChat);
      return true;
    } catch (error) {
      if (body) notice.textContent = error instanceof Error ? error.message : String(error);
      return false;
    }
  }

  async function send(body?: object) {
    const previous = pending;
    if (previous && !body) return;
    const mutation = previous ? previous.then(() => fetchChat(body)) : fetchChat(body);
    pending = mutation;
    try { return await mutation; } finally { if (pending === mutation) pending = undefined; }
  }

  form.onsubmit = async (event) => {
    event.preventDefault();
    saveStorage(path + ":name", identity.value);
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), (n) => n.toString(16).padStart(2, "0")).join("");
    if (await send({type: "message", id, name: identity.value, message: message.value})) message.value = "";
  };
  void send();
  window.setInterval(() => { if (!document.hidden) void send(); }, 3000);
}
