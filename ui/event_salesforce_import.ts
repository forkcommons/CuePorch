type Request = <T>(path: string, method?: string, body?: object, integration?: boolean) => Promise<T>;
type ImportResult = { added: number; existing: number; duplicates: number; skipped: number };
type Receipt = { available: boolean; enabled: boolean; state: string; campaign?: string; next_at?: number | null; last_result?: ImportResult | null; problem?: string | null };
type Page = { campaigns: { id: string; name: string }[]; next_cursor: string | null };

export function setupSalesforceImport(request: Request, onRosterChanged: () => void) {
  const $ = <T extends Element>(s: string) => document.querySelector(s) as T;
  const load = $<HTMLButtonElement>("#sfCampaignLoad");
  const more = $<HTMLButtonElement>("#sfCampaignMore");
  const importNow = $<HTMLButtonElement>("#sfImportNow");
  const schedule = $<HTMLButtonElement>("#sfImportSchedule");
  const stop = $<HTMLButtonElement>("#sfImportStop");
  const refresh = $<HTMLButtonElement>("#sfImportRefresh");
  const select = $<HTMLSelectElement>("#sfCampaign");
  const status = $<HTMLElement>("#sfImportStatus");
  const company = $<HTMLInputElement>("#sfLeadCompany");
  const send = $<HTMLButtonElement>("#sfOutboundEnable");
  const cancelSend = $<HTMLButtonElement>("#sfOutboundStop");
  const readSend = $<HTMLButtonElement>("#sfOutboundRefresh");
  const sendStatus = $<HTMLElement>("#sfOutboundStatus");
  const engagement = $<HTMLInputElement>("#sfOutboundEngagement");
  const complete = $<HTMLButtonElement>("#sfOutboundComplete");

  let base = "";
  let generation = 0;
  let busy = false;
  let cursor: string | null = null;
  let receipt: Receipt | undefined;
  const importPath = () => base + "/registration/salesforce";
  const syncPath = () => importPath() + "/sync";
  type OutboundReceipt = { available: boolean; enabled: boolean; state: string; campaign?: string; company?: string; states: Record<string, number>; engagement?: boolean; engagement_states?: Record<string, number>; engagement_report_at?: number | null };
  let outbound: OutboundReceipt | undefined;
  const outboundPath = () => importPath() + "/outbound";

  function updateControls() {
    const locked = busy || receipt?.available !== true;
    load.disabled = locked;
    more.disabled = locked;
    select.disabled = locked;
    importNow.disabled = locked || !select.value;
    schedule.disabled = locked || !select.value;
    refresh.disabled = busy;
    stop.disabled = busy;
    more.hidden = cursor === null;
    stop.hidden = !receipt || receipt.state === "off";
    send.disabled = busy || outbound?.available !== true || !select.value || !company.value.trim();
    company.disabled = busy || outbound?.available !== true;
    cancelSend.disabled = busy;
    cancelSend.hidden = !(outbound?.enabled || outbound?.state === "stale");
    readSend.disabled = busy;
    engagement.disabled = busy || outbound?.available !== true;
    const reportAt = outbound?.engagement_report_at;
    complete.hidden = !(outbound?.enabled && outbound?.engagement && reportAt != null && Number.isFinite(reportAt) && reportAt * 1000 > Date.now());
    complete.disabled = busy;
  }

  function resultText(r: ImportResult) {
    return `Added ${r.added}. Already registered ${r.existing}. Duplicates ${r.duplicates}. Skipped ${r.skipped}.`;
  }

  function renderReceipt(next: Receipt | null) {
    receipt = next ?? undefined;
    const p: string[] = [];
    if (!receipt) p.push("Disconnected.");
    else {
      if (!receipt.available) p.push("Salesforce import unavailable.");
      const s = receipt.state;
      if (s === "off") p.push("Off.");
      else if (s === "scheduled") p.push("Scheduled.");
      else if (s === "retry") p.push("Retry pending.");
      else if (s === "complete") p.push("Complete.");
      else if (s === "problem") p.push("Problem.");
      else p.push(`State: ${s}.`);
      if (receipt.problem) p.push(receipt.problem);
      if (receipt.next_at) p.push(`Next attempt at ${new Date(receipt.next_at * 1000).toLocaleString()}.`);
      if (receipt.last_result) p.push(resultText(receipt.last_result));
    }
    status.textContent = p.join(" ");
    updateControls();
  }

  function renderOutbound(r: OutboundReceipt) {
    outbound = r;
    company.value = r.company ?? "Individual";
    engagement.checked = !!r.engagement;
    const p: string[] = [];
    if (!r.available) {
      p.push("Salesforce delivery unavailable.");
    } else if (r.state === "off") {
      p.push("Off.");
    } else {
      if (r.enabled) p.push("Connected.");
      else p.push(`State: ${r.state}.`);
      const savedId = r.campaign || "";
      const opt = savedId ? Array.from(select.options).find(o => o.value === savedId) : undefined;
      const campaignName = opt ? (opt.textContent ?? savedId) : savedId;
      if (campaignName) p.push(`Campaign: ${campaignName}.`);
      const entries = Object.entries(r.states ?? {});
      p.push(entries.length ? `Counts: ${entries.map(([k, v]) => `${k} ${v}`).join(", ")}.` : "No deliveries yet.");
      const engagementEntries = Object.entries(r.engagement_states ?? {});
      if (engagementEntries.length) p.push(`Engagement counts: ${engagementEntries.map(([k, v]) => `${k} ${v}`).join(", ")}.`);
      if (r.engagement) p.push(r.engagement_report_at != null ? `Engagement report at ${new Date(r.engagement_report_at * 1000).toLocaleString()}.` : "Waiting for confirmed webinar end.");
    }
    sendStatus.textContent = p.join(" ");
    updateControls();
  }

  async function run(task: (ticket: number) => Promise<void>, errors = status) {
    if (!base || busy) return;
    const ticket = generation;
    busy = true;
    updateControls();
    try {
      await task(ticket);
    } catch (err) {
      if (ticket === generation) errors.textContent = err instanceof Error ? err.message : String(err);
    } finally {
      if (ticket === generation) {
        busy = false;
        updateControls();
      }
    }
  }

  const PH = () => new Option("Choose a campaign", "");

  async function loadCampaigns(ticket: number, reset: boolean): Promise<void> {
    if (!reset && cursor === null) return;
    const saved = select.value || receipt?.campaign || outbound?.campaign || "";
    if (reset) { select.replaceChildren(PH()); cursor = null; }
    const path = "/api/integrations/salesforce/campaigns" +
      (!reset && cursor ? `?cursor=${encodeURIComponent(cursor)}` : "");
    const page = await request<Page>(path);
    if (ticket !== generation) return;
    const seen = new Set<string>(Array.from(select.options).map(o => o.value));
    for (const c of page.campaigns) {
      if (!seen.has(c.id)) { seen.add(c.id); select.add(new Option(c.name, c.id)); }
    }
    if (seen.has(saved)) select.value = saved;
    cursor = page.next_cursor ?? null;
    updateControls();
  }

  load.addEventListener("click", () => run(t => loadCampaigns(t, true)));
  more.addEventListener("click", () => run(t => loadCampaigns(t, false)));
  select.addEventListener("change", updateControls);
  company.addEventListener("input", updateControls);

  send.addEventListener("click", () => run(async (t) => {
    const r = await request<OutboundReceipt>(outboundPath(), "PUT", { campaign: select.value, company: company.value.trim(), engagement: engagement.checked }, true);
    if (t !== generation) return;
    renderOutbound(r);
  }, sendStatus));

  cancelSend.addEventListener("click", () => run(async (t) => {
    const r = await request<OutboundReceipt>(outboundPath(), "DELETE", {}, true);
    if (t !== generation) return;
    renderOutbound(r);
  }, sendStatus));

  readSend.addEventListener("click", () => run(async (t) => {
    const r = await request<OutboundReceipt>(outboundPath());
    if (t !== generation) return;
    renderOutbound(r);
  }, sendStatus));

  complete.addEventListener("click", () => run(async (t) => {
    const r = await request<OutboundReceipt>(outboundPath() + "/complete", "POST", {}, true);
    if (t !== generation) return;
    renderOutbound(r);
  }, sendStatus));

  importNow.addEventListener("click", () => run(async (t) => {
    const r = await request<ImportResult>(importPath(), "POST", { campaign: select.value }, true);
    if (t !== generation) return;
    status.textContent = resultText(r);
    onRosterChanged();
  }));

  schedule.addEventListener("click", () => run(async (t) => {
    const r = await request<Receipt>(syncPath(), "PUT", { campaign: select.value }, true);
    if (t !== generation) return;
    renderReceipt(r);
  }));

  stop.addEventListener("click", () => run(async (t) => {
    const r = await request<Receipt>(syncPath(), "DELETE", {}, true);
    if (t !== generation) return;
    renderReceipt(r);
  }));

  refresh.addEventListener("click", () => run(async (t) => {
    const r = await request<Receipt>(syncPath());
    if (t !== generation) return;
    renderReceipt(r); onRosterChanged();
  }));

  return (target: string) => {
    base = target;
    generation++;
    busy = false; cursor = null; receipt = undefined;
    outbound = undefined;
    select.replaceChildren(PH());
    status.textContent = "";
    sendStatus.textContent = "";
    company.value = "Individual";
    engagement.checked = false;
    updateControls();
    run(async (t) => {
      const [syncRes, outboundRes] = await Promise.allSettled([
        request<Receipt>(syncPath()),
        request<OutboundReceipt>(outboundPath()),
      ]);
      if (t !== generation) return;
      if (syncRes.status === "fulfilled") {
        renderReceipt(syncRes.value);
      } else {
        status.textContent = syncRes.reason instanceof Error ? syncRes.reason.message : String(syncRes.reason);
      }
      if (outboundRes.status === "fulfilled") {
        renderOutbound(outboundRes.value);
      } else {
        sendStatus.textContent = outboundRes.reason instanceof Error ? outboundRes.reason.message : String(outboundRes.reason);
      }
    });
  };
}
