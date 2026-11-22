import { checked, teamRequest } from "./team_api.js";
const access = document.querySelector("#teamAnalyticsAccess");
const credential = access.querySelector("input");
const status = document.querySelector("#teamAnalyticsStatus");
const settings = document.querySelector("#teamAnalyticsSettings");
const enabled = settings.querySelector("input");
const report = document.querySelector("#teamAnalyticsReport");
const filter = document.querySelector("#teamAnalyticsFilter");
const group = document.querySelector("#teamAnalyticsGroup");
const from = filter.elements.namedItem("from");
const to = filter.elements.namedItem("to");
const reset = document.querySelector("#teamAnalyticsReset");
const totals = document.querySelector("#teamAnalyticsTotals");
const rows = document.querySelector("#teamAnalyticsRows");
const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
let selection;
function call(path, method = "GET", body = {}) {
    return teamRequest(path, method, body, credential.value).then(checked);
}
async function action(run) {
    status.textContent = "";
    try {
        await run();
    }
    catch (error) {
        status.textContent = error instanceof Error ? error.message : String(error);
    }
}
function query() {
    const params = new URLSearchParams();
    if (from.value)
        params.set("from", from.value);
    if (to.value)
        params.set("to", to.value);
    params.set("group", group.value);
    if (selection)
        params.set(selection.key, selection.id);
    return params;
}
function filterButton(key, id, title) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = title;
    button.onclick = () => { selection = { key, id }; void action(loadReport); };
    return button;
}
const metricLabels = [
    "Views",
    "Completions",
    "Net progress",
    "Average watched",
    "Net time (s)",
    "Time covered (s)",
];
const percent = new Intl.NumberFormat(undefined, {
    style: "percent",
    maximumFractionDigits: 1,
});
function formatPercent(value) {
    return value === null ? "—" : percent.format(value);
}
function appendMetrics(row, item) {
    const texts = [
        number.format(item.views),
        number.format(item.completions),
        formatPercent(item.net_fraction),
        formatPercent(item.avg_watched_fraction),
        number.format(item.net_seconds),
        number.format(item.covered_seconds),
    ];
    for (const text of texts) {
        const cell = row.insertCell();
        cell.textContent = text;
    }
}
function updateHead(cells) {
    const tr = document.querySelector("#teamAnalyticsHead");
    tr.replaceChildren();
    for (const text of cells) {
        const th = document.createElement("th");
        th.scope = "col";
        th.textContent = text;
        tr.append(th);
    }
}
async function loadReport() {
    const data = await (await call("analytics?" + query())).json();
    from.value = data.date_from;
    to.value = data.date_to;
    totals.textContent = `Members ${number.format(data.viewers)} · Videos ${number.format(data.videos)} · Views ${number.format(data.views)}`;
    reset.hidden = !selection;
    rows.replaceChildren();
    if (selection) {
        updateHead(["Member", "Video", ...metricLabels]);
        for (const item of data.rows) {
            const row = rows.insertRow();
            row.insertCell().append(filterButton("member", item.member, item.name + (item.removed ? " (removed)" : "")));
            row.insertCell().append(filterButton("video", item.work, item.title));
            appendMetrics(row, item);
        }
    }
    else {
        const byMember = data.group === "member";
        updateHead(byMember ? ["Member", "Videos", ...metricLabels] : ["Video", "Viewers", ...metricLabels]);
        for (const item of data.summary) {
            const row = rows.insertRow();
            row.insertCell().append(filterButton(data.group, item.id, item.label + (item.removed ? " (removed)" : "")));
            row.insertCell().textContent = number.format(byMember ? item.videos : item.viewers);
            appendMetrics(row, item);
        }
    }
    report.hidden = false;
}
async function loadSettings() {
    const state = await (await call("analytics/settings")).json();
    access.hidden = true;
    settings.hidden = false;
    enabled.checked = state.enabled;
    report.hidden = !state.enabled;
    if (state.enabled)
        await loadReport();
    else {
        totals.textContent = "";
        rows.replaceChildren();
    }
}
access.onsubmit = event => { event.preventDefault(); void action(loadSettings); };
settings.onsubmit = event => {
    event.preventDefault();
    void action(async () => {
        await call("analytics/settings", "PUT", { enabled: enabled.checked });
        await loadSettings();
        status.textContent = "Viewing privacy saved.";
    });
};
filter.onsubmit = event => { event.preventDefault(); void action(loadReport); };
group.onchange = () => { selection = undefined; void action(loadReport); };
reset.onclick = () => { selection = undefined; void action(loadReport); };
document.querySelector("#teamAnalyticsExport").onclick = () => void action(async () => {
    const params = query();
    params.set("format", "csv");
    const file = URL.createObjectURL(await (await call("analytics?" + params)).blob());
    const link = document.createElement("a");
    link.href = file;
    link.download = "cueporch-team-analytics.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(file), 1000);
});
credential.value = sessionStorage.getItem("cueporchOperator") || "";
if (credential.value)
    void action(loadSettings);
