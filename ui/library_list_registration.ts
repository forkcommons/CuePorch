import { checked } from "./team_api.js";

type Provider = "constant-contact" | "campaign-monitor";

type Receipt = {
  available: boolean;
  enabled: boolean;
  state: "off" | "connected" | "stale";
  list_id: string | null;
  list_name: string | null;
  states: Record<string, number>;
};

type ListPage = {
  lists: { id: string; name: string }[];
  next_cursor: string | null;
};

type Settings = {
  enabled: boolean;
  first_name: string;
  last_name: string;
};

const labels = {
  queued: "Queued",
  waiting: "Waiting",
  sent: "Sent",
  submitted: "Submitted",
  pending_confirmation: "Pending confirmation",
  skipped: "Skipped",
  canceled: "Canceled",
  tracking_stopped: "Tracking stopped",
  problem: "Problem",
  unconfirmed: "Unconfirmed",
} as const;

async function request<T>(
  credential: () => string,
  path: string,
  method = "GET",
  body?: object,
): Promise<T> {
  const headers: Record<string, string> = {};
  const token = credential();
  if (token) headers.Authorization = `Bearer ${token}`;
  const mutation = method !== "GET";
  if (mutation) {
    headers["X-CuePorch-Integration"] = "1";
    if (body !== undefined) headers["Content-Type"] = "application/json";
  }
  const response = await checked(
    await fetch(path, {
      method,
      headers,
      body: mutation && body !== undefined ? JSON.stringify(body) : undefined,
    }),
  );
  return response.json();
}

function receiptStatus(receipt: Receipt, ready: boolean, provider: Provider): string {
  const name = provider === "constant-contact" ? "Constant Contact" : "Campaign Monitor";
  let status: string;
  if (receipt.state === "stale") {
    status = `Reconnect ${name} and choose a list again.`;
  } else if (!receipt.available) {
    status = `Connect ${name} in Integrations.`;
  } else if (!ready) {
    status = "Save an enabled registration form with first and last name fields.";
  } else if (receipt.state === "connected") {
    status = `${name} delivery is enabled.`;
  } else {
    status = `${name} delivery is off.`;
  }

  const counts = Object.entries(labels).flatMap(([key, label]) =>
    receipt.states[key] ? [label + ": " + receipt.states[key]] : [],
  );

  return counts.length ? `${status} ${counts.join(" ")}` : status;
}

