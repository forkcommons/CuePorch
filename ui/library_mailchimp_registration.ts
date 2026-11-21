import { libraryMailchimpDestination } from "./library_mailchimp_destination.js";
import type { AudiencePage, FormSettings, MailchimpRequest } from "./mailchimp_registration_fields.js";
import { checked } from "./team_api.js";

type Destination = Awaited<ReturnType<typeof libraryMailchimpDestination>>;
type Receipt = {
  available: boolean;
  enabled: boolean;
  state: string;
  destinations: {audience: string; mapping: Record<string, string>; states: Record<string, number>}[];
};
type Scope = {
  endpoint: string;
  form: FormSettings;
  busy: boolean;
  state: string;
  audiences: Map<string, string>;
  rows: Map<string, Destination>;
  nextOffset: number | null;
};

export function setupLibraryMailchimp(panel: HTMLElement, credential: () => string) {
  const control = <T extends HTMLElement>(suffix: string) => panel.querySelector<T>("#registrationMailchimp" + suffix)!;
  const load = control<HTMLButtonElement>("Load");
  const more = control<HTMLButtonElement>("More");
  const add = control<HTMLButtonElement>("Add");
  const save = control<HTMLButtonElement>("Save");
  const reload = control<HTMLButtonElement>("Refresh");
  const stop = control<HTMLButtonElement>("Stop");
  const picker = control<HTMLSelectElement>("Audience");
  const destinations = control<HTMLDivElement>("Destinations");
  const status = control<HTMLParagraphElement>("Status");
  let current: Scope | undefined;

  const request: MailchimpRequest = async <T>(path: string, method = "GET", body?: object): Promise<T> => {
    const headers: Record<string, string> = {};
    const key = credential();
    if (key) headers.Authorization = "Bearer " + key;
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-CuePorch-Integration"] = "1";
    }
    return (await checked(await fetch(path, {method, headers, body: body && JSON.stringify(body)}))).json();
  };

  function draw(scope: Scope) {
    const selected = picker.value;
    const options = Array.from(scope.audiences)
      .filter(([id]) => !scope.rows.has(id))
      .map(([id, name]) => new Option(name, id));
    picker.replaceChildren(new Option("Choose an audience", ""), ...options);
    if (options.some(option => option.value === selected)) picker.value = selected;
    load.disabled = scope.busy || !scope.form.enabled;
    more.hidden = scope.nextOffset === null;
    more.disabled = load.disabled;
    picker.disabled = load.disabled || !options.length;
    add.disabled = picker.disabled || !picker.value;
    save.disabled = load.disabled || !scope.rows.size;
    reload.disabled = scope.busy;
    stop.hidden = scope.state === "off";
    stop.disabled = scope.busy;
    for (const row of scope.rows.values()) row.element.disabled = load.disabled;
  }

  async function run(scope: Scope, action: () => Promise<void>) {
    if (current !== scope || scope.busy) return;
    scope.busy = true;
    status.textContent = "";
    draw(scope);
    try {
      await action();
    } catch (error) {
      if (current === scope) {
        panel.hidden = false;
        status.textContent = error instanceof Error ? error.message : String(error);
      }
    } finally {
      scope.busy = false;
      if (current === scope) draw(scope);
    }
  }

  async function addRow(scope: Scope, id: string, mapping: Record<string, string>, states: Record<string, number>) {
    const row = await libraryMailchimpDestination(request, scope.form, id, mapping, () => {
      if (current !== scope || scope.busy || scope.rows.get(id) !== row) return;
      row.element.remove();
      scope.rows.delete(id);
      draw(scope);
    });
    if (current !== scope) return;
    row.element.disabled = scope.busy;
    row.updateCounts(states);
    scope.rows.set(id, row);
    destinations.append(row.element);
  }

  async function applyReceipt(scope: Scope, receipt: Receipt, rebuild: boolean) {
    if (current !== scope) return;
    panel.hidden = !receipt.available;
    scope.state = receipt.state;
    if (rebuild || !receipt.enabled) {
      scope.rows.clear();
      destinations.replaceChildren();
    }
    if (!receipt.available) return;
    for (const dest of receipt.destinations) {
      if (current !== scope) return;
      const row = scope.rows.get(dest.audience);
      if (row) row.updateCounts(dest.states);
      else await addRow(scope, dest.audience, dest.mapping, dest.states);
    }
    if (current !== scope) return;
    status.textContent = !scope.form.enabled
      ? "Save an enabled registration form to configure Mailchimp delivery."
      : receipt.enabled ? "Mailchimp delivery is enabled."
      : receipt.state === "stale" ? "Mailchimp delivery needs to be configured again."
      : "Mailchimp delivery is off.";
  }

  async function discover(scope: Scope, offset: number) {
    const page = await request<AudiencePage>("/api/integrations/mailchimp/audiences?offset=" + offset);
    if (current !== scope) return;
    if (page.next_offset !== null && (!Number.isSafeInteger(page.next_offset) || page.next_offset <= offset)) {
      throw new Error("Mailchimp returned an invalid audience page.");
    }
    if (offset === 0) scope.audiences.clear();
    for (const item of page.audiences) scope.audiences.set(item.id, item.name);
    scope.nextOffset = page.next_offset;
  }

  picker.onchange = () => { if (current) add.disabled = picker.disabled || !picker.value; };
  load.onclick = () => { const scope = current; if (scope) void run(scope, () => discover(scope, 0)); };
  more.onclick = () => {
    const scope = current;
    const offset = scope?.nextOffset;
    if (scope && offset != null) void run(scope, () => discover(scope, offset));
  };
  add.onclick = () => {
    const scope = current;
    const id = picker.value;
    if (scope?.form.enabled && scope.audiences.has(id) && !scope.rows.has(id)) {
      void run(scope, () => addRow(scope, id, {}, {}));
    }
  };
  save.onclick = () => {
    const scope = current;
    if (!scope?.form.enabled || !scope.rows.size) return;
    void run(scope, async () => {
      const plan = Array.from(scope.rows.values(), row => row.payload());
      await applyReceipt(scope, await request<Receipt>(scope.endpoint, "PUT", {destinations: plan}), false);
    });
  };
  reload.onclick = () => {
    const scope = current;
    if (scope) void run(scope, async () => applyReceipt(scope, await request<Receipt>(scope.endpoint), true));
  };
  stop.onclick = () => {
    const scope = current;
    if (scope) void run(scope, async () => applyReceipt(scope, await request<Receipt>(scope.endpoint, "DELETE", {}), true));
  };

  return async (endpoint: string, form: FormSettings) => {
    const scope: Scope = {
      endpoint, form, busy: false, state: "off", audiences: new Map(), rows: new Map(), nextOffset: null,
    };
    current = scope;
    panel.hidden = true;
    destinations.replaceChildren();
    await run(scope, async () => applyReceipt(scope, await request<Receipt>(scope.endpoint), true));
  };
}
