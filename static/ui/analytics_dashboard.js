import { setupAnalyticsFolders } from "./analytics_folder_picker.js";
const dimensionNames = {
    video: "Video", date: "Date", device: "Device", source: "Referring site", region: "Region", surface: "Playback surface",
    caption: "Captions and subtitles",
    audio: "Audio track",
    streaming: "Streaming type",
};
const metricNames = {
    impressions: "Impressions", views: "Views", view_rate: "View rate", finishes: "Finishes",
    watched_seconds: "Seconds watched", average_watched_seconds: "Average seconds watched",
    unique_impressions: "Unique impressions", unique_viewers: "Unique viewers", average_watched_fraction: "Average percent watched",
    downloads: "Downloads", likes: "Likes", comments: "Comments",
};
const parts = Object.keys(metricNames);
const count = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const percent = new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 });
const gigabytes = new Intl.NumberFormat(undefined, { maximumFractionDigits: 9 });
const identity = document.querySelector("#reportIdentity");
const key = document.querySelector("#reportKey");
const panel = document.querySelector("#analyticsDashboard");
const controls = document.querySelector("#reportControls");
const reportType = document.querySelector("#reportType");
const reportPanels = {
    video: document.querySelector("#videoReport"),
    engagement: document.querySelector("#engagementReport"),
    rating: document.querySelector("#ratingReport"),
    bandwidth: document.querySelector("#bandwidthReport"),
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
const eventIds = new Set();
let overview = [];
let activeComparison;
let comparisonRequest = 0;
let compared = [];
let requestNumber = 0;
key.value = sessionStorage.getItem("cueporchOperator") || "";
const folderPicker = setupAnalyticsFolders(panel, videoOptions, (ids) => {
    const candidate = new Set([...chosen, ...ids]);
    message.textContent = "";
    if (candidate.size > 10000) {
        message.textContent = "Choose up to 10,000 items.";
        return;
    }
    chosen.clear();
    for (const id of candidate)
        chosen.add(id);
    videoOptions();
}, (ids) => {
    for (const id of ids)
        chosen.delete(id);
    videoOptions();
});
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
    const kind = reportType.value;
    const everything = usesVideoMetrics(kind) || kind === "bandwidth" ? "All videos and events" : "All videos";
    selectedLabel.textContent = chosen.size ? `${count.format(chosen.size)} selected` : everything;
}
function videoOptions() {
    const query = search.value.trim().toLocaleLowerCase();
    const list = document.createDocumentFragment();
    const kind = reportType.value;
    const allowEvents = usesVideoMetrics(kind) || kind === "bandwidth";
    document.querySelector("#reportVideoLabel").textContent = allowEvents ? "Videos and events" : "Videos";
    document.querySelector("#reportSearchLabel").textContent = allowEvents ? "Search videos and events" : "Search library videos";
    for (const item of catalog) {
        if (item.event && !allowEvents) {
            chosen.delete(item.id);
            continue;
        }
        if (chosen.has(item.id) || ((!query || item.title.toLocaleLowerCase().includes(query)) && folderPicker.includes(item))) {
            list.append(new Option(item.title, item.id, false, chosen.has(item.id)));
        }
    }
    videos.replaceChildren(list);
    selectedCount();
}
videos.onchange = () => {
    if (videos.selectedOptions.length > 10000) {
        message.textContent = "Choose up to 10,000 items.";
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
    return kind !== "engagement" && kind !== "rating" && kind !== "bandwidth";
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
function compareCheckbox(entry) {
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.setAttribute("aria-label", `Compare ${entry.category}${entry.breakdown ? ` ${entry.breakdown}` : ""}`);
    const key = JSON.stringify(entry.key);
    const active = activeComparison;
    if (!active)
        return checkbox;
    checkbox.checked = active.selected.has(key);
    checkbox.addEventListener("change", () => {
        const current = activeComparison;
        if (!current)
            return;
        if (checkbox.checked) {
            if (current.selected.size >= 10) {
                checkbox.checked = false;
                message.textContent = "You can compare up to 10 entries.";
                return;
            }
            current.selected.set(key, entry);
        }
        else {
            current.selected.delete(key);
        }
        void refreshComparison();
    });
    return checkbox;
}
async function refreshComparison() {
    const requestId = ++comparisonRequest;
    const active = activeComparison;
    compared = [];
    renderTrend();
    if (!active)
        return;
    const keys = [...active.selected.values()].map(entry => entry.key);
    if (!keys.length)
        return;
    message.textContent = "";
    try {
        const response = await call(`${active.kind}-report`, {
            ...active.options,
            compare: keys,
        });
        const payload = await response.json();
        if (requestId !== comparisonRequest ||
            activeComparison !== active) {
            return;
        }
        compared = payload.comparison ?? [];
        renderTrend();
    }
    catch (error) {
        if (requestId !== comparisonRequest ||
            activeComparison !== active) {
            return;
        }
        showError(error);
    }
}
function drawReportTrend(canvas, periods, series, maximum, formatValue) {
    const chart = canvas.getContext("2d");
    if (!chart)
        return;
    const margin = 52;
    const top = 18;
    const width = canvas.width - margin - 25;
    const max = Number.isFinite(maximum) && maximum > 0 ? maximum : 1;
    const styles = [
        { color: "#166273", dash: [] },
        { color: "#c05621", dash: [] },
        { color: "#2f855a", dash: [] },
        { color: "#6b46c1", dash: [] },
        { color: "#b83280", dash: [] },
        { color: "#2c5282", dash: [] },
        { color: "#975a16", dash: [] },
        { color: "#4a5568", dash: [] },
        { color: "#166273", dash: [6, 4] },
        { color: "#c05621", dash: [6, 4] },
        { color: "#2f855a", dash: [6, 4] },
    ];
    const columns = series.length > 6 ? 3 : 2;
    const legendRows = Math.ceil(series.length / columns);
    const legendItemHeight = 16;
    const legendHeight = legendRows * legendItemHeight;
    const legendBottom = canvas.height - 4;
    const legendTop = legendBottom - legendHeight;
    const dateY = legendTop - 6;
    const bottom = Math.max(top + 20, dateY - 20);
    chart.clearRect(0, 0, canvas.width, canvas.height);
    chart.fillStyle = "#ffffff";
    chart.fillRect(0, 0, canvas.width, canvas.height);
    const toUtc = (s) => s.length === 10 ? Date.parse(s + "T00:00:00Z") : Date.parse(s);
    const points = periods
        .map((key) => ({ key, t: toUtc(key) }))
        .filter((p) => Number.isFinite(p.t));
    if (points.length === 0)
        return;
    points.sort((a, b) => a.t - b.t);
    const minT = points[0].t;
    const maxT = points[points.length - 1].t;
    const span = maxT - minT;
    const xFor = (t) => span === 0 ? margin + width / 2 : margin + (width * (t - minT)) / span;
    const yFor = (value) => {
        const clamped = Math.max(0, Math.min(max, value));
        return bottom - ((bottom - top) * clamped) / max;
    };
    const fitText = (text, maxWidth) => {
        if (chart.measureText(text).width <= maxWidth)
            return text;
        let lo = 0;
        let hi = text.length;
        while (lo < hi) {
            const mid = Math.ceil((lo + hi) / 2);
            const t = text.slice(0, mid) + "…";
            if (chart.measureText(t).width <= maxWidth)
                lo = mid;
            else
                hi = mid - 1;
        }
        return text.slice(0, lo) + "…";
    };
    chart.strokeStyle = "#a5b6bb";
    chart.lineWidth = 1;
    chart.fillStyle = "#152e33";
    chart.font = "13px sans-serif";
    chart.beginPath();
    chart.moveTo(margin, bottom);
    chart.lineTo(margin + width, bottom);
    chart.moveTo(margin, top);
    chart.lineTo(margin + width, top);
    chart.stroke();
    chart.textAlign = "right";
    chart.fillText(fitText(formatValue(0), margin - 10), margin - 8, bottom + 3);
    chart.fillText(fitText(formatValue(max), margin - 10), margin - 8, top + 5);
    chart.strokeStyle = "#e2e8f0";
    chart.beginPath();
    chart.moveTo(xFor(minT), top);
    chart.lineTo(xFor(minT), bottom);
    if (span !== 0) {
        chart.moveTo(xFor(maxT), top);
        chart.lineTo(xFor(maxT), bottom);
    }
    chart.stroke();
    series.forEach((s, index) => {
        const style = styles[index % styles.length];
        chart.strokeStyle = style.color;
        chart.setLineDash(style.dash);
        chart.lineWidth = 2.5;
        chart.beginPath();
        let started = false;
        for (const p of points) {
            const value = s.values.get(p.key);
            if (value === null || value === undefined || !Number.isFinite(value)) {
                started = false;
                continue;
            }
            const x = xFor(p.t);
            const y = yFor(value);
            if (started)
                chart.lineTo(x, y);
            else
                chart.moveTo(x, y);
            started = true;
        }
        chart.stroke();
        chart.setLineDash([]);
        chart.fillStyle = style.color;
        for (const p of points) {
            const value = s.values.get(p.key);
            if (value === null || value === undefined || !Number.isFinite(value))
                continue;
            const x = xFor(p.t);
            const y = yFor(value);
            chart.beginPath();
            chart.arc(x, y, 3, 0, Math.PI * 2);
            chart.fill();
        }
    });
    const formatDate = (t) => new Date(t).toISOString().slice(0, 10);
    chart.fillStyle = "#152e33";
    chart.font = "12px sans-serif";
    chart.textAlign = "center";
    const minLabel = formatDate(minT);
    const maxLabel = formatDate(maxT);
    const minW = chart.measureText(minLabel).width;
    const maxW = chart.measureText(maxLabel).width;
    const minX = xFor(minT);
    const maxX = xFor(maxT);
    const minLabelX = Math.max(minW / 2 + 2, Math.min(canvas.width - minW / 2 - 2, minX));
    const maxLabelX = Math.max(maxW / 2 + 2, Math.min(canvas.width - maxW / 2 - 2, maxX));
    chart.fillText(minLabel, minLabelX, dateY);
    if (span !== 0) {
        chart.fillText(maxLabel, maxLabelX, dateY);
    }
    if (series.length > 0) {
        const legendX = margin;
        const legendWidth = width;
        const columnWidth = legendWidth / columns;
        chart.font = "12px sans-serif";
        chart.textAlign = "left";
        chart.textBaseline = "middle";
        for (let i = 0; i < series.length; i++) {
            const col = i % columns;
            const row = Math.floor(i / columns);
            const x = legendX + col * columnWidth;
            const y = legendTop + row * legendItemHeight + legendItemHeight / 2;
            const style = styles[i % styles.length];
            chart.strokeStyle = style.color;
            chart.setLineDash(style.dash);
            chart.lineWidth = 2.5;
            chart.beginPath();
            chart.moveTo(x, y);
            chart.lineTo(x + 16, y);
            chart.stroke();
            chart.setLineDash([]);
            const maxLabelWidth = columnWidth - 24;
            const label = fitText(series[i].label, maxLabelWidth);
            chart.fillStyle = "#152e33";
            chart.fillText(label, x + 22, y);
        }
        chart.textBaseline = "alphabetic";
    }
}
function renderTrend() {
    const name = metric.value;
    const series = [];
    if (document.querySelector("#trendTotal").checked) {
        series.push({
            label: "Total",
            values: new Map(overview.map((bucket) => [bucket.period, bucket[name]])),
        });
    }
    for (const item of compared) {
        const entry = activeComparison?.selected.get(JSON.stringify(item.key));
        if (!entry)
            continue;
        const label = entry.breakdown
            ? `${entry.category} ${entry.breakdown}`
            : entry.category;
        series.push({ label, values: new Map(item.overview.map(bucket => [bucket.period, bucket[name]])) });
    }
    const periodSet = new Set();
    for (const s of series) {
        for (const key of s.values.keys())
            periodSet.add(key);
    }
    const periods = [...periodSet].sort();
    let maximum = 1;
    for (const s of series) {
        for (const v of s.values.values()) {
            if (v !== null && v > maximum)
                maximum = v;
        }
    }
    const columnsRow = document.querySelector("#trendColumns");
    columnsRow.replaceChildren();
    const dateTh = document.createElement("th");
    dateTh.scope = "col";
    dateTh.textContent = "UTC date";
    columnsRow.append(dateTh);
    for (const s of series) {
        const th = document.createElement("th");
        th.scope = "col";
        th.textContent = s.label;
        columnsRow.append(th);
    }
    const tbody = document.querySelector("#reportTrend");
    tbody.replaceChildren();
    for (const period of periods) {
        const row = tbody.insertRow();
        row.insertCell().textContent = period;
        for (const s of series) {
            const cell = row.insertCell();
            const v = s.values.get(period) ?? null;
            if (v !== null) {
                const bar = document.createElement("meter");
                bar.max = maximum;
                bar.value = v;
                bar.setAttribute("aria-hidden", "true");
                cell.append(bar, " ", formatted(name, v));
            }
            else {
                cell.textContent = formatted(name, null);
            }
        }
    }
    drawReportTrend(document.querySelector("#reportChart"), periods, series, maximum, value => formatted(name, value));
}
metric.onchange = renderTrend;
document.querySelector("#trendTotal").onchange = renderTrend;
function formatted(name, value) {
    if (value === null)
        return "—";
    return (name === "view_rate" || name === "average_watched_fraction" ? percent : count).format(value);
}
function videoLink(cell, title, uid) {
    if (!uid) {
        cell.textContent = title;
        return;
    }
    const link = document.createElement("a");
    link.href = eventIds.has(uid) ? "/operator/analytics?video=" + encodeURIComponent(uid)
        : "/operator/videos/" + encodeURIComponent(uid) + "/analytics";
    link.textContent = title;
    cell.append(link);
}
function renderTop(top) {
    document.querySelector("#reportTop").hidden = !top?.video.length;
    const lists = { video: "#topVideos", region: "#topRegions", device: "#topDevices", source: "#topSources" };
    for (const dimension of Object.keys(lists)) {
        const list = document.querySelector(lists[dimension]);
        list.replaceChildren();
        for (const entry of top?.[dimension] ?? []) {
            const row = document.createElement("li");
            videoLink(row, entry.label, entry.video);
            row.append(` — ${count.format(entry.views)}`);
            list.append(row);
        }
    }
}
function renderVideo(report) {
    const { group, breakdown } = report.filters;
    document.querySelector("#trackBreakdownNote").hidden =
        ![group, breakdown].some(dimension => dimension === "caption" || dimension === "audio");
    document.querySelector("#communityBreakdownNote").hidden = report.community_breakdown;
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
    const { identified_impressions, identified_views, impressions, views } = report.totals;
    const coverage = document.querySelector("#uniqueCoverage");
    coverage.hidden = identified_impressions === impressions && identified_views === views;
    coverage.textContent = `Browser identity available for ${count.format(identified_impressions)} of ${count.format(impressions ?? 0)} impressions and ${count.format(identified_views)} of ${count.format(views ?? 0)} views.`;
    overview = report.overview;
    renderTrend();
    renderTop(report.top);
    const columns = document.querySelector("#reportColumns");
    columns.replaceChildren();
    const labels = [...(activeComparison ? ["Compare"] : []), dimensionNames[group],
        ...(breakdown ? [dimensionNames[breakdown]] : []),
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
        if (activeComparison)
            row.insertCell().append(compareCheckbox(entry));
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
function renderBandwidth(report) {
    document.querySelector("#bandwidthLabel").textContent = report.group === "month" ? "Month (UTC)" : "Content";
    const table = document.querySelector("#bandwidthRows");
    table.replaceChildren();
    for (const entry of [{ ...report.totals, label: "Total", video: null }, ...report.rows]) {
        const row = table.insertRow();
        videoLink(row.insertCell(), entry.label, entry.video);
        for (const bytes of [entry.live_bytes, entry.vod_bytes, entry.total_bytes]) {
            row.insertCell().textContent = gigabytes.format(bytes / 1_000_000_000);
        }
    }
}
function syncReportFilters(kind) {
    for (const label of controls.querySelectorAll("[data-video-filter], [data-playback-filter], [data-bandwidth-filter]")) {
        const field = label.querySelector("input,select");
        if (label.hasAttribute("data-bandwidth-filter"))
            field.disabled = kind !== "bandwidth";
        else if (label.hasAttribute("data-video-filter")) {
            field.disabled = !usesVideoMetrics(kind) || (field.name === "group" && kind !== "video");
        }
        else
            field.disabled = kind === "rating" || kind === "bandwidth";
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
    if (data.caption_language)
        data.caption_language = String(data.caption_language).trim();
    if (data.audio_language)
        data.audio_language = String(data.audio_language).trim();
    return { ...data, videos: [...chosen] };
}
async function apply() {
    const request = ++requestNumber;
    const kind = reportType.value;
    syncReportFilters(kind);
    if (!controls.reportValidity())
        return;
    message.textContent = "";
    const submitted = filters();
    const report = await (await call(`${kind}-report`, submitted)).json();
    if (request !== requestNumber)
        return;
    compared = [];
    activeComparison = usesVideoMetrics(kind) && report.filters.group !== "date" ? { kind, options: submitted, selected: new Map() } : undefined;
    if (usesVideoMetrics(kind))
        renderVideo(report);
    else if (kind === "engagement")
        renderEngagement(report);
    else if (kind === "bandwidth")
        renderBandwidth(report);
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
    const videoResponse = await call("videos?events=1&folders=1");
    const { items, folders } = await videoResponse.json();
    catalog = items;
    folderPicker.populate(folders, items);
    eventIds.clear();
    for (const item of catalog)
        if (item.event)
            eventIds.add(item.id);
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
reportType.onchange = () => { videoOptions(); void apply().catch(showError); };
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
