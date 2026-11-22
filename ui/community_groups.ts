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
  const body = Object.fromEntries(new FormData(settings));
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
