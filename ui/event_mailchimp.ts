type Request = <T>(path: string, method?: string, body?: object, integration?: boolean) => Promise<T>;
type Audience = { id: string; name: string };
type AudiencePage = { audiences: Audience[]; next_offset: number | null };
type MergeField = { tag: string; name: string; type: string; required: boolean; choices: string[] };
type FieldPage = { audience: Audience & { marketing_permissions: boolean; double_optin: boolean }; fields: MergeField[]; next_offset: number | null };
type Receipt = { available: boolean; enabled: boolean; state: string; audience?: string; mapping?: Record<string, string>; states: Record<string, number> };
type FormField = { id: string; label: string; type: string; required: boolean; options?: string[] };
type FormSettings = { enabled: boolean; first_name: string; last_name: string; fields: FormField[] };

function formFields(form: FormSettings): FormField[] {
  const out: FormField[] = [];
  if (form.enabled) {
    if (form.first_name === 'optional' || form.first_name === 'required') {
      out.push({ id: 'first_name', label: 'First name', type: 'text', required: form.first_name === 'required' });
    }
    if (form.last_name === 'optional' || form.last_name === 'required') {
      out.push({ id: 'last_name', label: 'Last name', type: 'text', required: form.last_name === 'required' });
    }
  }
  for (const f of form.fields ?? []) {
    if (f.id && (f.type === 'text' || f.type === 'select')) out.push(f);
  }
  return out;
}

function compatible(source: FormField, target: MergeField): boolean | null {
  if (target.required && !source.required) return false;
  const t = target.type.toLowerCase();
  if (t === 'text') return source.type === 'text' || source.type === 'select';
  if (t === 'radio' || t === 'dropdown') {
    if (source.type !== 'select' || !source.options) return false;
    return source.options.every(c => target.choices.includes(c));
  }
  if (t === 'number' || t === 'zip' || t === 'phone' || t === 'url') {
    return source.type === 'text';
  }
  return null;
}

function renderMapping(container: HTMLDivElement, sources: FormField[], targets: MergeField[], preferred: Record<string, string>): void {
  container.replaceChildren();
  for (const source of sources) {
    const row = document.createElement('div');
    const label = document.createElement('label');
    label.textContent = source.label + (source.required ? ' (required)' : '');
    const select = document.createElement('select');
    select.dataset.formField = source.id;
    select.append(new Option('Do not send', ''));
    for (const target of targets) {
      if (!compatible(source, target)) continue;
      select.append(new Option(target.name + (target.required ? ' (required)' : ''), target.tag));
    }
    const want = preferred[source.id];
    if (want && Array.from(select.options).some(o => o.value === want)) select.value = want;
    label.append(select);
    row.append(label);
    container.append(row);
  }
}

function readMapping(container: HTMLDivElement): Record<string, string> {
  return Object.fromEntries(
    Array.from(container.querySelectorAll<HTMLSelectElement>('select[data-form-field]'))
      .filter(el => el.value)
      .map(el => [el.dataset.formField!, el.value]),
  );
}

async function loadFieldPages(request: Request, audience: string): Promise<{ fields: MergeField[]; marketingPermissions: boolean }> {
  let offset = 0, marketingPermissions = false;
  const fields: MergeField[] = [], seen = new Set<string>();
  for (let i = 0; i < 20; i++) {
    const page = await request<FieldPage>('/api/integrations/mailchimp/audiences/' + encodeURIComponent(audience) + '/fields?offset=' + offset, undefined, undefined, true);
    if (page.audience?.id !== audience) throw new Error(`Audience mismatch: expected ${audience}.`);
    marketingPermissions ||= page.audience.marketing_permissions;
    for (const field of page.fields) {
      if (seen.has(field.tag)) throw new Error(`Duplicate merge field tag: ${field.tag}.`);
      seen.add(field.tag);
      fields.push(field);
    }
    if (page.next_offset === null) return { fields, marketingPermissions };
    if (!Number.isSafeInteger(page.next_offset) || page.next_offset <= offset) throw new Error('Invalid pagination offset.');
    offset = page.next_offset;
  }
  throw new Error('Field page limit exceeded.');
}

