

import { checked } from "./team_api.js";

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

function receiptStatus(receipt: Receipt, ready: boolean): string {
  let status: string;
  if (receipt.state === "stale") {
    status = "Reconnect Constant Contact and choose a list again.";
  } else if (!receipt.available) {
    status = "Connect Constant Contact in Integrations.";
  } else if (!ready) {
    status = "Save an enabled registration form with first and last name fields.";
  } else if (receipt.state === "connected") {
    status = "Constant Contact delivery is enabled.";
  } else {
    status = "Constant Contact delivery is off.";
  }

  const counts = Object.entries(labels).flatMap(([key, label]) =>
    receipt.states[key] ? [label + ": " + receipt.states[key]] : [],
  );

  return counts.length ? `${status} ${counts.join(" ")}` : status;
}

export function setupLibraryConstantContact(
  panel: HTMLElement,
  credential: () => string,
  isCurrent: (endpoint: string) => boolean,
) {
  const control = <T extends HTMLElement>(suffix: string) =>
    panel.querySelector<T>("#registrationConstantContact" + suffix)!;
  const load = control<HTMLButtonElement>("Load");
  const more = control<HTMLButtonElement>("More");
  const picker = control<HTMLSelectElement>("List");
  const save = control<HTMLButtonElement>("Save");
  const reload = control<HTMLButtonElement>("Refresh");
  const stop = control<HTMLButtonElement>("Stop");
  const status = control<HTMLElement>("Status");
  let generation = 0;

  return async (endpoint: string, settings: Settings) => {
    const scope = ++generation;
    const active = () => generation === scope && isCurrent(endpoint);
    const ready = settings.enabled && settings.first_name !== "off" && settings.last_name !== "off";
    const lists = new Map<string, string>();
    let receipt: Receipt | undefined;
    let nextCursor: string | null = null;
    let busy = false;

    function drawControls() {
      const disabled = busy || !receipt?.available || !ready;
      load.disabled = disabled;
      more.hidden = nextCursor === null;
      more.disabled = disabled;
      picker.disabled = disabled || picker.options.length <= 1;
      save.disabled = disabled || !picker.value;
      reload.disabled = busy;
      stop.hidden = !receipt || receipt.state === "off";
      stop.disabled = busy;
    }

    function drawLists(selected: string) {
      const options = new Map(lists);
      if (receipt?.list_id && receipt.list_name && !options.has(receipt.list_id)) {
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
        if (active() && receipt) status.textContent = receiptStatus(receipt, ready);
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

    panel.hidden = true;
    status.textContent = "";
    drawLists("");
    picker.onchange = drawControls;
    load.onclick = () => void run(() => loadLists());
    more.onclick = () => {
      const cursor = nextCursor;
      if (cursor) void run(() => loadLists(cursor));
    };
    save.onclick = () => {
      const listId = picker.value;
      if (listId) void run(() => readReceipt("PUT", {list_id: listId}));
    };
    reload.onclick = () => void run(() => readReceipt());
    stop.onclick = () => void run(() => readReceipt("DELETE", {}));
    await run(() => readReceipt());
  };
}
