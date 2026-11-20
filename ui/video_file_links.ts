import {checked} from "./team_api.js";

interface FileLinks {
  version: string;
  state: "available" | "queued" | "rendering" | "ready" | "failed" | "unavailable";
  id?: string;
  issue?: string;
  hls?: string;
  dash?: string;
  files?: {width: number; height: number; bytes: number; play: string; download: string | null}[];
}

const root = document.querySelector<HTMLElement>("main")!;
const access = document.querySelector<HTMLFormElement>("#fileLinkAccess")!;
const key = access.querySelector<HTMLInputElement>("input")!;
const status = document.querySelector<HTMLElement>("#fileLinkStatus")!;
const panel = document.querySelector<HTMLElement>("#fileLinkPanel")!;
const prepare = document.querySelector<HTMLButtonElement>("#prepareFileLinks")!;
const revoke = document.querySelector<HTMLButtonElement>("#revokeFileLinks")!;
const list = document.querySelector<HTMLUListElement>("#fileLinkList")!;
let latest: FileLinks;
let timer: number | undefined;
let working = false;

async function call(method = "GET", body?: object) {
  return checked(await fetch(`/api/library/work/${root.dataset.work}/file-links`, {
    method, headers: {Authorization: `Bearer ${key.value}`, "Content-Type": "application/json"},
    body: body ? JSON.stringify(body) : undefined,
  }));
}

function showLink(label: string, path: string) {
  const row = document.createElement("li");
  const link = document.createElement("a");
  const copy = document.createElement("button");
  link.href = new URL(path, location.origin).href;
  link.textContent = label;
  copy.type = "button";
  copy.textContent = "Copy link";
  copy.setAttribute("aria-label", `Copy ${label} link`);
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(link.href);
      status.textContent = "Link copied.";
    } catch {
      status.textContent = "Use the link's context menu to copy its address.";
    }
  };
  row.append(link, " ", copy);
  list.append(row);
}

async function refresh() {
  latest = await (await call()).json();
  const pending = latest.state === "queued" || latest.state === "rendering";
  panel.hidden = false;
  access.hidden = true;
  prepare.hidden = latest.state !== "available" && latest.state !== "failed";
  prepare.textContent = latest.state === "failed" ? "Retry file links" : "Prepare file links";
  revoke.hidden = !latest.id;
  list.replaceChildren();
  status.textContent = latest.issue || (pending ? "Preparing video renditions…" :
    latest.state === "ready" ? "Video file links ready." : "");
  if (latest.hls) showLink("HLS", latest.hls);
  if (latest.dash) showLink("DASH", latest.dash);
  for (const file of latest.files || []) {
    const size = `${file.width} × ${file.height}`;
    showLink(`Play MP4 ${size}`, file.play);
    if (file.download) showLink(`Download MP4 ${size}`, file.download);
  }
  window.clearTimeout(timer);
  if (pending) timer = window.setTimeout(() => void task(refresh), 750);
}

async function task(action: () => Promise<void>) {
  if (working) return;
  working = true;
  try {
    await action();
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    working = false;
  }
}

access.onsubmit = event => {
  event.preventDefault();
  void task(async () => {
    await refresh();
    sessionStorage.setItem("cueporchOperator", key.value);
  });
};
prepare.onclick = () => void task(async () => {
  await call("POST", {version: latest.version});
  await refresh();
});
revoke.onclick = () => void task(async () => {
  await call("DELETE", {id: latest.id});
  await refresh();
  status.textContent = "Video file links revoked.";
});
key.value = sessionStorage.getItem("cueporchOperator") || "";
if (key.value) void task(refresh);