export function setupLibraryListDelivery(
  panel: HTMLElement,
  credential: () => string,
  isCurrent: (endpoint: string) => boolean,
  provider: Provider,
) {
  const prefix = provider === "constant-contact"
    ? "registrationConstantContact"
    : "registrationCampaignMonitor";
  const control = <T extends HTMLElement>(suffix: string) =>
    panel.querySelector<T>("#" + prefix + suffix);
  const load = control<HTMLButtonElement>("Load")!;
  const more = provider === "constant-contact" ? control<HTMLButtonElement>("More")! : null;
  const client = provider === "campaign-monitor" ? control<HTMLSelectElement>("Client")! : null;
  const picker = control<HTMLSelectElement>("List")!;
  const save = control<HTMLButtonElement>("Save")!;
  const reload = control<HTMLButtonElement>("Refresh")!;
  const stop = control<HTMLButtonElement>("Stop")!;
  const status = control<HTMLElement>("Status")!;
  let generation = 0;

  return async (endpoint: string, settings: Settings) => {
    const scope = ++generation;
    const active = () => generation === scope && isCurrent(endpoint);
    const ready = settings.enabled && settings.first_name !== "off" && settings.last_name !== "off";
    const lists = new Map<string, string>();
    let receipt: Receipt | undefined;
    let nextCursor: string | null = null;
    let busy = false;
    let savedListVisible = true;

    if (client) {
      client.replaceChildren(new Option("Choose a client", ""));
      client.value = "";
    }

    function drawControls() {
      const disabled = busy || !receipt?.available || !ready;
      load.disabled = disabled;
      if (more) {
        more.hidden = nextCursor === null;
        more.disabled = disabled;
      }
      if (client) client.disabled = disabled;
      picker.disabled = disabled || picker.options.length <= 1;
      save.disabled = disabled || !picker.value;
      reload.disabled = busy;
      stop.hidden = !receipt || receipt.state === "off";
      stop.disabled = busy;
    }

    function drawLists(selected: string) {
      const options = new Map(lists);
      if (savedListVisible && receipt?.list_id && receipt.list_name && !options.has(receipt.list_id)) {
        options.set(receipt.list_id, receipt.list_name);
      }
      picker.replaceChildren(
        new Option("Choose a list", ""),
        ...Array.from(options, ([id, name]) => new Option(name, id)),
      );
      picker.value = options.has(selected) ? selected : "";
    }

    async function run(task: () => Promise<void>) {
      if (!active() || busy) return;
      busy = true;
      drawControls();
      try {
        await task();
        if (active() && receipt) status.textContent = receiptStatus(receipt, ready, provider);
      } catch (error) {
        if (active()) {
          panel.hidden = false;
          status.textContent = error instanceof Error ? error.message : String(error);
        }
      } finally {
        busy = false;
        if (active()) drawControls();
      }
    }

    async function readReceipt(method = "GET", body?: object) {
      const updated = await request<Receipt>(credential, endpoint, method, body);
      if (!active()) return;
      receipt = updated;
      savedListVisible = true;
      panel.hidden = !receipt.available && receipt.state === "off";
      drawLists(receipt.list_id ?? "");
    }

    async function loadLists(cursor?: string) {
      const query = cursor ? "?" + new URLSearchParams({cursor}) : "";
      const page = await request<ListPage>(
        credential, "/api/integrations/constant-contact/lists" + query,
      );
      if (!active()) return;
      if (!cursor) lists.clear();
      for (const list of page.lists) lists.set(list.id, list.name);
      nextCursor = page.next_cursor;
      drawLists(picker.value);
    }

    async function loadClients() {
      const data = await request<{ clients: { id: string; name: string }[] }>(
        credential,
        "/api/integrations/campaign-monitor/clients",
      );
      if (!active() || !client) return;
      const previous = client.value;
      client.replaceChildren(
        new Option("Choose a client", ""),
        ...data.clients.map(({ id, name }) => new Option(name, id)),
      );
      const keep = data.clients.some(({ id }) => id === previous);
      client.value = keep ? previous : "";
      if (previous && !keep) {
        lists.clear();
        nextCursor = null;
        savedListVisible = false;
        drawLists("");
      }
    }

    async function loadClientLists(clientId: string) {
      const data = await request<{ lists: { id: string; name: string }[] }>(
        credential,
        "/api/integrations/campaign-monitor/clients/" + encodeURIComponent(clientId) + "/lists",
      );
      if (!active() || !client || client.value !== clientId) return;
      lists.clear();
      for (const list of data.lists) lists.set(list.id, list.name);
      nextCursor = null;
      drawLists(picker.value);
    }

    panel.hidden = true;
    status.textContent = "";
    lists.clear();
    nextCursor = null;
    savedListVisible = true;
    drawLists("");
    picker.onchange = drawControls;
    load.onclick = () => {
      if (provider === "constant-contact") {
        void run(() => loadLists());
      } else {
        void run(() => loadClients());
      }
    };
    if (more) {
      more.onclick = () => {
        const cursor = nextCursor;
        if (cursor) void run(() => loadLists(cursor));
      };
    }
    if (client) {
      client.onchange = () => {
        const clientId = client.value;
        lists.clear();
        savedListVisible = false;
        nextCursor = null;
        drawLists("");
        if (clientId) void run(() => loadClientLists(clientId));
        else drawControls();
      };
    }
    save.onclick = () => {
      const listId = picker.value;
      if (listId) void run(() => readReceipt("PUT", {list_id: listId}));
    };
    reload.onclick = () => void run(() => readReceipt());
    stop.onclick = () => void run(() => readReceipt("DELETE", {}));
    await run(() => readReceipt());
  };
}
