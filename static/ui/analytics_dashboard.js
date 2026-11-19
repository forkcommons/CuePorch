const dimensionNames = {
    video: "Video", date: "Date", device: "Device", source: "Referring site", surface: "Playback surface",
};
const metricNames = {
    impressions: "Impressions", views: "Views", view_rate: "View rate", finishes: "Finishes",
    watched_seconds: "Seconds watched", average_watched_seconds: "Average seconds watched",
};
const parts = Object.keys(metricNames);
const count = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const percent = new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 });
const identity = document.querySelector("#reportIdentity");
const key = document.querySelector("#reportKey");
const panel = document.querySelector("#analyticsDashboard");
const controls = document.querySelector("#reportControls");
const message = document.querySelector("#reportStatus");
const search = document.querySelector("#reportSearch");
const videos = document.querySelector("#reportVideos");
const selectedLabel = document.querySelector("#reportSelection");
const metric = document.querySelector("#trendMetric");
const body = document.querySelector("#reportRows");
const select = (name) => controls.elements.namedItem(name);
const input = (name) => controls.elements.namedItem(name);
const chosen = new Set();
let catalog = [];
let overview = [];
let requestNumber = 0;
key.value = sessionStorage.getItem("cueporchOperator") || "";
async function call(path, filters) {
    const headers = { Authorization: "Bearer " + key.value };
    if (filters)
        headers["Content-Type"] = "application/json";
    const response = await fetch("/api/analytics/" + path, {
        method: filters ? "POST" : "GET", headers, body: filters && JSON.stringify(filters),
    });
    if (!response.ok) {
        const detail = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
        throw new Error(detail?.textContent || `Report unavailable (${response.status})`);
    }
    return response;
}
function selectedCount() {
    selectedLabel.textContent = chosen.size ? `${count.format(chosen.size)} selected` : "All videos";
}
function videoOptions() {
    const query = search.value.trim().toLocaleLowerCase();
    const list = document.createDocumentFragment();
    for (const item of catalog) {
        if (!query || chosen.has(item.id) || item.title.toLocaleLowerCase().includes(query)) {
            list.append(new Option(item.title, item.id, false, chosen.has(item.id)));
        }
    }
    videos.replaceChildren(list);
    selectedCount();
}
videos.onchange = () => {
    if (videos.selectedOptions.length > 10000) {
        message.textContent = "Choose up to 10,000 videos.";
        videoOptions();
        return;
    }
    chosen.clear();
    for (const item of Array.from(videos.selectedOptions))
        chosen.add(item.value);
    selectedCount();
};
search.oninput = videoOptions;
document.querySelector("#clearVideos").onclick = () => {
    chosen.clear();
    videoOptions();
};
function datePreset() {
    const days = Number(select("dates").value);
    if (!days)
        return;
    const today = new Date();
    const from = new Date(today.getTime() - (days - 1) * 86400000);
    input("from").value = from.toISOString().slice(0, 10);
    input("to").value = today.toISOString().slice(0, 10);
}
select("dates").onchange = datePreset;
input("from").oninput = input("to").oninput = () => { select("dates").value = ""; };
for (const [name, label] of Object.entries(dimensionNames))
    select("group").add(new Option(label, name));
function breakdownOptions() {
    const breakdown = select("breakdown");
    const current = breakdown.value;
    breakdown.replaceChildren(new Option("None", ""));
    for (const [name, label] of Object.entries(dimensionNames)) {
        if (name !== select("group").value)
            breakdown.add(new Option(label, name));
    }
    breakdown.value = current === select("group").value ? "" : current;
}
select("group").onchange = breakdownOptions;
breakdownOptions();
for (const name of parts)
    metric.add(new Option(metricNames[name], name));
metric.value = "views";
function renderTrend() {
    const name = metric.value;
    const maximum = overview.reduce((max, row) => Math.max(max, row[name]), 1);
    const table = document.querySelector("#reportTrend");
    table.replaceChildren();
    for (const bucket of overview) {
        const row = table.insertRow();
        row.insertCell().textContent = bucket.period;
        const cell = row.insertCell();
        const bar = document.createElement("meter");
        bar.max = maximum;
        bar.value = bucket[name];
        bar.setAttribute("aria-hidden", "true");
        cell.append(bar, " ", formatted(name, bucket[name]));
    }
}
metric.onchange = renderTrend;
function formatted(name, value) {
    return (name === "view_rate" ? percent : count).format(value);
}
function videoLink(cell, title, uid) {
    if (!uid) {
        cell.textContent = title;
        return;
    }
    const link = document.createElement("a");
    link.href = "/operator/videos/" + encodeURIComponent(uid) + "/analytics";
    link.textContent = title;
    cell.append(link);
}
function render(report) {
    const { group, breakdown } = report.filters;
    const totals = document.querySelector("#reportTotals");
    totals.replaceChildren();
    for (const name of parts) {
        const item = document.createElement("div");
        const heading = document.createElement("dt");
        const data = document.createElement("dd");
        heading.textContent = metricNames[name];
        data.textContent = formatted(name, report.totals[name]);
        item.append(heading, data);
        totals.append(item);
    }
    overview = report.overview;
    renderTrend();
    const columns = document.querySelector("#reportColumns");
    columns.replaceChildren();
    const labels = [dimensionNames[group], ...(breakdown ? [dimensionNames[breakdown]] : []),
        ...parts.map(name => metricNames[name])];
    for (const label of labels) {
        const header = document.createElement("th");
        header.scope = "col";
        header.textContent = label;
        columns.append(header);
    }
    body.replaceChildren();
    for (const entry of report.rows) {
        const row = body.insertRow();
        videoLink(row.insertCell(), entry.category, group === "video" ? entry.video : null);
        if (breakdown)
            videoLink(row.insertCell(), entry.breakdown, breakdown === "video" ? entry.video : null);
        for (const name of parts)
            row.insertCell().textContent = formatted(name, entry[name]);
    }
    document.querySelector("#reportEmpty").hidden = report.rows.length > 0;
}
function filters() {
    const data = Object.fromEntries(new FormData(controls));
    delete data.dates;
    data.source = String(data.source).trim();
    return { ...data, videos: [...chosen] };
}
async function apply() {
    const request = ++requestNumber;
    if (!controls.reportValidity())
        return;
    message.textContent = "";
    const report = await (await call("video-report", filters())).json();
    if (request === requestNumber)
        render(report);
}
function showError(error) {
    message.textContent = error instanceof Error ? error.message : String(error);
}
async function open() {
    catalog = await (await call("videos")).json();
    const initial = new URLSearchParams(location.search).get("video");
    if (initial && catalog.some(item => item.id === initial))
        chosen.add(initial);
    videoOptions();
    datePreset();
    await apply();
    sessionStorage.setItem("cueporchOperator", key.value);
    panel.hidden = false;
    identity.hidden = true;
}
identity.onsubmit = (event) => { event.preventDefault(); void open().catch(showError); };
controls.onsubmit = (event) => { event.preventDefault(); void apply().catch(showError); };
document.querySelector("#downloadReport").onclick = async () => {
    if (!controls.reportValidity())
        return;
    try {
        const response = await call("video-report?format=csv", filters());
        const object = URL.createObjectURL(await response.blob());
        const link = document.createElement("a");
        link.href = object;
        link.download = "cueporch-video-report.csv";
        link.click();
        setTimeout(() => URL.revokeObjectURL(object), 1000);
    }
    catch (error) {
        showError(error);
    }
};
if (key.value)
    void open().catch(showError);
export {};
