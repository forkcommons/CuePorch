export {};

interface Upload {
  id: string; state: string; sent: number; bytes: number; requested_privacy: string; cancelable: boolean;
  privacy: string | null; error: string | null; link: string | null;
  company: string | null;
}
interface Publication {
  cut: string; title: string; connected: boolean; configured: boolean; allowed: boolean; jobs: Upload[];
  company: string | null;
  pages: {id: string; name: string}[];
  modes: string[];
  connection_scope: "personal" | "workspace";
  connection_revision: string | null;
}

const provider = document.body.dataset.provider!;
const providerName = document.body.dataset.providerName!;
const team = document.body.dataset.team === "1";
const endpoint = `/api/library/work/${document.body.dataset.work}/social/${provider}`;
const access = document.querySelector<HTMLFormElement>("#socialAccess")!;
const operator = access.querySelector<HTMLInputElement>("input")!;
const notice = document.querySelector<HTMLElement>("#socialState")!;
const connect = document.querySelector<HTMLElement>("#socialConnect")!;
const form = document.querySelector<HTMLFormElement>("#socialForm")!;
const title = form.querySelector<HTMLInputElement>("[name=title]")!;
const companyLabel = form.querySelector<HTMLOutputElement>("#socialCompany");
const connectionScope = form.querySelector<HTMLOutputElement>("#socialConnectionScope");
const pageSelector = form.querySelector<HTMLSelectElement>("#socialPage");
const visibility = form.querySelector<HTMLSelectElement>("[name=visibility]");
const submit = form.querySelector<HTMLButtonElement>("button")!;
const historySection = document.querySelector<HTMLElement>("#socialHistory")!;
const jobs = document.querySelector<HTMLUListElement>("#socialJobs")!;
let credential = team ? "team-session" : sessionStorage.getItem("cueporchOperator") || "";
let draft = crypto.randomUUID();
let cut = "";
let company: string | null = null;
let scope: "personal" | "workspace" = "personal";
let revision: string | null = null;
let watch = 0;

async function api(body?: object, path = endpoint): Promise<Publication> {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: {Authorization: `Bearer ${credential}`, "Content-Type": "application/json", "X-CuePorch-Social": "1",
      ...(team ? {"X-CuePorch-Team": "1"} : {})},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const html = new DOMParser().parseFromString(await response.text(), "text/html");
    throw new Error(html.querySelector("p")?.textContent || `Request failed (${response.status}).`);
  }
  return response.json();
}

function render(data: Publication, fresh = false) {
  access.hidden = true;
  connect.hidden = data.connected || !data.allowed;
  form.hidden = !data.connected || !data.configured || !data.allowed;
  if (visibility) {
    for (const option of visibility.options) {
      option.disabled = !data.modes.includes(option.value);
    }
    if (!data.modes.includes(visibility.value)) visibility.value = data.modes[0] || "";
  }
  historySection.hidden = data.jobs.length === 0;
  if (fresh) {
    cut = data.cut;
    company = data.company;
    scope = data.connection_scope;
    revision = data.connection_revision;
    if (connectionScope) connectionScope.value = scope === "workspace" ? "Workspace account" : "Your connected account";
    if (companyLabel) {
      companyLabel.value = data.pages.find(page => page.id === company)?.name || "";
    }
    if (pageSelector) {
      pageSelector.replaceChildren(new Option("Choose a Page", ""), ...data.pages.map(page => new Option(page.name, page.id)));
    }
    title.value = data.title;
    if (!data.allowed) notice.textContent = "Publishing to this destination is restricted by your workspace.";
    else if (!data.configured) notice.textContent = team ? `${providerName} needs workspace setup.` :
      `Configure ${providerName} for this workspace in Integrations.`;
    else notice.textContent = data.connected ? `Choose how this copy should appear on ${providerName}.` :
      `Connect ${providerName} to publish.`;
  }
  jobs.replaceChildren(...data.jobs.map(job => {
    const item = document.createElement("li");
    item.textContent = `${job.state} · ${job.privacy || job.requested_privacy} · ${job.sent.toLocaleString()} of ${job.bytes.toLocaleString()} bytes`;
    if (job.company) {
      const page = data.pages.find(candidate => candidate.id === job.company);
      item.append(` · ${page?.name && page.name !== job.company ? page.name : "Page " + job.company}`);
    }
    if (job.error) item.append(`. ${job.error}`);
    if (job.link) {
      const link = document.createElement("a");
      link.href = job.link;
      link.rel = "noreferrer";
      link.textContent = `Open on ${providerName}`;
      item.append(" · ", link);
    }
    if (job.cancelable) {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "Cancel upload";
      cancel.onclick = async () => {
        cancel.disabled = true;
        try {
          clearTimeout(watch);
          render(await api({}, `${endpoint}/${encodeURIComponent(job.id)}/cancel`));
          notice.textContent = "Upload canceled.";
        } catch (error) {
          try { render(await api()); } catch { cancel.disabled = false; }
          notice.textContent = error instanceof Error ? error.message : String(error);
        }
      };
      item.append(" ", cancel);
    }
    return item;
  }));
  clearTimeout(watch);
  if (data.jobs.some(job => job.state === "queued" || job.state === "waiting for publication approval")) {
    watch = window.setTimeout(async () => {
      try { render(await api()); }
      catch (error) { notice.textContent = error instanceof Error ? error.message : String(error); }
    }, 1500);
  }
}

async function open() {
  try {
    render(await api(), true);
    if (!team) sessionStorage.setItem("cueporchOperator", credential);
  } catch (error) {
    access.hidden = team;
    notice.textContent = error instanceof Error ? error.message : String(error);
  }
}

access.onsubmit = (event) => {
  event.preventDefault();
  credential = operator.value;
  void open();
};

form.onsubmit = async (event) => {
  event.preventDefault();
  submit.disabled = true;
  const fields = new FormData(form);
  try {
    const options = provider === "youtube"
      ? {visibility: fields.get("visibility"), made_for_kids: fields.has("madeForKids")}
      : {company: pageSelector ? fields.get("company") : company, visibility: fields.has("publicPost") ? "public" : null};
    render(await api({id: draft, cut, title: fields.get("title"), description: fields.get("description"), connection_scope: scope, connection_revision: revision, ...options}));
    draft = crypto.randomUUID();
    notice.textContent = "Upload requested. Follow its progress below.";
  } catch (error) {
    notice.textContent = error instanceof Error ? error.message : String(error);
  } finally {
    submit.disabled = false;
  }
};

if (credential) void open();
else access.hidden = false;
