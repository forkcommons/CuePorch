import {viewerRequest} from "./audience_api.js";

const status = document.querySelector<HTMLElement>("#groupStatus")!;
const groupSlug = document.body.dataset.groupSlug ?? "";
let busy = false;

function accountUrl() {
  return "/account?next=" + encodeURIComponent(location.pathname);
}

async function errorStatus(response: Response) {
  const doc = new DOMParser().parseFromString(await response.text(), "text/html");
  const paragraph = doc.querySelector("p");
  status.textContent = paragraph?.textContent?.trim() || "Could not update this group. Try again.";
}

async function send(method: "POST" | "PUT" | "DELETE", path: string, body?: object) {
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

async function update(run: () => Promise<void>) {
  if (busy) return;
  busy = true;
  status.textContent = "";
  try { await run(); }
  catch { if (!status.textContent) status.textContent = "Could not update this group. Try again."; }
  finally { busy = false; }
}

function videoPath(value: string) {
  return "groups/" + encodeURIComponent(groupSlug) + "/videos/" +
    value.split("/").map(encodeURIComponent).join("/");
}

const settings = document.querySelector<HTMLFormElement>("#groupSettings");
if (settings) settings.onsubmit = event => {
  event.preventDefault();
  const editing = settings.dataset.purpose === "edit";
  const slug = settings.dataset.slug ?? groupSlug;
  const formData = new FormData(settings);
  const body = {
    ...Object.fromEntries(formData),
    ...(editing ? {allow_members: formData.has("allow_members"), require_review: formData.has("require_review")} : {}),
  };
  void update(async () => {
    const response = await send(editing ? "PUT" : "POST", editing ? "groups/" + encodeURIComponent(slug) : "groups", body);
    location.assign((await response.json()).url);
  });
};

const deleteForm = document.querySelector<HTMLFormElement>("#groupDelete");
if (deleteForm) deleteForm.onsubmit = event => {
  event.preventDefault();
  const body = Object.fromEntries(new FormData(deleteForm));
  void update(async () => {
    await send("DELETE", "groups/" + encodeURIComponent(groupSlug), body);
    location.assign("/watch/groups");
  });
};

const membership = document.querySelector<HTMLElement>("#groupMembership");
if (membership) {
  const toggle = () => {
    const join = membership.dataset.membership === "join";
    void update(async () => {
      await send(join ? "PUT" : "DELETE", "groups/" + encodeURIComponent(groupSlug) + "/membership");
      location.reload();
    });
  };
  if (membership instanceof HTMLFormElement) membership.onsubmit = event => { event.preventDefault(); toggle(); };
  else membership.onclick = toggle;
}

const videoForm = document.querySelector<HTMLFormElement>("#groupVideoForm");
if (videoForm) videoForm.onsubmit = event => {
  event.preventDefault();
  const select = videoForm.querySelector<HTMLSelectElement>("select")!;
  void update(async () => { await send("PUT", videoPath(select.value)); location.reload(); });
};

for (const button of document.querySelectorAll<HTMLButtonElement>("[data-remove-group-video]")) {
  button.onclick = () => {
    void update(async () => { await send("DELETE", videoPath(button.dataset.removeGroupVideo ?? "")); location.reload(); });
  };
}

for (const button of document.querySelectorAll<HTMLButtonElement>("[data-approve-submission], [data-reject-submission]")) {
  button.onclick = () => {
    const value = (button.dataset.approveSubmission ?? button.dataset.rejectSubmission)!;
    const approve = button.dataset.approveSubmission !== undefined;
    const key = value.split("/").map(encodeURIComponent).join("/");
    void update(async () => {
      await send("POST", "groups/" + encodeURIComponent(groupSlug) + "/submissions/" + key, {approve});
      location.reload();
    });
  };
}

for (const button of document.querySelectorAll<HTMLButtonElement>("[data-withdraw-submission]")) {
  button.onclick = () => {
    void update(async () => {
      await send("DELETE", videoPath(button.dataset.withdrawSubmission ?? ""));
      location.reload();
    });
  };
}

const inviteForm = document.querySelector<HTMLFormElement>("#groupInvite");
if (inviteForm) {
  const groupPath = "groups/" + encodeURIComponent(groupSlug);
  const link = document.querySelector<HTMLInputElement>("#groupInviteLink")!;
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
      const result = await response.json() as {url: string; delivery: "accepted" | "failed" | null};
      const destination = new URL(result.url, location.origin);
      destination.searchParams.set("invitation", result.delivery || "saved");
      location.assign(destination);
    });
  };
  const ownerForm = document.querySelector<HTMLFormElement>("#groupOwner");
  if (ownerForm) ownerForm.onsubmit = event => {
    event.preventDefault();
    const fields = Object.fromEntries(new FormData(ownerForm));
    void update(async () => {
      const response = await send("POST", groupPath + "/ownership", fields);
      location.assign((await response.json()).url);
    });
  };
  const revision = () => new FormData(inviteForm).get("revision");
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-group-moderator]")) {
    button.onclick = () => {
      const member = encodeURIComponent(button.dataset.groupModerator!);
      const enabled = button.dataset.enabled === "true";
      void update(async () => {
        await send("PUT", groupPath + "/moderators/" + member, {enabled, revision: revision()});
        location.reload();
      });
    };
  }
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-revoke-invite]")) {
    button.onclick = () => {
      void update(async () => {
        await send("DELETE", groupPath + "/invite", {email: button.dataset.revokeInvite, revision: revision()});
        location.reload();
      });
    };
  }
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-remove-group-member]")) {
    button.onclick = () => {
      const member = encodeURIComponent(button.dataset.removeGroupMember!);
      void update(async () => {
        await send("DELETE", groupPath + "/members/" + member, {revision: revision()});
        location.reload();
      });
    };
  }
}
