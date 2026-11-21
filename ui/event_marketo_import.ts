import { marketoMappingFields, renderMarketoMapping, readMarketoMapping } from "./event_marketo.js";

type Request = <T>(path: string, method?: string, body?: object, integration?: boolean) => Promise<T>;

type ListPage = {
  lists: { id: number; name: string }[];
  next_offset: number | null;
};

type ImportResult = {
  added: number;
  existing: number;
  duplicates: number;
  skipped: number;
};

type Receipt = {
  available: boolean;
  enabled: boolean;
  state: string;
  list?: number;
  mapping?: Record<string, string>;
  next_at?: number | null;
  last_at?: number | null;
  last_result?: ImportResult | null;
  problem?: string | null;
};

export function setupMarketoImport(
  request: Request,
  onRosterChanged: () => void,
) {
  const panel = document.getElementById("eventMarketoImportPanel") as HTMLElement;
  const loadButton = document.getElementById("eventMarketoListLoad") as HTMLButtonElement;
  const moreButton = document.getElementById("eventMarketoListMore") as HTMLButtonElement;
  const list = document.getElementById("eventMarketoList") as HTMLSelectElement;
  const mapping = document.getElementById("eventMarketoImportMapping") as HTMLDivElement;
  const importButton = document.getElementById("eventMarketoImportNow") as HTMLButtonElement;
  const scheduleButton = document.getElementById("eventMarketoImportSchedule") as HTMLButtonElement;
  const stopButton = document.getElementById("eventMarketoImportStop") as HTMLButtonElement;
  const refreshButton = document.getElementById("eventMarketoImportRefresh") as HTMLButtonElement;
  const status = document.getElementById("eventMarketoImportStatus") as HTMLElement;

  let base = "";
  let eventId = "";
  let generation = 0;
  let busy = false;
  let loaded = false;
  let offset: number | null = null;
  let receipt: Receipt | undefined;

  function syncControls(): void {
    const unavailable = receipt?.available !== true;
    loadButton.disabled = busy || unavailable;
    moreButton.disabled = busy || unavailable;
    list.disabled = busy || unavailable;
    moreButton.hidden = offset === null;
    const noSelection = list.value === "";
    importButton.disabled = busy || unavailable || !loaded || noSelection;
    scheduleButton.disabled = busy || unavailable || !loaded || noSelection;
    stopButton.hidden = !receipt || receipt.state === "off";
    stopButton.disabled = busy;
    refreshButton.disabled = busy;
  }

  async function run(task: (ticket: number) => Promise<void>): Promise<void> {
    if (busy || !base) return;
    busy = true;
    const ticket = ++generation;
    syncControls();
    try {
      await task(ticket);
    } catch (error) {
      if (ticket === generation) {
        status.textContent =
          error instanceof Error ? error.message : String(error);
      }
    } finally {
      if (ticket === generation) {
        busy = false;
        syncControls();
      }
    }
  }

  function resultText(result: ImportResult): string {
    return `Added ${result.added}. Already registered ${result.existing}. Duplicates ${result.duplicates}. Skipped ${result.skipped}.`;
  }

  function renderReceipt(result: Receipt): void {
    receipt = result;
    panel.hidden = !result.available && result.state === "off";

    let text: string;
    switch (result.state) {
      case "scheduled":
        text = "Automatic import scheduled.";
        break;
      case "retry":
        text = "Automatic import will retry.";
        break;
      case "problem":
        text = "Automatic import needs attention.";
        break;
      case "complete":
        text = "Automatic import completed.";
        break;
      default:
        text = "Automatic import is off.";
    }
    if (result.problem) {
      text += ` ${result.problem}`;
    }
    if (result.next_at) {
      text += ` Next: ${new Date(result.next_at * 1000).toLocaleString()}.`;
    }
    if (result.last_at) {
      text += ` Last: ${new Date(result.last_at * 1000).toLocaleString()}.`;
    }
    if (result.last_result) {
      text += ` ${resultText(result.last_result)}`;
    }
    status.textContent = text;
  }

  async function loadLists(ticket: number, reset: boolean) {
    const off = reset ? 0 : offset;
    if (off == null) return;
    const [page, fields] = await Promise.all([
      request<ListPage>(`/api/integrations/marketo/static-lists?offset=${off}`, undefined, undefined, true),
      reset ? marketoMappingFields(request, eventId) : Promise.resolve(null),
    ]);
    if (ticket !== generation) return;
    const selected = list.value || (receipt?.list !== undefined ? String(receipt.list) : "");
    const savedMapping = loaded ? readMarketoMapping(mapping) : receipt?.mapping;
    if (reset) list.replaceChildren(new Option("Choose a static list", ""));
    const seen = new Set<string>();
    for (const o of Array.from(list.options)) seen.add(o.value);
    for (const entry of page.lists) {
      const value = String(entry.id);
      if (seen.has(value)) continue;
      list.add(new Option(entry.name, value));
      seen.add(value);
    }
    if (receipt?.list != null && !seen.has(String(receipt.list))) {
      const value = String(receipt.list);
      list.add(new Option(`Marketo list #${value}`, value));
      seen.add(value);
    }
    if (selected && seen.has(selected)) list.value = selected;
    if (fields) {
      renderMarketoMapping(mapping, fields.form.fields, fields.leadFields, savedMapping, "Do not import");
      loaded = fields.form.enabled;
      if (!loaded) status.textContent = "Enable webinar registration to import attendees.";
    }
    offset = page.next_offset;
  }

  const importEndpoint = () => base + "/registration/marketo";
  const syncEndpoint = () => importEndpoint() + "/sync";

  function payload() {
    if (!loaded) return null;
    const listId = Number(list.value);
    if (!Number.isSafeInteger(listId) || listId <= 0) return null;
    return { list: listId, mapping: readMarketoMapping(mapping) };
  }

  list.onchange = syncControls;

  loadButton.onclick = () => run((ticket) => loadLists(ticket, true));
  moreButton.onclick = () => run((ticket) => loadLists(ticket, false));

  importButton.onclick = () =>
    run(async (ticket) => {
      const body = payload();
      if (!body) return;
      const result = await request<ImportResult>(importEndpoint(), "POST", body, true);
      if (ticket !== generation) return;
      status.textContent = resultText(result);
      onRosterChanged();
    });

  scheduleButton.onclick = () =>
    run(async (ticket) => {
      const body = payload();
      if (!body) return;
      const result = await request<Receipt>(syncEndpoint(), "PUT", body, true);
      if (ticket !== generation) return;
      renderReceipt(result);
    });

  stopButton.onclick = () =>
    run(async (ticket) => {
      const result = await request<Receipt>(syncEndpoint(), "DELETE", {}, true);
      if (ticket !== generation) return;
      renderReceipt(result);
    });

  function refreshStatus(reloadRoster: boolean) {
    return run(async ticket => {
      const result = await request<Receipt>(syncEndpoint(), "GET", undefined, true);
      if (ticket === generation) {
        renderReceipt(result);
        if (reloadRoster) onRosterChanged();
      }
    });
  }

  refreshButton.onclick = () => void refreshStatus(true);

  return (selectedBase: string, selectedEvent: string) => {
    generation++;
    base = selectedBase;
    eventId = selectedEvent;
    busy = false;
    receipt = undefined;
    offset = null;
    loaded = false;
    list.replaceChildren(new Option("Choose a static list", ""));
    mapping.replaceChildren();
    panel.hidden = true;
    status.textContent = "";
    syncControls();
    void refreshStatus(false);
  };

}
