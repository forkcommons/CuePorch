import { checked, teamRequest } from "./team_api.js";
const access = document.querySelector("#workspaceLinkAccess");
const key = access.querySelector("input");
const directory = document.querySelector("#workspaceLinkDirectory");
const status = document.querySelector("#workspaceLinkStatus");
const selector = document.querySelector("#workspaceLinkSelector");
const editor = document.querySelector("#workspaceLinkForm");
const title = editor.querySelector("[name=name]");
const origin = editor.querySelector("[name=origin]");
const groups = document.querySelector("#workspaceLinkGroups");
const remove = document.querySelector("#deleteWorkspaceLink");
let data;
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
    selector.replaceChildren(new Option("New workspace link", ""), ...data.workspaces.map(workspace => new Option(workspace.name, workspace.id)));
    if (data.workspaces.some(workspace => workspace.id === id))
        selector.value = id;
    show();
    directory.hidden = false;
    access.hidden = true;
}
async function run(action, done = "") {
    if (working)
        return;
    working = true;
    status.textContent = "";
    try {
        await action();
        status.textContent = done;
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
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
        const body = { name: title.value, origin: origin.value, groups: new FormData(editor).getAll("group"),
            revision: workspace?.revision };
        const { id } = await (await call(workspace ? `/${workspace.id}` : "", workspace ? "PUT" : "POST", body)).json();
        await load(id);
    }, "Workspace link saved.");
};
remove.onclick = () => void run(async () => {
    const workspace = current();
    if (!workspace)
        return;
    await call(`/${workspace.id}`, "DELETE", { revision: workspace.revision });
    await load("");
}, "Workspace link removed.");
document.querySelector("#reloadWorkspaceLinks").onclick = () => void run(() => load());
key.value = sessionStorage.getItem("cueporchOperator") || "";
if (key.value)
    void run(() => load());
