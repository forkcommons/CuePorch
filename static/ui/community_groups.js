import { viewerRequest } from "./audience_api.js";
const status = document.querySelector("#groupStatus");
const groupSlug = document.body.dataset.groupSlug ?? "";
let busy = false;
function accountUrl() {
    return "/account?next=" + encodeURIComponent(location.pathname);
}
async function errorStatus(response) {
    const doc = new DOMParser().parseFromString(await response.text(), "text/html");
    const paragraph = doc.querySelector("p");
    status.textContent = paragraph?.textContent?.trim() || "Could not update this group. Try again.";
}
async function send(method, path, body) {
    const response = await viewerRequest(path, method, body);
    if (response.status === 401) {
        location.assign(accountUrl());
        throw new Error("Signed out.");
    }
    if (!response.ok) {
        await errorStatus(response);
        throw new Error("Request failed.");
    }
    return response;
}
async function update(run) {
    if (busy)
        return;
    busy = true;
    status.textContent = "";
    try {
        await run();
    }
    catch {
        if (!status.textContent)
            status.textContent = "Could not update this group. Try again.";
    }
    finally {
        busy = false;
    }
}
function videoPath(value) {
    return "groups/" + encodeURIComponent(groupSlug) + "/videos/" +
        value.split("/").map(encodeURIComponent).join("/");
}
const settings = document.querySelector("#groupSettings");
if (settings)
    settings.onsubmit = event => {
        event.preventDefault();
        const editing = settings.dataset.purpose === "edit";
        const slug = settings.dataset.slug ?? groupSlug;
        const formData = new FormData(settings);
        const body = {
            ...Object.fromEntries(formData),
            ...(editing ? { allow_members: formData.has("allow_members"), require_review: formData.has("require_review") } : {}),
        };
        void update(async () => {
            const response = await send(editing ? "PUT" : "POST", editing ? "groups/" + encodeURIComponent(slug) : "groups", body);
            location.assign((await response.json()).url);
        });
    };
const deleteForm = document.querySelector("#groupDelete");
if (deleteForm)
    deleteForm.onsubmit = event => {
        event.preventDefault();
        const body = Object.fromEntries(new FormData(deleteForm));
        void update(async () => {
            await send("DELETE", "groups/" + encodeURIComponent(groupSlug), body);
            location.assign("/watch/groups");
        });
    };
const membership = document.querySelector("#groupMembership");
if (membership) {
    const toggle = () => {
        const join = membership.dataset.membership === "join";
        void update(async () => {
            await send(join ? "PUT" : "DELETE", "groups/" + encodeURIComponent(groupSlug) + "/membership");
            location.reload();
        });
    };
    if (membership instanceof HTMLFormElement)
        membership.onsubmit = event => { event.preventDefault(); toggle(); };
    else
        membership.onclick = toggle;
}
const videoForm = document.querySelector("#groupVideoForm");
if (videoForm)
    videoForm.onsubmit = event => {
        event.preventDefault();
        const select = videoForm.querySelector("select");
        void update(async () => { await send("PUT", videoPath(select.value)); location.reload(); });
    };
