import {checked, teamRequest} from "./team_api.js";

interface WorkspaceLink {id: string; name: string; origin: string; revision: string; groups: string[]}
interface Directory {workspaces: WorkspaceLink[]; groups: {id: string; name: string}[]}

const access = document.querySelector<HTMLFormElement>("#workspaceLinkAccess")!;
const key = access.querySelector<HTMLInputElement>("input")!;
const directory = document.querySelector<HTMLElement>("#workspaceLinkDirectory")!;
const status = document.querySelector<HTMLElement>("#workspaceLinkStatus")!;
const selector = document.querySelector<HTMLSelectElement>("#workspaceLinkSelector")!;
const editor = document.querySelector<HTMLFormElement>("#workspaceLinkForm")!;
const title = editor.querySelector<HTMLInputElement>("[name=name]")!;
const origin = editor.querySelector<HTMLInputElement>("[name=origin]")!;
const groups = document.querySelector<HTMLElement>("#workspaceLinkGroups")!;
const remove = document.querySelector<HTMLButtonElement>("#deleteWorkspaceLink")!;
let data: Directory;
let working = false;

function current() {
  return data.workspaces.find(workspace => workspace.id === selector.value);
}

function show() {
  const workspace = current();
  title.value = workspace?.name || "";
  origin.value = workspace?.origin || "";
  remove.disabled = !workspace;
  groups.replaceChildren(...data.groups.map(group => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.name = "group";
    input.type = "checkbox";
    input.value = group.id;
    input.checked = workspace?.groups.includes(group.id) ?? false;
    label.append(input, " ", group.name);
    return label;
  }));
}

async function call(path = "", method = "GET", body = {}) {
  return checked(await teamRequest("workspace-links" + path, method, body, key.value));
}

async function load(id = selector.value) {
  data = await (await call()).json();
  selector.replaceChildren(new Option("New workspace link", ""), ...data.workspaces.map(
    workspace => new Option(workspace.name, workspace.id)));
  if (data.workspaces.some(workspace => workspace.id === id)) selector.value = id;
  show();
  directory.hidden = false;
  access.hidden = true;
}

async function run(action: () => Promise<void>, done = "") {
  if (working) return;
  working = true;
  status.textContent = "";
  try {
    await action();
    status.textContent = done;
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    working = false;
  }
}

access.onsubmit = event => {
  event.preventDefault();
  void run(async () => {
    await load();
    sessionStorage.setItem("cueporchOperator", key.value);
  });
};
selector.onchange = show;
editor.onsubmit = event => {
  event.preventDefault();
  void run(async () => {
    const workspace = current();
    const body = {name: title.value, origin: origin.value, groups: new FormData(editor).getAll("group"),
      revision: workspace?.revision};
    const {id}: {id: string} = await (await call(workspace ? `/${workspace.id}` : "",
      workspace ? "PUT" : "POST", body)).json();
    await load(id);
  }, "Workspace link saved.");
};
remove.onclick = () => void run(async () => {
  const workspace = current();
  if (!workspace) return;
  await call(`/${workspace.id}`, "DELETE", {revision: workspace.revision});
  await load("");
}, "Workspace link removed.");
document.querySelector<HTMLButtonElement>("#reloadWorkspaceLinks")!.onclick = () => void run(() => load());
key.value = sessionStorage.getItem("cueporchOperator") || "";
if (key.value) void run(() => load());
