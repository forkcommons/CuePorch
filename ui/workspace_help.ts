import {checked, sessionMarker, teamRequest} from "./team_api.js";

interface RequestSummary {id: string; subject: string; state: "open" | "resolved"}
interface RequestPage {requests: RequestSummary[]; page: number; next: boolean; can_manage: boolean}
interface Thread extends RequestSummary {
  revision: number;
  can_manage: boolean;
  history: {sender: string; kind: "message" | "resolved" | "reopened"; message: string; at: number}[];
}

const access = document.querySelector<HTMLFormElement>("#helpAccess")!;
const key = access.querySelector<HTMLInputElement>("input")!;
const workspace = document.querySelector<HTMLElement>("#helpWorkspace")!;
const status = document.querySelector<HTMLElement>("#helpStatus")!;
const mode = document.querySelector<HTMLSelectElement>("#helpView select")!;
const list = document.querySelector<HTMLUListElement>("#helpList")!;
const empty = document.querySelector<HTMLElement>("#helpEmpty")!;
const pagination = document.querySelector<HTMLElement>("#helpPages")!;
const pageLabel = document.querySelector<HTMLElement>("#helpPageNumber")!;
const previous = document.querySelector<HTMLButtonElement>("#helpPrevious")!;
const next = document.querySelector<HTMLButtonElement>("#helpNext")!;
const create = document.querySelector<HTMLFormElement>("#helpCreate")!;
const detail = document.querySelector<HTMLElement>("#helpThread")!;
const subject = document.querySelector<HTMLElement>("#helpSubject")!;
const state = document.querySelector<HTMLElement>("#helpState")!;
const historyList = document.querySelector<HTMLOListElement>("#helpHistory")!;
const reply = document.querySelector<HTMLFormElement>("#helpReply")!;
const resolve = document.querySelector<HTMLButtonElement>("#helpResolve")!;
const signIn = document.querySelector<HTMLElement>("#helpSignIn");
const signedInTeam = document.body.dataset.helpTeam === "yes";
let pageNumber = 1;
let thread: Thread | undefined;
let busy = false;

key.value = signedInTeam ? "" : sessionStorage.getItem("cueporchOperator") || "";
const credential = () => signedInTeam ? sessionMarker : key.value;
const route = "support/requests";
const stateLabel = (value: RequestSummary["state"]) => value === "open" ? "Open" : "Resolved";
const requestPath = (id: string) => route + "/" + encodeURIComponent(id);

async function call<T>(path: string, method = "GET", body?: object): Promise<T> {
  const response = await teamRequest(path, method, body, credential());
  if (!response.ok && [401, 403].includes(response.status)) {
    detail.hidden = true;
    if (response.status === 401 || path.startsWith(route + "?view=mine")) {
      workspace.hidden = true;
      if (signIn) signIn.hidden = false;
      access.hidden = signedInTeam;
    }
  }
  return (await checked(response)).json();
}

async function doWork(action: () => Promise<void>) {
  if (busy) return;
  busy = true;
  workspace.setAttribute("aria-busy", "true");
  status.textContent = "";
  try {
    await action();
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    workspace.removeAttribute("aria-busy");
    busy = false;
  }
}

function showThread(model: Thread) {
  thread = model;
  subject.textContent = model.subject;
  state.textContent = stateLabel(model.state);
  resolve.hidden = !model.can_manage;
  resolve.textContent = model.state === "open" ? "Resolve request" : "Reopen request";
  historyList.replaceChildren();
  for (const entry of model.history) {
    const row = document.createElement("li");
    const heading = document.createElement("strong");
    heading.textContent = entry.kind === "message" ? entry.sender
      : `${entry.sender} · Request ${entry.kind}`;
    const when = document.createElement("time");
    const date = new Date(entry.at * 1000);
    when.dateTime = date.toISOString();
    when.textContent = date.toLocaleString();
    row.append(heading, " · ", when);
    if (entry.message) {
      const message = document.createElement("p");
      message.textContent = entry.message;
      row.append(message);
    }
    historyList.append(row);
  }
  const url = new URL(location.href);
  url.searchParams.set("request", model.id);
  window.history.replaceState(null, "", url);
  detail.hidden = false;
}

async function refresh() {
  const data = await call<RequestPage>(route + `?view=${mode.value}&page=${pageNumber}`);
  mode.parentElement!.hidden = !data.can_manage;
  empty.hidden = data.requests.length > 0;
  pagination.hidden = data.page === 1 && !data.next;
  previous.disabled = data.page === 1;
  next.disabled = !data.next;
  pageLabel.textContent = String(data.page);
  list.replaceChildren();
  for (const request of data.requests) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = request.subject;
    button.onclick = () => { void doWork(async () => showThread(await call<Thread>(requestPath(request.id)))); };
    item.append(button, ` · ${stateLabel(request.state)}`);
    list.append(item);
  }
}

async function openWorkspace() {
  await refresh();
  workspace.hidden = false;
  if (signIn) signIn.hidden = true;
  access.hidden = true;
  const params = new URL(location.href).searchParams;
  const active = params.get("request");
  const suggested = params.get("subject");
  if (active) showThread(await call<Thread>(requestPath(active)));
  else if (suggested) {
    (create.elements.namedItem("subject") as HTMLInputElement).value = suggested.slice(0, 120);
    create.closest("details")!.open = true;
  }
}

access.onsubmit = event => { event.preventDefault(); void doWork(openWorkspace); };
mode.onchange = () => { void doWork(async () => { pageNumber = 1; await refresh(); }); };
previous.onclick = () => { void doWork(async () => { pageNumber -= 1; await refresh(); }); };
next.onclick = () => { void doWork(async () => { pageNumber += 1; await refresh(); }); };
create.onsubmit = event => {
  event.preventDefault();
  void doWork(async () => {
    showThread(await call<Thread>(route, "POST", Object.fromEntries(new FormData(create))));
    create.reset();
    create.closest("details")!.open = false;
    pageNumber = 1;
    await refresh();
    status.textContent = "Request sent.";
  });
};
reply.onsubmit = event => {
  event.preventDefault();
  if (!thread) return;
  const id = thread.id;
  void doWork(async () => {
    showThread(await call<Thread>(requestPath(id), "POST", Object.fromEntries(new FormData(reply))));
    reply.reset();
    await refresh();
    status.textContent = "Reply sent.";
  });
};
resolve.onclick = () => {
  if (!thread) return;
  const {id, revision, state: current} = thread;
  void doWork(async () => {
    showThread(await call<Thread>(requestPath(id), "PUT", {revision, state: current === "open" ? "resolved" : "open"}));
    await refresh();
    status.textContent = current === "open" ? "Request resolved." : "Request reopened.";
  });
};

if (credential()) void doWork(openWorkspace);
