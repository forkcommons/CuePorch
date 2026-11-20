type Request = <T>(path: string, method?: string, body?: object, integration?: boolean) => Promise<T>;

interface SegmentsResponse {
  segments: { id: string; name: string }[];
  after: string | null;
}

interface ImportResponse {
  added: number;
  existing: number;
  duplicates: number;
  skipped: number;
}

type SyncReceipt = {
  enabled: boolean;
  state: "off" | "scheduled" | "complete" | "retry" | "problem";
  next_at?: number | null;
  last_at?: number | null;
  problem?: string | null;
  last_result?: ImportResponse | null;
};

export function setupHubSpot(
  request: Request,
  refreshRoster: () => void,
): (base: string) => void {
  const listsBtn = document.querySelector<HTMLButtonElement>("#eventHubSpotLists")!;
  const moreBtn = document.querySelector<HTMLButtonElement>("#eventHubSpotMore")!;
  const segmentSel = document.querySelector<HTMLSelectElement>("#eventHubSpotSegment")!;
  const importBtn = document.querySelector<HTMLButtonElement>("#eventHubSpotImport")!;
  const status = document.querySelector<HTMLElement>("#eventHubSpotStatus")!;
  const startSyncBtn = document.querySelector<HTMLButtonElement>("#eventHubSpotStartSync")!;
  const stopSyncBtn = document.querySelector<HTMLButtonElement>("#eventHubSpotStopSync")!;
  const readSyncBtn = document.querySelector<HTMLButtonElement>("#eventHubSpotReadSync")!;
  const syncStatus = document.querySelector<HTMLElement>("#eventHubSpotSyncStatus")!;

  let base = "";
  let scope = 0;
  let cursor: string | null = null;
  let busy = false;

  const setBusy = (v: boolean) => {
    busy = v;
    listsBtn.disabled = v;
    moreBtn.disabled = v;
    segmentSel.disabled = v;
    importBtn.disabled = v || !segmentSel.value;
    startSyncBtn.disabled = v || !segmentSel.value;
    stopSyncBtn.disabled = v;
    readSyncBtn.disabled = v;
  };

  segmentSel.onchange = () => {
    importBtn.disabled = busy || !segmentSel.value;
    startSyncBtn.disabled = busy || !segmentSel.value;
  };

  const setStatus = (msg: string) => {
    status.textContent = msg;
  };

  const resetOptions = () => {
    while (segmentSel.options.length > 1) segmentSel.remove(1);
    segmentSel.selectedIndex = 0;
  };

  const appendSegments = (segments: { id: string; name: string }[]) => {
    const seen = new Set<string>();
    for (let i = 0; i < segmentSel.options.length; i++) seen.add(segmentSel.options[i].value);
    for (const s of segments) {
      if (seen.has(s.id)) continue;
      seen.add(s.id);
      segmentSel.add(new Option(s.name, s.id));
    }
  };

  const updateMore = () => {
    moreBtn.hidden = !cursor;
  };

  const load = async (after: string | null) => {
    if (busy) return;
    const gen = scope;
    setBusy(true);
    setStatus(after ? "Loading more…" : "Loading…");
    try {
      const q = after ? `?after=${encodeURIComponent(after)}` : "";
      const res = await request<SegmentsResponse>(`/api/integrations/hubspot/segments${q}`);
      if (gen !== scope) return;
      if (!after) resetOptions();
      appendSegments(res.segments);
      cursor = res.after === after ? null : res.after;
      updateMore();
      setStatus("");
    } catch (error) {
      if (gen !== scope) return;
      setStatus((error as Error).message);
    } finally {
      if (gen === scope) setBusy(false);
    }
  };

  listsBtn.addEventListener("click", () => { void load(null); });
  moreBtn.addEventListener("click", () => { if (cursor) void load(cursor); });

  importBtn.addEventListener("click", async () => {
    if (busy) return;
    const segment = segmentSel.value;
    if (!segment) {
      setStatus("Select a HubSpot list.");
      return;
    }
    const gen = scope;
    setBusy(true);
    setStatus("Importing…");
    try {
      const res = await request<ImportResponse>(
        `${base}/registration/hubspot`,
        "POST",
        { segment },
        true,
      );
      if (gen !== scope) return;
      setStatus(
        `${res.added} added, ${res.existing} already registered, ${res.duplicates} duplicates, ${res.skipped} skipped.`,
      );
      refreshRoster();
    } catch (error) {
      if (gen !== scope) return;
      setStatus((error as Error).message);
    } finally {
      if (gen === scope) setBusy(false);
    }
  });

  const syncRequest = async (method: "GET" | "PUT" | "DELETE") => {
    if (!base || busy) return;
    const gen = scope;
    const segment = segmentSel.value;
    setBusy(true);
    syncStatus.textContent =
      method === "GET" ? "Loading sync…" : method === "PUT" ? "Scheduling sync…" : "Stopping sync…";
    try {
      const receipt = await request<SyncReceipt>(
        `${base}/registration/hubspot/sync`,
        method,
        method === "GET" ? undefined : method === "PUT" ? { segment } : {},
        method !== "GET",
      );
      if (gen !== scope) return;
      const stateLabels: Record<string, string> = {
  off: "Automatic refresh is off",
  scheduled: "Automatic refresh scheduled",
  complete: "Automatic refresh complete",
  retry: "Waiting to retry",
  problem: "Automatic refresh needs attention",
};
const parts: string[] = [stateLabels[receipt.state] ?? receipt.state];
      if (receipt.last_at) parts.push(`last ${new Date(receipt.last_at * 1000).toLocaleString()}`);
      if (receipt.next_at) parts.push(`next ${new Date(receipt.next_at * 1000).toLocaleString()}`);
      if (receipt.last_result) {
        const result = receipt.last_result;
        parts.push(
          `${result.added} added, ${result.existing} already registered, ${result.duplicates} duplicates, ${result.skipped} skipped`,
        );
      }
      if (receipt.problem) parts.push(receipt.problem);
      syncStatus.textContent = parts.join(" · ");
      stopSyncBtn.hidden = receipt.state === "off";
      if (method === "GET" && receipt.last_result) refreshRoster();
    } catch (error) {
      if (gen !== scope) return;
      syncStatus.textContent = (error as Error).message;
    } finally {
      if (gen === scope) setBusy(false);
    }
  };

  startSyncBtn.addEventListener("click", () => { void syncRequest("PUT"); });
  stopSyncBtn.addEventListener("click", () => { void syncRequest("DELETE"); });
  readSyncBtn.addEventListener("click", () => { void syncRequest("GET"); });

  updateMore();

  return (newBase: string) => {
    scope++;
    base = newBase;
    cursor = null;
    resetOptions();
    updateMore();
    setStatus("");
    syncStatus.textContent = "";
    stopSyncBtn.hidden = true;
    setBusy(false);
    void syncRequest("GET");
  };
}