export function setupMailchimp(request: Request) {
  const panel = document.querySelector<HTMLElement>('#eventMailchimp')!;
  const load = document.querySelector<HTMLButtonElement>('#eventMailchimpLoad')!;
  const more = document.querySelector<HTMLButtonElement>('#eventMailchimpMore')!;
  const audience = document.querySelector<HTMLSelectElement>('#eventMailchimpAudience')!;
  const mapping = document.querySelector<HTMLDivElement>('#eventMailchimpMapping')!;
  const mappingStatus = document.querySelector<HTMLElement>('#eventMailchimpMappingStatus')!;
  const save = document.querySelector<HTMLButtonElement>('#eventMailchimpEnable')!;
  const stop = document.querySelector<HTMLButtonElement>('#eventMailchimpStop')!;
  const refresh = document.querySelector<HTMLButtonElement>('#eventMailchimpRefresh')!;
  const status = document.querySelector<HTMLElement>('#eventMailchimpStatus')!;

  type Selection = {
    base: string;
    eventId: string;
    busy: boolean;
    nextOffset: number | null;
    receipt?: Receipt;
    fields?: MergeField[];
    formEnabled: boolean;
    marketingPermissions: boolean;
  };

  let selected: Selection | undefined;

  const endpoint = (scope: Selection) =>
    scope.base + '/registration/mailchimp/outbound';

  function updateControls(scope: Selection): void {
    load.disabled = scope.busy;
    more.disabled = scope.busy;
    audience.disabled = scope.busy;
    save.disabled =
      scope.busy ||
      !audience.value ||
      scope.fields === undefined ||
      mappingIssue(scope) !== null;
    stop.disabled = scope.busy;
    refresh.disabled = scope.busy;
    more.hidden = scope.nextOffset == null;
    stop.hidden = !scope.receipt?.enabled;
  }

  function mappingIssue(scope: Selection): string | null {
    if (!audience.value || scope.fields === undefined) return null;
    if (!scope.formEnabled) return 'Enable the form to save mappings.';
    if (scope.marketingPermissions) {
      return 'This audience requires marketing permission mapping.';
    }
    const fields = scope.fields;
    const targets = Object.values(readMapping(mapping))
      .map((value) => value.trim())
      .filter(Boolean);
    if (targets.length > 20) return 'Use at most 20 mappings.';
    if (new Set(targets).size !== targets.length) return 'Remove duplicate targets.';
    const missing = fields.find((field) => field.required && !targets.includes(field.tag));
    if (missing) return `Map required field: ${missing.name}.`;
    return null;
  }

  function syncMapping(scope: Selection): void {
    updateControls(scope);
    mappingStatus.textContent = mappingIssue(scope) ?? '';
  }

  function showReceipt(scope: Selection, receipt: Receipt): void {
    scope.receipt = receipt;
    panel.hidden = !receipt.available;
    if (receipt.enabled) {
      const counts = Object.entries(receipt.states)
        .filter(([, count]) => count > 0)
        .map(([state, count]) => `${state}: ${count}`);
      status.textContent = ['Mailchimp delivery enabled.', ...counts].join(' ');
    } else if (receipt.state === 'stale') {
      status.textContent = 'Mailchimp connection is stale. Reconnect to continue.';
    } else {
      status.textContent = 'Mailchimp delivery is off.';
    }
  }

  async function run(scope: Selection, operation: () => Promise<void>): Promise<void> {
    if (selected !== scope || scope.busy) return;
    scope.busy = true;
    updateControls(scope);
    try {
      await operation();
    } catch {
      if (selected === scope) status.textContent = 'Mailchimp request failed. Try again.';
    } finally {
      if (selected === scope) {
        scope.busy = false;
        syncMapping(scope);
      }
    }
  }

  async function loadAudienceFields(scope: Selection): Promise<void> {
    mapping.replaceChildren();
    mappingStatus.textContent = '';
    scope.fields = undefined;
    const id = audience.value;
    if (!id) return;
    const [schema, reg] = await Promise.all([
      loadFieldPages(request, id),
      request<{ settings: FormSettings }>(`/api/live-events/${scope.eventId}/registration`),
    ]);
    if (selected !== scope) return;
    const saved = scope.receipt?.audience === id ? scope.receipt : undefined;
    scope.fields = schema.fields;
    scope.marketingPermissions = schema.marketingPermissions;
    scope.formEnabled = reg.settings.enabled;
    renderMapping(mapping, formFields(reg.settings), schema.fields, saved?.mapping ?? {});
  }

  async function loadAudiencePage(scope: Selection, reset: boolean): Promise<void> {
    const offset = reset ? 0 : scope.nextOffset;
    if (offset === null) return;
    const page = await request<AudiencePage>(
      `/api/integrations/mailchimp/audiences?offset=${offset}`,
      undefined,
      undefined,
      true,
    );
    if (selected !== scope) return;
    scope.nextOffset = page.next_offset;
    if (reset) {
      audience.replaceChildren(new Option('Choose an audience',''));
    }
    const options = Array.from(audience.options);
    for (const a of page.audiences) {
      const option = options.find(o => o.value === a.id);
      if (option) {
        option.text = a.name;
      } else {
        audience.appendChild(new Option(a.name, a.id));
      }
    }
    if (!reset) return;
    const savedId = scope.receipt?.audience;
    if (savedId) {
      const option = Array.from(audience.options).find(o => o.value === savedId);
      if (!option) {
        audience.appendChild(new Option(savedId, savedId));
      }
      audience.value = savedId;
    }
    await loadAudienceFields(scope);
  }

  const mutate = (method: string, body?: object) => {
    const scope = selected;
    if (!scope) return;
    return run(scope, async () => {
      const result = await request<Receipt>(endpoint(scope), method, body, true);
      if (selected !== scope) return;
      showReceipt(scope, result);
    });
  };

  load.addEventListener('click', () => {
    const scope = selected;
    if (scope) run(scope, () => loadAudiencePage(scope, true));
  });
  more.addEventListener('click', () => {
    const scope = selected;
    if (scope) run(scope, () => loadAudiencePage(scope, false));
  });
  audience.addEventListener('change', () => {
    const scope = selected;
    if (scope) run(scope, () => loadAudienceFields(scope));
  });
  mapping.addEventListener('change', () => {
    if (selected) syncMapping(selected);
  });
  refresh.addEventListener('click', () => mutate('GET'));
  save.addEventListener('click', () =>
    mutate('PUT', { audience: audience.value, mapping: readMapping(mapping) }),
  );
  stop.addEventListener('click', () => mutate('DELETE', {}));

  return (base: string, eventId: string) => {
    const scope: Selection = {
      base, eventId, busy: false, nextOffset: null,
      formEnabled: false, marketingPermissions: false,
    };
    selected = scope;
    panel.hidden = true;
    status.textContent = '';
    mappingStatus.textContent = '';
    audience.replaceChildren(new Option('Choose an audience',''));
    mapping.replaceChildren();
    return mutate('GET');
  };
}
