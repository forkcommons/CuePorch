import {checked, teamRequest} from "./team_api.js";

interface ViewingRow extends MetricsRow {
  member: string; name: string; email: string; removed: number | null;
  work: string; title: string;
}
interface TeamSummaryRow extends MetricsRow {
  id: string; label: string; removed: number | null;
  viewers: number; videos: number;
}
interface TeamReport {
  group: "member" | "video";
  date_from: string; date_to: string;
  viewers: number; videos: number; views: number;
  rows: ViewingRow[]; summary: TeamSummaryRow[];
}

const access = document.querySelector<HTMLFormElement>("#teamAnalyticsAccess")!;
const credential = access.querySelector<HTMLInputElement>("input")!;
const status = document.querySelector<HTMLElement>("#teamAnalyticsStatus")!;
const settings = document.querySelector<HTMLFormElement>("#teamAnalyticsSettings")!;
const enabled = settings.querySelector<HTMLInputElement>("input")!;
const report = document.querySelector<HTMLElement>("#teamAnalyticsReport")!;
const filter = document.querySelector<HTMLFormElement>("#teamAnalyticsFilter")!;
const group = document.querySelector<HTMLSelectElement>("#teamAnalyticsGroup")!;
const from = filter.elements.namedItem("from") as HTMLInputElement;
const to = filter.elements.namedItem("to") as HTMLInputElement;
const reset = document.querySelector<HTMLButtonElement>("#teamAnalyticsReset")!;
const totals = document.querySelector<HTMLElement>("#teamAnalyticsTotals")!;
const rows = document.querySelector<HTMLTableSectionElement>("#teamAnalyticsRows")!;
const number = new Intl.NumberFormat(undefined, {maximumFractionDigits: 1});
let selection: {key: "member" | "video"; id: string} | undefined;

function call(path: string, method = "GET", body = {}) {
  return teamRequest(path, method, body, credential.value).then(checked);
}

async function action(run: () => Promise<void>) {
  status.textContent = "";
  try {
    await run();
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : String(error);
  }
}

function query() {
  const params = new URLSearchParams();
  if (from.value) params.set("from", from.value);
  if (to.value) params.set("to", to.value);
  params.set("group", group.value);
  if (selection) params.set(selection.key, selection.id);
  return params;
}

function filterButton(key: "member" | "video", id: string, title: string) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = title;
  button.onclick = () => { selection = {key, id}; void action(loadReport); };
  return button;
}

interface MetricsRow {
  views: number;
  completions: number;
  net_fraction: number | null;
  avg_watched_fraction: number | null;
  net_seconds: number;
  covered_seconds: number;
}

const metricLabels = [
  "Views",
  "Completions",
  "Net progress",
  "Average watched",
  "Net time (s)",
  "Time covered (s)",
] as const;

const percent = new Intl.NumberFormat(undefined, {
  style: "percent",
  maximumFractionDigits: 1,
});

function formatPercent(value: number | null): string {
  return value === null ? "—" : percent.format(value);
}

function appendMetrics(row: HTMLTableRowElement, item: MetricsRow): void {
  const texts: string[] = [
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

function updateHead(cells: string[]) {
  const tr = document.querySelector<HTMLTableRowElement>("#teamAnalyticsHead")!;
  tr.replaceChildren();
  for (const text of cells) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = text;
    tr.append(th);
  }
}

async function loadReport() {
  const data: TeamReport = await (await call("analytics?" + query())).json();
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
  } else {
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
  const state: {enabled: boolean} = await (await call("analytics/settings")).json();
  access.hidden = true;
  settings.hidden = false;
  enabled.checked = state.enabled;
  report.hidden = !state.enabled;
  if (state.enabled) await loadReport();
  else {
    totals.textContent = "";
    rows.replaceChildren();
  }
}

access.onsubmit = event => { event.preventDefault(); void action(loadSettings); };
settings.onsubmit = event => {
  event.preventDefault();
  void action(async () => {
    await call("analytics/settings", "PUT", {enabled: enabled.checked});
    await loadSettings();
    status.textContent = "Viewing privacy saved.";
  });
};
filter.onsubmit = event => { event.preventDefault(); void action(loadReport); };
group.onchange = () => { selection = undefined; void action(loadReport); };
reset.onclick = () => { selection = undefined; void action(loadReport); };
document.querySelector<HTMLButtonElement>("#teamAnalyticsExport")!.onclick = () => void action(async () => {
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
if (credential.value) void action(loadSettings);
