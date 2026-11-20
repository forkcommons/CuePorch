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

export function setupHubSpot(
  request: Request,
  refreshRoster: () => void,
): (base: string) => void {
  const listsBtn = document.querySelector<HTMLButtonElement>("#eventHubSpotLists")!;
  const moreBtn = document.querySelector<HTMLButtonElement>("#eventHubSpotMore")!;
  const segmentSel = document.querySelector<HTMLSelectElement>("#eventHubSpotSegment")!;
  const importBtn = document.querySelector<HTMLButtonElement>("#eventHubSpotImport")!;
  const status = document.querySelector<HTMLElement>("#eventHubSpotStatus")!;

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
  };

  segmentSel.onchange = () => {
    importBtn.disabled = busy || !segmentSel.value;
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

  updateMore();

  return (newBase: string) => {
    scope++;
    base = newBase;
    cursor = null;
    resetOptions();
    updateMore();
    setStatus("");
    setBusy(false);
  };
}
