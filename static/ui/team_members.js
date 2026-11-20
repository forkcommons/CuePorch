import { checked, roleLabels, teamRequest } from "./team_api.js";
const access = document.querySelector("#teamManagementAccess");
const credential = access.querySelector("input");
const management = document.querySelector("#teamManagement");
const status = document.querySelector("#teamMembersStatus");
const invite = document.querySelector("#teamInvite");
const emailInvite = invite.querySelector("[name=send_email]");
const invitationBox = document.querySelector("#teamInvitationLink");
const invitationLink = invitationBox.querySelector("input");
const invitations = document.querySelector("#teamInvitations");
const members = document.querySelector("#teamMemberList");
const sharing = document.querySelector("#teamShares");
const shareWork = document.querySelector("#teamShareWork");
const workPermission = document.querySelector("#teamWorkPermission");
const shareButton = document.querySelector("#shareTeamVideo");
const shared = document.querySelector("#teamSharedVideos");
const folderTitle = document.querySelector("#teamFoldersTitle");
const shareFolder = document.querySelector("#teamShareFolder");
const folderPermission = document.querySelector("#teamFolderPermission");
const shareFolderButton = document.querySelector("#shareTeamFolder");
const sharedFolders = document.querySelector("#teamSharedFolders");
const activity = document.querySelector("#teamMembershipActivity");
const groupSelector = document.querySelector("#teamGroup");
const groupForm = document.querySelector("#teamGroupForm");
const groupName = groupForm.querySelector("[name=name]");
const groupRole = groupForm.querySelector("[name=default_role]");
const groupMembers = document.querySelector("#teamGroupMembers");
const groupFolders = document.querySelector("#teamGroupFolders");
const deleteGroup = document.querySelector("#removeTeamGroup");
const date = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
let selected;
let working = false;
let names;
let groupOptions;
function call(path, method = "GET", body = {}) {
    return teamRequest(path, method, body, credential.value).then(checked);
}
async function task(action, success = "") {
    if (working)
        return;
    working = true;
    status.textContent = "";
    try {
        status.textContent = await action() ?? success;
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
function permissionSelect(value, group = false) {
    const select = document.createElement("select");
    if (group)
        select.add(new Option("No access", ""));
    select.add(new Option("Can view", "view"));
    select.add(new Option("Can edit", "edit"));
    select.value = value;
    return select;
}
function shareRow(share, path, isFolder = false) {
    const row = document.createElement("li");
    const select = permissionSelect(share.permission ?? "view");
    select.setAttribute("aria-label", `Permission for ${share.title}`);
    row.append(`${share.title} `, select, button("Save permission", () => void task(async () => {
        await call(path, "PUT", { permission: select.value });
        await loadShares();
    }, "Permission saved.")), button(`Revoke ${isFolder ? "folder " : ""}${share.title}`, () => void task(async () => {
        await call(path, "DELETE");
        await loadShares();
    }, isFolder ? "Folder access revoked." : "Video access revoked.")));
    return row;
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
            shared.append(shareRow(work, `members/${member.id}/works/${work.id}`));
        }
        else
            shareWork.add(new Option(work.title, work.id));
    }
    shareButton.disabled = !shareWork.options.length;
    shareFolder.replaceChildren();
    sharedFolders.replaceChildren();
    for (const folder of folders) {
        if (folder.shared) {
            sharedFolders.append(shareRow(folder, `members/${member.id}/folders/${folder.id}`, true));
        }
        else
            shareFolder.add(new Option(folder.title, folder.id));
    }
    shareFolderButton.disabled = !shareFolder.options.length;
}
shareButton.onclick = () => void task(async () => {
    if (!selected || !shareWork.value)
        return;
    await call(`members/${selected.id}/works/${shareWork.value}`, "PUT", { permission: workPermission.value });
    workPermission.value = "view";
    await loadShares();
}, "Video shared.");
shareFolderButton.onclick = () => void task(async () => {
    if (!selected || !shareFolder.value)
        return;
    await call(`members/${selected.id}/folders/${shareFolder.value}`, "PUT", { permission: folderPermission.value });
    folderPermission.value = "view";
    await loadShares();
}, "Folder shared.");
function checkbox(name, value, label, selected, disabled = false) {
    const row = document.createElement("div");
    const caption = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = name;
    input.value = value;
    input.checked = selected;
    input.disabled = disabled;
    caption.append(input, " ", label);
    row.append(caption);
    return row;
}
function selectedGroup() {
    return groupOptions.groups.find(group => group.id === groupSelector.value);
}
function showGroup() {
    const group = selectedGroup();
    groupName.value = group?.name ?? "";
    groupRole.value = group?.default_role ?? "";
    groupName.readOnly = Boolean(group?.managed);
    deleteGroup.disabled = !group || Boolean(group.managed);
    document.querySelector("#teamGroupManagement").hidden = !group?.managed;
    groupMembers.replaceChildren(...groupOptions.members.map(member => checkbox("members", member.id, `${member.name} · ${member.email}`, group?.members.includes(member.id) ?? false, group?.managed)));
    groupFolders.replaceChildren(...[...groupOptions.folders, ...(group?.unavailable_folders ?? [])].map(folder => {
        const value = group?.edit_folders.includes(folder.id) ? "edit" : group?.folders.includes(folder.id) ? "view" : "";
        const select = permissionSelect(value, true);
        select.name = folder.id;
        select.setAttribute("aria-label", folder.title);
        const label = document.createElement("label");
        label.append(folder.title, " ", select);
        return label;
    }));
}
async function loadGroups(id = groupSelector.value) {
    groupOptions = await (await call("groups")).json();
    groupSelector.replaceChildren(new Option("New group", ""), ...groupOptions.groups.map(group => new Option(group.name, group.id)));
    if (groupOptions.groups.some(group => group.id === id))
        groupSelector.value = id;
    showGroup();
}
groupSelector.onchange = showGroup;
document.querySelector("#reloadTeamGroups").onclick = () => void task(() => loadGroups());
groupForm.onsubmit = event => {
    event.preventDefault();
    void task(async () => {
        const group = selectedGroup();
        const form = new FormData(groupForm);
        const folders = Array.from(groupFolders.querySelectorAll("select"));
        const body = {
            name: groupName.value, members: form.getAll("members"),
            folders: folders.filter(folder => folder.value).map(folder => folder.name),
            edit_folders: folders.filter(folder => folder.value === "edit").map(folder => folder.name),
            revision: group?.revision,
            default_role: groupRole.value || null,
        };
        const { id } = await (await call(group ? `groups/${group.id}` : "groups", group ? "PUT" : "POST", body)).json();
        await loadGroups(id);
        if (activity.open)
            await loadActivity();
    }, "Group saved.");
};
deleteGroup.onclick = () => void task(async () => {
    const group = selectedGroup();
    if (!group)
        return;
    await call(`groups/${group.id}`, "DELETE", { revision: group.revision });
    await loadGroups("");
    if (activity.open)
        await loadActivity();
}, "Group deleted.");
async function loadActivity() {
    const { events } = await (await call("audit")).json();
    const list = activity.querySelector("ul");
    list.replaceChildren();
    for (const event of events) {
        const item = document.createElement("li");
        const action = event.action.replaceAll("_", " ");
        const actor = names.get(event.actor.replace(/^team:/, "")) || event.actor;
        const subject = event.action.startsWith("group_") ? event.detail || event.subject :
            names.get(event.subject) || event.subject;
        const detail = event.detail && !/^(video|folder|group)_/.test(event.action) ? ` (${event.detail})` : "";
        item.textContent = `${date.format(event.at * 1000)} · ${actor} · ${action} · ${subject}${detail}`;
        list.append(item);
    }
}
async function loadMembers() {
    const roster = await (await call("members")).json();
    document.querySelector("#teamOwnershipHelp").hidden = !roster.ownership.can_transfer;
    names = new Map([...roster.members.map(member => [member.id, member.name]),
        ...roster.invitations.map(pending => [pending.id, pending.email])]);
    invitations.replaceChildren();
    members.replaceChildren();
    for (const pending of roster.invitations) {
        const item = document.createElement("li");
        const valid = pending.expires * 1000 > Date.now();
        item.append(`${pending.email} · ${roleLabels[pending.role]} · ${valid ? "Expires " : "Expired "}${date.format(pending.expires * 1000)} `, button("Replace link", () => void task(async () => {
            await createInvitation({ email: pending.email, role: pending.role });
        }, "New invitation link created.")), button("Cancel invitation", () => void task(async () => {
            await call(`invitations/${pending.id}`, "DELETE");
            clearInvitation();
            await loadMembers();
        }, "Invitation canceled.")));
        if (emailInvite)
            item.append(button("Email replacement link", () => void task(() => createInvitation({ email: pending.email, role: pending.role, send_email: true }))));
        invitations.append(item);
    }
    for (const member of roster.members) {
        const item = document.createElement("li");
        item.append(`${member.name} · ${member.email} · ${roleLabels[member.effective_role ?? member.role]} `);
        if (member.removed)
            item.append("· Access removed");
        else if (member.role !== "owner") {
            const role = document.createElement("select");
            role.setAttribute("aria-label", `Role for ${member.name}`);
            for (const [value, label] of Object.entries(roleLabels))
                if (value !== "owner")
                    role.add(new Option(label, value));
            role.value = member.role;
            const override = document.createElement("input");
            override.type = "checkbox";
            override.checked = Boolean(member.role_override);
            const overrideLabel = document.createElement("label");
            overrideLabel.append(override, " Use this member's direct role");
            role.onchange = () => { override.checked = true; };
            item.append(role, overrideLabel, button("Save role", () => void task(async () => {
                await call(`members/${member.id}`, "PUT", { role: role.value, role_override: override.checked });
                await loadMembers();
            }, "Member role saved.")), button("Manage media access", () => void task(async () => {
                selected = member;
                await loadShares();
            })), button("Remove access", () => void task(async () => {
                await call(`members/${member.id}`, "DELETE");
                await loadMembers();
            }, "Team access removed.")));
            if (roster.ownership.can_transfer)
                item.append(button(`Make ${member.name} owner`, () => void task(async () => {
                    await call(`owner/${member.id}`, "PUT", { previous: roster.ownership.owner });
                    await loadMembers();
                }, "Ownership updated.")));
        }
        members.append(item);
    }
    if (!roster.members.some(member => member.id === selected?.id && !member.removed)) {
        selected = undefined;
        sharing.hidden = true;
    }
    await loadGroups();
    if (activity.open)
        await loadActivity();
    management.hidden = false;
    access.hidden = true;
}
document.querySelector("#downloadTeamRoster").onclick = () => void task(async () => {
    const response = await call("roster.csv");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = "cueporch-team-roster.csv";
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    if (activity.open)
        await loadActivity();
}, "Roster downloaded.");
async function createInvitation(body) {
    const { path, url, delivery } = await (await call("invitations", "POST", body)).json();
    await loadMembers();
    invitationLink.value = new URL(url ?? path, location.origin).href;
    invitationBox.hidden = false;
    if (delivery === "accepted")
        return "The mail relay accepted the invitation.";
    if (delivery === "failed")
        return "Email delivery failed. Copy the private link to share it.";
    return "Invitation link created.";
}
invite.onsubmit = event => {
    event.preventDefault();
    void task(async () => {
        const form = Object.fromEntries(new FormData(invite));
        const outcome = await createInvitation({ ...form, send_email: emailInvite?.checked ?? false });
        invite.reset();
        return outcome;
    });
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
