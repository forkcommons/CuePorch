const provider = document.body.dataset.provider;
const providerName = document.body.dataset.providerName;
const team = document.body.dataset.team === "1";
const endpoint = `/api/library/work/${document.body.dataset.work}/social/${provider}`;
const access = document.querySelector("#socialAccess");
const operator = access.querySelector("input");
const notice = document.querySelector("#socialState");
const connect = document.querySelector("#socialConnect");
const form = document.querySelector("#socialForm");
const title = form.querySelector("[name=title]");
const companyLabel = form.querySelector("#socialCompany");
const connectionScope = form.querySelector("#socialConnectionScope");
const pageSelector = form.querySelector("#socialPage");
const visibility = form.querySelector("[name=visibility]");
const submit = form.querySelector("button:not([type]),button[type=submit]");
const historySection = document.querySelector("#socialHistory");
const jobs = document.querySelector("#socialJobs");
let credential = team ? "team-session" : sessionStorage.getItem("cueporchOperator") || "";
let draft = crypto.randomUUID();
let cut = "";
let company = null;
let scope = "personal";
let revision = null;
let watch = 0;
async function api(body, path = endpoint) {
    const response = await fetch(path, {
        method: body ? "POST" : "GET",
        headers: { Authorization: `Bearer ${credential}`, "Content-Type": "application/json", "X-CuePorch-Social": "1",
            ...(team ? { "X-CuePorch-Team": "1" } : {}) },
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
    if (visibility) {
        for (const option of visibility.options) {
            option.disabled = !data.modes.includes(option.value);
        }
        if (!data.modes.includes(visibility.value))
            visibility.value = data.modes[0] || "";
    }
    historySection.hidden = data.jobs.length === 0;
    if (fresh) {
        cut = data.cut;
        company = data.company;
        scope = data.connection_scope;
        revision = data.connection_revision;
        if (connectionScope)
            connectionScope.value = provider === "shopify" ? data.shop : scope === "workspace" ? "Workspace account" : "Your connected account";
        if (companyLabel) {
            companyLabel.value = data.pages.find(page => page.id === company)?.name || "";
        }
        if (pageSelector) {
            pageSelector.replaceChildren(new Option(provider === "tiktok" ? "Choose an advertiser" : "Choose a Page", ""), ...data.pages.map(page => new Option(page.name, page.id)));
        }
        title.value = data.title;
        if (!data.allowed)
            notice.textContent = "Publishing to this destination is restricted by your workspace.";
        else if (!data.configured)
            notice.textContent = team ? `${providerName} needs workspace setup.` :
                `Configure ${providerName} for this workspace in Integrations.`;
        else
            notice.textContent = data.connected ? `Choose how this copy should appear on ${providerName}.` :
                `Connect ${providerName} to publish.`;
    }
    jobs.replaceChildren(...data.jobs.map(job => {
        const item = document.createElement("li");
        item.textContent = `${job.state} · ${job.privacy || job.requested_privacy} · ${job.sent.toLocaleString()} of ${job.bytes.toLocaleString()} bytes`;
        if (job.company) {
            const page = data.pages.find(candidate => candidate.id === job.company);
            const fallback = provider === "tiktok" ? "Advertiser " : "Page ";
            item.append(` · ${page?.name && page.name !== job.company ? page.name : fallback + job.company}`);
        }
        if (job.remote_id)
            item.append(` · Video ${job.remote_id}`);
        if (job.error)
            item.append(`. ${job.error}`);
        if (job.link) {
            const link = document.createElement("a");
            link.href = job.link;
            link.rel = "noreferrer";
            link.textContent = provider === "tiktok" ? "Open TikTok Ads Manager" : `Open on ${providerName}`;
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
const shopifySearch = form.querySelector('#shopifySearch');
const shopifyFind = form.querySelector('#shopifyFind');
const shopifyMore = form.querySelector('#shopifyMore');
const confirmBox = form.querySelector('input[type="checkbox"]');
let lastQuery = '';
let nextCursor = null;
let loading = false;
async function loadProducts(more = false) {
    if (loading || !shopifySearch || !shopifyFind || !shopifyMore)
        return;
    const q = more ? lastQuery : shopifySearch.value.trim();
    const params = new URLSearchParams({ q });
    if (revision)
        params.set('revision', revision);
    if (more && nextCursor)
        params.set('cursor', nextCursor);
    if (!more) {
        lastQuery = q;
        nextCursor = null;
        shopifyMore.hidden = true;
        if (pageSelector) {
            pageSelector.length = 0;
            pageSelector.add(new Option('Choose a product', ''));
            pageSelector.selectedIndex = 0;
        }
        if (confirmBox)
            confirmBox.checked = false;
    }
    loading = true;
    shopifyFind.disabled = shopifyMore.disabled = true;
    notice.textContent = 'Loading…';
    try {
        const data = await api(undefined, `${endpoint}/products?${params}`);
        if (pageSelector) {
            const keep = more ? pageSelector.value : '';
            for (const p of data.products)
                pageSelector.add(new Option(p.title, p.id));
            if (keep)
                pageSelector.value = keep;
        }
        nextCursor = data.cursor;
        shopifyMore.hidden = !data.cursor;
        notice.textContent = data.products.length
            ? ''
            : more ? 'No more products' : 'No products found';
    }
    catch (e) {
        notice.textContent = e?.message || 'Failed to load products';
    }
    finally {
        loading = false;
        shopifyFind.disabled = shopifyMore.disabled = false;
    }
}
if (shopifySearch && shopifyFind && shopifyMore) {
    shopifySearch.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            void loadProducts();
        }
    });
    shopifyFind.addEventListener('click', () => void loadProducts());
    shopifyMore.addEventListener('click', () => void loadProducts(true));
}
async function open() {
    try {
        const data = await api();
        render(data, true);
        if (provider === "shopify" && data.connected && data.configured && data.allowed)
            void loadProducts();
        if (!team)
            sessionStorage.setItem("cueporchOperator", credential);
    }
    catch (error) {
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
            ? { visibility: fields.get("visibility"), made_for_kids: fields.has("madeForKids") }
            : provider === "tiktok"
                ? { company: fields.get("company"), visibility: "private" }
                : { company: pageSelector ? fields.get("company") : company, visibility: fields.has("publicPost") ? "public" : null };
        render(await api({ id: draft, cut, title: fields.get("title"), description: fields.get("description"), connection_scope: scope, connection_revision: revision, ...options }));
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
