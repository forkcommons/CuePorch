export {};

interface Counts { impressions: number; views: number; view_rate: number; finishes: number; watched_seconds: number; }
interface Day extends Counts { day: string; surface: string; }
interface Segment { second: number; percent: number; }
interface Report {
  title: string; date_from: string; date_to: string; surface: string;
  totals: Counts; daily: Day[]; retention: Segment[]; duration: number | null;
}

const identity = document.querySelector<HTMLFormElement>("#identify")!;
const key = document.querySelector<HTMLInputElement>("#analyticsKey")!;
const panel = document.querySelector<HTMLElement>("#analytics")!;
const message = document.querySelector<HTMLElement>("#analyticsStatus")!;
const title = document.querySelector<HTMLElement>("#analyticsTitle")!;
const range = document.querySelector<HTMLFormElement>("#analyticsRange")!;
const field = (name: string) => range.elements.namedItem(name) as HTMLInputElement | HTMLSelectElement;
const video = document.querySelector<HTMLVideoElement>("#retentionPreview")!;
const canvas = document.querySelector<HTMLCanvasElement>("#retentionPlot")!;
const data = document.querySelector<HTMLTableSectionElement>("#retentionRows")!;
const rows = document.querySelector<HTMLTableSectionElement>("#dailyRows")!;
const amount = new Intl.NumberFormat(undefined, {maximumFractionDigits: 1});
const percent = new Intl.NumberFormat(undefined, {style: "percent", maximumFractionDigits: 1});
const teamPreview = document.body.dataset.teamPreview;
const endpoint = (teamPreview ? "/api/team/video/" : "/api/library/work/") +
  encodeURIComponent(document.body.dataset.work || "");

key.value = sessionStorage.getItem("cueporchOperator") || "";

async function call(suffix: string, body?: object) {
  const headers: Record<string, string> = {Authorization: "Bearer " + (teamPreview ? "team-session" : key.value)};
  if (body) headers["Content-Type"] = "application/json";
  const response = await fetch(endpoint + suffix, {
    method: body ? "POST" : "GET", headers, body: body && JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = new DOMParser().parseFromString(await response.text(), "text/html").querySelector("p");
    throw new Error(detail?.textContent || `Request failed (${response.status})`);
  }
  return response;
}

function draw(retention: Segment[], duration: number) {
  const context = canvas.getContext("2d")!;
  const {width, height} = canvas;
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#22767b";
  canvas.hidden = retention.length === 0;
  data.replaceChildren();
  for (let index = 0; index < retention.length; index++) {
    const point = retention[index];
    const from = point.second / duration * width;
    const to = (retention[index + 1]?.second ?? duration) / duration * width;
    const h = point.percent * height;
    context.fillRect(from, height - h, to - from, h);
    const row = data.insertRow();
    const jump = document.createElement("button");
    jump.type = "button";
    jump.textContent = `${point.second}s`;
    jump.disabled = video.hidden;
    jump.onclick = () => { video.currentTime = point.second; };
    row.insertCell().append(jump);
    row.insertCell().textContent = percent.format(point.percent);
  }
  canvas.onclick = (event) => {
    const bounds = canvas.getBoundingClientRect();
    if (!video.hidden && Number.isFinite(video.duration)) {
      video.currentTime = (event.clientX - bounds.left) / bounds.width * duration;
    }
  };
}

function render(report: Report) {
  title.textContent = "Analytics for " + report.title;
  field("from").value = report.date_from;
  field("to").value = report.date_to;
  field("surface").value = report.surface;
  const counts = {
    impressions: amount.format(report.totals.impressions), views: amount.format(report.totals.views),
    viewRate: percent.format(report.totals.view_rate), finishes: amount.format(report.totals.finishes),
    timeWatched: amount.format(report.totals.watched_seconds) + " s",
  };
  for (const [name, value] of Object.entries(counts)) document.getElementById(name)!.textContent = value;
  draw(report.retention, report.duration || 1);
  rows.replaceChildren();
  for (const day of report.daily) {
    const row = rows.insertRow();
    const values = [day.day, day.surface === "watch" ? "Watch page" : "Embed",
      amount.format(day.impressions), amount.format(day.views), percent.format(day.view_rate),
      amount.format(day.finishes), amount.format(day.watched_seconds) + " s"];
    for (const value of values) row.insertCell().textContent = value;
  }
}

function query() {
  return new URLSearchParams({
    from: field("from").value, to: field("to").value, surface: field("surface").value,
  }).toString();
}

async function action(run: () => Promise<void>) {
  message.textContent = "";
  try {
    await run();
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : String(error);
  }
}

function open() {
  void action(async () => {
    const report: Report = await (await call("/analytics")).json();
    if (teamPreview) {
      video.src = teamPreview;
      video.hidden = false;
    } else {
      const work: {preview: string | null} = await (await call("")).json();
      if (work.preview) {
        const ticket: {preview: string} = await (await call("/ticket", {sha256: work.preview})).json();
        video.src = ticket.preview;
        video.hidden = false;
      }
      sessionStorage.setItem("cueporchOperator", key.value);
    }
    render(report);
    identity.hidden = true;
    panel.hidden = false;
  });
}

identity.onsubmit = (event) => { event.preventDefault(); open(); };
range.onsubmit = (event) => {
  event.preventDefault();
  void action(async () => { render(await (await call("/analytics?" + query())).json()); });
};
document.querySelector<HTMLButtonElement>("#exportAnalytics")!.onclick = () => void action(async () => {
  const file = URL.createObjectURL(await (await call("/analytics?format=csv&" + query())).blob());
  const link = document.createElement("a");
  link.href = file;
  link.download = "cueporch-video-analytics.csv";
  link.click();
  setTimeout(() => URL.revokeObjectURL(file), 1000);
});
if (teamPreview || key.value) open();
