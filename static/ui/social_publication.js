const provider = document.body.dataset.provider;
const providerName = document.body.dataset.providerName;
const endpoint = `/api/library/work/${document.body.dataset.work}/social/${provider}`;
const access = document.querySelector("#socialAccess");
const operator = access.querySelector("input");
const notice = document.querySelector("#socialState");
const connect = document.querySelector("#socialConnect");
const form = document.querySelector("#socialForm");
const title = form.querySelector("[name=title]");
const companyLabel = form.querySelector("#socialCompany");
const pageSelector = form.querySelector("#socialPage");
const submit = form.querySelector("button");
const historySection = document.querySelector("#socialHistory");
const jobs = document.querySelector("#socialJobs");
let credential = sessionStorage.getItem("cueporchOperator") || "";
let draft = crypto.randomUUID();
let cut = "";
let company = null;
let watch = 0;
async function api(body, path = endpoint) {
    const response = await fetch(path, {
        method: body ? "POST" : "GET",
        headers: { Authorization: `Bearer ${credential}`, "Content-Type": "application/json", "X-CuePorch-Social": "1" },
        body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
        const html = new DOMParser().parseFromString(await response.text(), "text/html");
        throw new Error(html.querySelector("p")?.textContent || `Request failed (${response.status}).`);
    }
    return response.json();
}
function render(data, fresh = false) {
    access.hidden = true;
    connect.hidden = data.connected || !data.allowed;
    form.hidden = !data.connected || !data.configured || !data.allowed;
    historySection.hidden = data.jobs.length === 0;
    if (fresh) {
        cut = data.cut;
        company = data.company;
        if (companyLabel) {
            companyLabel.value = company || "";
        }
        if (pageSelector) {
            pageSelector.replaceChildren(new Option("Choose a Page", ""), ...data.pages.map(page => new Option(page.name, page.id)));
        }
        title.value = data.title;
        if (!data.allowed)
            notice.textContent = "Publishing to this destination is restricted by your workspace.";
        else if (!data.configured)
            notice.textContent = `Configure ${providerName} for this workspace in Integrations.`;
        else
            notice.textContent = data.connected ? `Choose how this copy should appear on ${providerName}.` :
                `Connect ${providerName} to publish.`;
    }
    jobs.replaceChildren(...data.jobs.map(job => {
        const item = document.createElement("li");
        item.textContent = `${job.state} · ${job.privacy || job.requested_privacy} · ${job.sent.toLocaleString()} of ${job.bytes.toLocaleString()} bytes`;
        if (job.company) {
            const page = data.pages.find(candidate => candidate.id === job.company);
            item.append(` · ${page?.name && page.name !== job.company ? page.name : "Page " + job.company}`);
        }
        if (job.error)
            item.append(`. ${job.error}`);
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
                }
                catch (error) {
                    try {
                        render(await api());
                    }
                    catch {
                        cancel.disabled = false;
                    }
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
            try {
                render(await api());
            }
            catch (error) {
                notice.textContent = error instanceof Error ? error.message : String(error);
            }
        }, 1500);
    }
}
async function open() {
    try {
        render(await api(), true);
        sessionStorage.setItem("cueporchOperator", credential);
    }
    catch (error) {
        access.hidden = false;
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
            ? { visibility: fields.get("visibility"), made_for_kids: fields.has("madeForKids") }
            : { company: pageSelector ? fields.get("company") : company, visibility: fields.has("publicPost") ? "public" : null };
        render(await api({ id: draft, cut, title: fields.get("title"), description: fields.get("description"), ...options }));
        draft = crypto.randomUUID();
        notice.textContent = "Upload requested. Follow its progress below.";
    }
    catch (error) {
        notice.textContent = error instanceof Error ? error.message : String(error);
    }
    finally {
        submit.disabled = false;
    }
};
if (credential)
    void open();
else
    access.hidden = false;
export {};
