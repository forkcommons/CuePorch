import {checked, type Member, type Role, roleLabels, teamRequest} from "./team_api.js";

interface Invitation {id: string; email: string; role: Role; expires: number}
interface Share {id: string; title: string; shared: boolean}
interface Roster {members: (Member & {removed: number | null})[]; invitations: Invitation[]}
interface Audit {actor: string; action: string; subject: string; detail: string | null; at: number}

const access = document.querySelector<HTMLFormElement>("#teamManagementAccess")!;
const credential = access.querySelector<HTMLInputElement>("input")!;
const management = document.querySelector<HTMLElement>("#teamManagement")!;
const status = document.querySelector<HTMLElement>("#teamMembersStatus")!;
const invite = document.querySelector<HTMLFormElement>("#teamInvite")!;
const invitationBox = document.querySelector<HTMLElement>("#teamInvitationLink")!;
const invitationLink = invitationBox.querySelector<HTMLInputElement>("input")!;
const invitations = document.querySelector<HTMLUListElement>("#teamInvitations")!;
const members = document.querySelector<HTMLUListElement>("#teamMemberList")!;
const sharing = document.querySelector<HTMLElement>("#teamShares")!;
const shareWork = document.querySelector<HTMLSelectElement>("#teamShareWork")!;
const shareButton = document.querySelector<HTMLButtonElement>("#shareTeamVideo")!;
const shared = document.querySelector<HTMLUListElement>("#teamSharedVideos")!;
const activity = document.querySelector<HTMLDetailsElement>("#teamMembershipActivity")!;
const date = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"});
let selected: Member | undefined;
let working = false;
let names: Map<string, string>;

function call(path: string, method = "GET", body = {}) {
  return teamRequest(path, method, body, credential.value).then(checked);
}

async function task(action: () => Promise<void>, success = "") {
  if (working) return;
  working = true;
  status.textContent = "";
  try {
    await action();
    status.textContent = success;
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    working = false;
  }
}

function button(text: string, action: () => void) {
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
  if (!selected) return;
  const member = selected;
  const {works}: {works: Share[]} = await (await call(`members/${member.id}/works`)).json();
  sharing.querySelector("h3")!.textContent = `Videos shared with ${member.name}`;
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
    } else shareWork.add(new Option(work.title, work.id));
  }
  shareButton.disabled = !shareWork.options.length;
}

shareButton.onclick = () => void task(async () => {
  if (!selected || !shareWork.value) return;
  await call(`members/${selected.id}/works/${shareWork.value}`, "PUT");
  await loadShares();
}, "Video shared.");

async function loadActivity() {
  const {events}: {events: Audit[]} = await (await call("audit")).json();
  const list = activity.querySelector("ul")!;
  list.replaceChildren();
  for (const event of events) {
    const item = document.createElement("li");
    const action = event.action.replaceAll("_", " ");
    const actor = names.get(event.actor.replace(/^team:/, "")) || event.actor;
    const subject = names.get(event.subject) || event.subject;
    const detail = event.detail && !event.action.startsWith("video_") ? ` (${event.detail})` : "";
    item.textContent = `${date.format(event.at * 1000)} · ${actor} · ${action} · ${subject}${detail}`;
    list.append(item);
  }
}

async function loadMembers() {
  const roster: Roster = await (await call("members")).json();
  names = new Map([...roster.members.map(member => [member.id, member.name] as const),
    ...roster.invitations.map(pending => [pending.id, pending.email] as const)]);
  invitations.replaceChildren();
  members.replaceChildren();
  for (const pending of roster.invitations) {
    const item = document.createElement("li");
    const valid = pending.expires * 1000 > Date.now();
    item.append(`${pending.email} · ${roleLabels[pending.role]} · ${valid ? "Expires " : "Expired "}${date.format(pending.expires * 1000)} `,
      button("Replace link", () => void task(() => createInvitation({email: pending.email, role: pending.role}), "New invitation link created.")),
      button("Cancel invitation", () => void task(async () => {
        await call(`invitations/${pending.id}`, "DELETE");
        clearInvitation();
        await loadMembers();
      }, "Invitation canceled.")));
    invitations.append(item);
  }
  for (const member of roster.members) {
    const item = document.createElement("li");
    item.append(`${member.name} · ${member.email} · ${roleLabels[member.role]} `);
    if (member.removed) item.append("· Access removed");
    else {
      const role = document.createElement("select");
      role.setAttribute("aria-label", `Role for ${member.name}`);
      for (const [value, label] of Object.entries(roleLabels)) role.add(new Option(label, value));
      role.value = member.role;
      item.append(role, button("Save role", () => void task(async () => {
        await call(`members/${member.id}`, "PUT", {role: role.value});
        await loadMembers();
      }, "Member role saved.")), button("Manage video access", () => void task(async () => {
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
  if (activity.open) await loadActivity();
  management.hidden = false;
  access.hidden = true;
}

async function createInvitation(body: object) {
  const {path}: {path: string} = await (await call("invitations", "POST", body)).json();
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

invitationBox.querySelector("button")!.onclick = () => void task(async () => {
  invitationLink.select();
  await navigator.clipboard.writeText(invitationLink.value);
}, "Invitation link copied.");

activity.ontoggle = () => { if (activity.open) void task(loadActivity); };

access.onsubmit = event => {
  event.preventDefault();
  void task(async () => {
    await loadMembers();
    sessionStorage.setItem("cueporchOperator", credential.value);
  });
};
credential.value = sessionStorage.getItem("cueporchOperator") || "";
if (credential.value) void task(loadMembers);
