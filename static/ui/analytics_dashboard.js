const dimensionNames = {
    video: "Video", date: "Date", device: "Device", source: "Referring site", region: "Region", surface: "Playback surface",
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
const reportType = document.querySelector("#reportType");
const reportPanels = {
    video: document.querySelector("#videoReport"),
    engagement: document.querySelector("#engagementReport"),
    rating: document.querySelector("#ratingReport"),
};
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
function usesVideoMetrics(kind) {
    return kind !== "engagement" && kind !== "rating";
}
function breakdownOptions() {
    const kind = reportType.value;
    const group = usesVideoMetrics(kind) && kind !== "video" ? kind : select("group").value;
    const breakdown = select("breakdown");
    const current = breakdown.value;
    breakdown.replaceChildren(new Option("None", ""));
    for (const [name, label] of Object.entries(dimensionNames)) {
        if (name !== group)
            breakdown.add(new Option(label, name));
    }
    breakdown.value = current === group ? "" : current;
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
function renderVideo(report) {
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
function renderCurve(values) {
    const canvas = document.querySelector("#engagementCurve");
    const chart = canvas.getContext("2d");
    if (!chart)
        return;
    const margin = 52;
    const top = 18;
    const bottom = canvas.height - 34;
    const width = canvas.width - margin - 25;
    const max = Math.max(1, ...values);
    chart.clearRect(0, 0, canvas.width, canvas.height);
    chart.fillStyle = "#ffffff";
    chart.fillRect(0, 0, canvas.width, canvas.height);
    chart.strokeStyle = "#a5b6bb";
    chart.lineWidth = 1;
    chart.fillStyle = "#152e33";
    chart.font = "13px sans-serif";
    chart.textAlign = "center";
    for (const mark of [0, 25, 50, 75, 100]) {
        const x = margin + width * mark / 100;
        chart.beginPath();
        chart.moveTo(x, top);
        chart.lineTo(x, bottom);
        chart.stroke();
        chart.fillText(`${mark}%`, x, bottom + 20);
    }
    chart.textAlign = "right";
    chart.fillText("0", margin - 8, bottom + 3);
    chart.fillText(count.format(max), margin - 8, top + 5);
    chart.strokeStyle = "#166273";
    chart.lineWidth = 2.5;
    chart.beginPath();
    values.forEach((value, index) => chart.lineTo(margin + width * index / 100, bottom - (bottom - top) * value / max));
    chart.stroke();
}
function renderEngagement(report) {
    const { totals } = report;
    const table = document.querySelector("#engagementRows");
    table.replaceChildren();
    for (const entry of [{ ...totals, title: "Total", video: null }, ...report.rows]) {
        const row = table.insertRow();
        videoLink(row.insertCell(), entry.title, entry.video);
        const fields = [count.format(entry.views), entry.average_watched === null ? "—" : percent.format(entry.average_watched),
            percent.format(entry.completion), ...[1, 25, 50, 75, 100].map(point => count.format(entry.points[point]))];
        for (const field of fields)
            row.insertCell().textContent = field;
    }
    const coverage = document.querySelector("#engagementCoverage");
    coverage.hidden = totals.measured === totals.views;
    coverage.textContent = `Duration is available for ${count.format(totals.measured)} of ${count.format(totals.views)} views.`;
    renderCurve(report.retention);
}
function renderRatings(report) {
    const table = document.querySelector("#ratingRows");
    table.replaceChildren();
    for (const entry of [...report.totals.map(total => ({ ...total, title: "Total", video: null })), ...report.rows]) {
        const row = table.insertRow();
        videoLink(row.insertCell(), entry.title, entry.video);
        const thumbs = entry.mode === "thumbs";
        row.insertCell().textContent = thumbs ? "Thumbs" : "Stars";
        row.insertCell().textContent = count.format(entry.votes);
        row.insertCell().textContent = entry.average === null ? "—" : (thumbs ? percent : count).format(entry.average);
        const labels = thumbs ? ["Down", "Up"] : ["1 star", "2 stars", "3 stars", "4 stars", "5 stars"];
        row.insertCell().textContent = entry.scores.map((votes, index) => `${labels[index]} ${count.format(votes)}`).join(", ");
    }
}
function syncReportFilters(kind) {
    for (const label of controls.querySelectorAll("[data-video-filter], [data-playback-filter]")) {
        const field = label.querySelector("input,select");
        field.disabled = label.hasAttribute("data-video-filter")
            ? !usesVideoMetrics(kind) || (field.name === "group" && kind !== "video")
            : kind === "rating";
        label.hidden = field.disabled;
    }
    if (usesVideoMetrics(kind))
        breakdownOptions();
}
function filters() {
    const data = Object.fromEntries(new FormData(controls));
    delete data.dates;
    if (data.source)
        data.source = String(data.source).trim();
    if (data.region)
        data.region = String(data.region).trim();
    return { ...data, videos: [...chosen] };
}
async function apply() {
    const request = ++requestNumber;
    const kind = reportType.value;
    syncReportFilters(kind);
    if (!controls.reportValidity())
        return;
    message.textContent = "";
    const report = await (await call(`${kind}-report`, filters())).json();
    if (request !== requestNumber)
        return;
    if (usesVideoMetrics(kind))
        renderVideo(report);
    else if (kind === "engagement")
        renderEngagement(report);
    else
        renderRatings(report);
    const activePanel = usesVideoMetrics(kind) ? "video" : kind;
    for (const [name, section] of Object.entries(reportPanels))
        section.hidden = name !== activePanel;
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
reportType.onchange = () => { void apply().catch(showError); };
document.querySelector("#downloadReport").onclick = async () => {
    if (!controls.reportValidity())
        return;
    try {
        const kind = reportType.value;
        const response = await call(`${kind}-report?format=csv`, filters());
        const object = URL.createObjectURL(await response.blob());
        const link = document.createElement("a");
        link.href = object;
        link.download = `cueporch-${kind}-report.csv`;
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
