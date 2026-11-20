import { checked, roleLabels, teamRequest } from "./team_api.js";
const access = document.querySelector("#teamManagementAccess");
const credential = access.querySelector("input");
const management = document.querySelector("#teamManagement");
const status = document.querySelector("#teamMembersStatus");
const invite = document.querySelector("#teamInvite");
const invitationBox = document.querySelector("#teamInvitationLink");
const invitationLink = invitationBox.querySelector("input");
const invitations = document.querySelector("#teamInvitations");
const members = document.querySelector("#teamMemberList");
const sharing = document.querySelector("#teamShares");
const shareWork = document.querySelector("#teamShareWork");
const shareButton = document.querySelector("#shareTeamVideo");
const shared = document.querySelector("#teamSharedVideos");
const folderTitle = document.querySelector("#teamFoldersTitle");
const shareFolder = document.querySelector("#teamShareFolder");
const shareFolderButton = document.querySelector("#shareTeamFolder");
const sharedFolders = document.querySelector("#teamSharedFolders");
const activity = document.querySelector("#teamMembershipActivity");
const date = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
let selected;
let working = false;
let names;
function call(path, method = "GET", body = {}) {
    return teamRequest(path, method, body, credential.value).then(checked);
}
async function task(action, success = "") {
    if (working)
        return;
    working = true;
    status.textContent = "";
    try {
        await action();
        status.textContent = success;
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        working = false;
    }
}
function button(text, action) {
    const control = document.createElement("button");
    control.type = "button";
    control.textContent = text;
    control.onclick = action;
    return control;
}
function clearInvitation() {
    invitationLink.value = "";
    invitationBox.hidden = true;
}
async function loadShares() {
    if (!selected)
        return;
    const member = selected;
    const { works, folders } = await (await call(`members/${member.id}/works`)).json();
    sharing.querySelector("h3").textContent = `Videos shared with ${member.name}`;
    folderTitle.textContent = `Folders shared with ${member.name}`;
    sharing.hidden = false;
    shareWork.replaceChildren();
    shared.replaceChildren();
    for (const work of works) {
        if (work.shared) {
            const row = document.createElement("li");
            row.append(`${work.title} `, button(`Revoke ${work.title}`, () => void task(async () => {
                await call(`members/${member.id}/works/${work.id}`, "DELETE");
                await loadShares();
            }, "Video access revoked.")));
            shared.append(row);
        }
        else
            shareWork.add(new Option(work.title, work.id));
    }
    shareButton.disabled = !shareWork.options.length;
    shareFolder.replaceChildren();
    sharedFolders.replaceChildren();
    for (const folder of folders) {
        if (folder.shared) {
            const row = document.createElement("li");
            row.append(`${folder.title} `, button(`Revoke folder ${folder.title}`, () => void task(async () => {
                await call(`members/${member.id}/folders/${folder.id}`, "DELETE");
                await loadShares();
            }, "Folder access revoked.")));
            sharedFolders.append(row);
        }
        else
            shareFolder.add(new Option(folder.title, folder.id));
    }
    shareFolderButton.disabled = !shareFolder.options.length;
}
shareButton.onclick = () => void task(async () => {
    if (!selected || !shareWork.value)
        return;
    await call(`members/${selected.id}/works/${shareWork.value}`, "PUT");
    await loadShares();
}, "Video shared.");
shareFolderButton.onclick = () => void task(async () => {
    if (!selected || !shareFolder.value)
        return;
    await call(`members/${selected.id}/folders/${shareFolder.value}`, "PUT");
    await loadShares();
}, "Folder shared.");
async function loadActivity() {
    const { events } = await (await call("audit")).json();
    const list = activity.querySelector("ul");
    list.replaceChildren();
    for (const event of events) {
        const item = document.createElement("li");
        const action = event.action.replaceAll("_", " ");
        const actor = names.get(event.actor.replace(/^team:/, "")) || event.actor;
        const subject = names.get(event.subject) || event.subject;
        const detail = event.detail && !/^(video|folder)_/.test(event.action) ? ` (${event.detail})` : "";
        item.textContent = `${date.format(event.at * 1000)} · ${actor} · ${action} · ${subject}${detail}`;
        list.append(item);
    }
}
async function loadMembers() {
    const roster = await (await call("members")).json();
    names = new Map([...roster.members.map(member => [member.id, member.name]),
        ...roster.invitations.map(pending => [pending.id, pending.email])]);
    invitations.replaceChildren();
    members.replaceChildren();
    for (const pending of roster.invitations) {
        const item = document.createElement("li");
        const valid = pending.expires * 1000 > Date.now();
        item.append(`${pending.email} · ${roleLabels[pending.role]} · ${valid ? "Expires " : "Expired "}${date.format(pending.expires * 1000)} `, button("Replace link", () => void task(() => createInvitation({ email: pending.email, role: pending.role }), "New invitation link created.")), button("Cancel invitation", () => void task(async () => {
            await call(`invitations/${pending.id}`, "DELETE");
            clearInvitation();
            await loadMembers();
        }, "Invitation canceled.")));
        invitations.append(item);
    }
    for (const member of roster.members) {
        const item = document.createElement("li");
        item.append(`${member.name} · ${member.email} · ${roleLabels[member.role]} `);
        if (member.removed)
            item.append("· Access removed");
        else {
            const role = document.createElement("select");
            role.setAttribute("aria-label", `Role for ${member.name}`);
            for (const [value, label] of Object.entries(roleLabels))
                role.add(new Option(label, value));
            role.value = member.role;
            item.append(role, button("Save role", () => void task(async () => {
                await call(`members/${member.id}`, "PUT", { role: role.value });
                await loadMembers();
            }, "Member role saved.")), button("Manage media access", () => void task(async () => {
                selected = member;
                await loadShares();
            })), button("Remove access", () => void task(async () => {
                await call(`members/${member.id}`, "DELETE");
                await loadMembers();
            }, "Team access removed.")));
        }
        members.append(item);
    }
    if (!roster.members.some(member => member.id === selected?.id && !member.removed)) {
        selected = undefined;
        sharing.hidden = true;
    }
    if (activity.open)
        await loadActivity();
    management.hidden = false;
    access.hidden = true;
}
async function createInvitation(body) {
    const { path } = await (await call("invitations", "POST", body)).json();
    await loadMembers();
    invitationLink.value = new URL(path, location.origin).href;
    invitationBox.hidden = false;
}
invite.onsubmit = event => {
    event.preventDefault();
    void task(async () => {
        await createInvitation(Object.fromEntries(new FormData(invite)));
        invite.reset();
    }, "Invitation link created.");
};
invitationBox.querySelector("button").onclick = () => void task(async () => {
    invitationLink.select();
    await navigator.clipboard.writeText(invitationLink.value);
}, "Invitation link copied.");
activity.ontoggle = () => { if (activity.open)
    void task(loadActivity); };
access.onsubmit = event => {
    event.preventDefault();
    void task(async () => {
        await loadMembers();
        sessionStorage.setItem("cueporchOperator", credential.value);
    });
};
credential.value = sessionStorage.getItem("cueporchOperator") || "";
if (credential.value)
    void task(loadMembers);
