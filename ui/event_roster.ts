import {setupHubSpot} from "./event_hubspot.js";
import {setupMarketo} from "./event_marketo.js";
import {setupMailchimp} from "./event_mailchimp.js";
import {setupMarketoImport} from "./event_marketo_import.js";
import {setupSalesforceImport} from "./event_salesforce_import.js";

type Request = <T>(path: string, method?: string, body?: object, integration?: boolean) => Promise<T>;
type Download = (path: string, filename: string) => Promise<void>;

interface Roster {
  attendees: {email: string; registered: number | null; source: string;
    answers: {first_name?: string; last_name?: string};
    watch_time: {live_seconds: number; replay_seconds: number; plays: number} | null}[];
  total: number;
  offset: number;
  page_size: number;
}

const sourceNames: Record<string, string> = {form: "Registration form", api: "API", csv: "CSV import", hubspot: "HubSpot", "hubspot-scheduled": "HubSpot automatic refresh",
  salesforce: "Salesforce",
  "salesforce-scheduled": "Salesforce automatic import",
  marketo: "Marketo",
  "marketo-scheduled": "Marketo automatic import",
  "retained-link": "Retained join link"};

function watchTime(seconds?: number) {
  if (seconds === undefined) return "—";
  return seconds.toLocaleString(undefined, {maximumFractionDigits: 1}) + " s";
}

export function setupEventRoster(request: Request, download: Download) {
  const panel = document.querySelector<HTMLDetailsElement>("#eventRoster")!;
  const form = document.querySelector<HTMLFormElement>("#eventRosterSearch")!;
  const rows = document.querySelector<HTMLTableSectionElement>("#eventRosterRows")!;
  const prev = document.querySelector<HTMLButtonElement>("#eventRosterPrevious")!;
  const next = document.querySelector<HTMLButtonElement>("#eventRosterNext")!;
  const exportCsv = document.querySelector<HTMLButtonElement>("#eventRosterDownload")!;
  const status = document.querySelector("#eventRosterStatus")!;
  let base = "";
  let occurrence = 0;
  let page = 0;
  let pageSize = 0;
  let generation = 0;
  let filter = "";

  async function load(offset: number) {
    if (!base) return;
    const ticket = ++generation;
    const query = new URLSearchParams({email: filter, offset: String(offset)});
    status.textContent = "Loading attendees…";
    prev.disabled = next.disabled = true;
    rows.replaceChildren();
    try {
      const result = await request<Roster>(base + "/registrants?" + query);
      if (ticket !== generation) return;
      page = result.offset;
      pageSize = result.page_size;
      for (const attendee of result.attendees) {
        const row = rows.insertRow();
        row.insertCell().textContent = [attendee.answers.first_name, attendee.answers.last_name].filter(Boolean).join(" ");
        row.insertCell().textContent = attendee.email;
        row.insertCell().textContent = attendee.registered === null ? "Unknown" : new Date(attendee.registered * 1000).toLocaleString();
        row.insertCell().textContent = sourceNames[attendee.source] ?? attendee.source;
        row.insertCell().textContent = watchTime(attendee.watch_time?.live_seconds);
        row.insertCell().textContent = watchTime(attendee.watch_time?.replay_seconds);
      }
      prev.disabled = page === 0;
      next.disabled = page + pageSize >= result.total;
      status.textContent = result.attendees.length ? `${page + 1}–${page + result.attendees.length} of ${result.total} attendees.`
        : "No matching attendees.";
    } catch (error) {
      if (ticket === generation) status.textContent = (error as Error).message;
    }
  }

  const chooseHubSpot = setupHubSpot(request, () => void load(0));
  const chooseMarketo = setupMarketo(request);
  const chooseMailchimp = setupMailchimp(request);
  const chooseMarketoImport = setupMarketoImport(request, () => void load(0));
  const chooseSalesforceImport = setupSalesforceImport(request, () => void load(0));

  panel.addEventListener("toggle", () => {
    if (panel.open) void load(page);
  });
  form.onsubmit = event => {
    event.preventDefault();
    filter = String(new FormData(form).get("email") ?? "");
    void load(0);
  };
  prev.onclick = () => void load(Math.max(0, page - pageSize));
  next.onclick = () => void load(page + pageSize);
  exportCsv.onclick = async () => {
    const selected = base;
    try {
      await download(base + "/registration/export", `cueporch-attendees-${occurrence}.csv`);
    } catch (error) {
      if (selected === base) status.textContent = (error as Error).message;
    }
  };

  return (event: string, opens: number) => {
    const selected = `/api/live-events/${event}/${opens}`;
    if (base === selected) return;
    generation++;
    occurrence = opens;
    base = selected;
    chooseHubSpot(base);
    chooseMarketo(base, event);
    chooseMailchimp(base, event);
    chooseMarketoImport(base, event);
    chooseSalesforceImport(base);
    filter = "";
    page = 0;
    panel.open = false;
    form.reset();
    rows.replaceChildren();
    prev.disabled = next.disabled = true;
    status.textContent = "";
  };
}