for (const button of document.querySelectorAll("[data-remove-group-video]")) {
    button.onclick = () => {
        void update(async () => { await send("DELETE", videoPath(button.dataset.removeGroupVideo ?? "")); location.reload(); });
    };
}
for (const button of document.querySelectorAll("[data-approve-submission], [data-reject-submission]")) {
    button.onclick = () => {
        const value = (button.dataset.approveSubmission ?? button.dataset.rejectSubmission);
        const approve = button.dataset.approveSubmission !== undefined;
        const key = value.split("/").map(encodeURIComponent).join("/");
        void update(async () => {
            await send("POST", "groups/" + encodeURIComponent(groupSlug) + "/submissions/" + key, { approve });
            location.reload();
        });
    };
}
for (const button of document.querySelectorAll("[data-withdraw-submission]")) {
    button.onclick = () => {
        void update(async () => {
            await send("DELETE", videoPath(button.dataset.withdrawSubmission ?? ""));
            location.reload();
        });
    };
}
for (const form of document.querySelectorAll("[data-album-form]")) {
    const list = form.querySelector("[data-album-items]");
    const choices = form.querySelector("[data-album-choices]");
    const template = form.querySelector("[data-album-item-template]");
    const id = form.dataset.albumId;
    const path = "groups/" + encodeURIComponent(groupSlug) + "/albums" + (id ? "/" + encodeURIComponent(id) : "");
    const items = () => Array.from(list.querySelectorAll("[data-album-video]"));
    form.addEventListener("click", event => {
        const button = event.target.closest("button");
        if (!button)
            return;
        const item = button.closest("[data-album-video]");
        if (button.matches("[data-album-add]")) {
            const choice = choices.selectedOptions[0];
            if (!choice || items().some(row => row.dataset.albumVideo === choice.value))
                return;
            const next = template.content.firstElementChild.cloneNode(true);
            next.dataset.albumVideo = choice.value;
            next.querySelector("[data-album-item-title]").textContent = choice.textContent;
            list.append(next);
        }
        else if (button.dataset.albumMove === "up" && item?.previousElementSibling) {
            item.previousElementSibling.before(item);
            button.focus();
        }
        else if (button.dataset.albumMove === "down" && item?.nextElementSibling) {
            item.nextElementSibling.after(item);
            button.focus();
        }
        else if (button.matches("[data-album-remove]")) {
            item?.remove();
            choices.focus();
        }
        else if (button.matches("[data-album-delete]")) {
            void update(async () => {
                await send("DELETE", path, { revision: new FormData(form).get("revision") });
                location.reload();
            });
        }
    });
    form.onsubmit = event => {
        event.preventDefault();
        const fields = Object.fromEntries(new FormData(form));
        void update(async () => {
            await send(id ? "PUT" : "POST", path, {
                ...fields, items: items().map(item => item.dataset.albumVideo.split("/")),
            });
            location.reload();
        });
    };
}
const inviteForm = document.querySelector("#groupInvite");
if (inviteForm) {
    const groupPath = "groups/" + encodeURIComponent(groupSlug);
    const link = document.querySelector("#groupInviteLink");
    link.value = new URL(location.pathname, location.origin).href;
    const url = new URL(location.href);
    const inviteStatus = url.searchParams.get("invitation");
    if (inviteStatus) {
        const messages = new Map([
            ["saved", "Invitation saved."],
            ["accepted", "Invitation saved and accepted by the email relay."],
            ["failed", "Invitation saved. Email delivery failed. Share the group link."],
        ]);
        status.textContent = messages.get(inviteStatus) || "";
        url.searchParams.delete("invitation");
        history.replaceState(null, "", url);
    }
    inviteForm.onsubmit = event => {
        event.preventDefault();
        const fields = new FormData(inviteForm);
        void update(async () => {
            const response = await send("POST", groupPath + "/invite", {
                ...Object.fromEntries(fields), send_email: fields.has("send_email"),
            });
            const result = await response.json();
            const destination = new URL(result.url, location.origin);
            destination.searchParams.set("invitation", result.delivery || "saved");
            location.assign(destination);
        });
    };
    const ownerForm = document.querySelector("#groupOwner");
    if (ownerForm)
        ownerForm.onsubmit = event => {
            event.preventDefault();
            const fields = Object.fromEntries(new FormData(ownerForm));
            void update(async () => {
                const response = await send("POST", groupPath + "/ownership", fields);
                location.assign((await response.json()).url);
            });
        };
    const revision = () => new FormData(inviteForm).get("revision");
    for (const button of document.querySelectorAll("[data-group-moderator]")) {
        button.onclick = () => {
            const member = encodeURIComponent(button.dataset.groupModerator);
            const enabled = button.dataset.enabled === "true";
            void update(async () => {
                await send("PUT", groupPath + "/moderators/" + member, { enabled, revision: revision() });
                location.reload();
            });
        };
    }
    for (const button of document.querySelectorAll("[data-revoke-invite]")) {
        button.onclick = () => {
            void update(async () => {
                await send("DELETE", groupPath + "/invite", { email: button.dataset.revokeInvite, revision: revision() });
                location.reload();
            });
        };
    }
    for (const button of document.querySelectorAll("[data-remove-group-member]")) {
        button.onclick = () => {
            const member = encodeURIComponent(button.dataset.removeGroupMember);
            void update(async () => {
                await send("DELETE", groupPath + "/members/" + member, { revision: revision() });
                location.reload();
            });
        };
    }
}
