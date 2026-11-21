type Request = <T>(path: string, method?: string, body?: object, integration?: boolean) => Promise<T>;

type MarketoReceipt = {
  available: boolean;
  enabled: boolean;
  state: string;
  program?: number;
  mapping?: Record<string, string>;
  states: Record<string, number>;
  engagement?: boolean;
  engagement_states?: Record<string, number>;
};

type ProgramPage = { programs: { id: number; name: string }[]; next_offset: number | null };
type LeadField = { name: string; label: string; type: string; length: number | null };
type FormField = { id: string; label: string; type: string };

export function setupMarketo(request: Request) {
  const eventMarketo = document.querySelector<HTMLFieldSetElement>('#eventMarketo')!;
  const eventMarketoLoad = document.querySelector<HTMLButtonElement>('#eventMarketoLoad')!;
  const eventMarketoMore = document.querySelector<HTMLButtonElement>('#eventMarketoMore')!;
  const eventMarketoProgram = document.querySelector<HTMLSelectElement>('#eventMarketoProgram')!;
  const eventMarketoMapping = document.querySelector<HTMLDivElement>('#eventMarketoMapping')!;
  const eventMarketoEngagement = document.querySelector<HTMLInputElement>('#eventMarketoEngagement')!;
  const eventMarketoEnable = document.querySelector<HTMLButtonElement>('#eventMarketoEnable')!;
  const eventMarketoStop = document.querySelector<HTMLButtonElement>('#eventMarketoStop')!;
  const eventMarketoRefresh = document.querySelector<HTMLButtonElement>('#eventMarketoRefresh')!;
  const eventMarketoStatus = document.querySelector<HTMLParagraphElement>('#eventMarketoStatus')!;

  let base = '';
  let eventId = '';
  let generation = 0;
  let offset: number | null = null;
  let receipt: MarketoReceipt | undefined;
  let loaded = false;

  const endpoint = () => base + '/registration/marketo/outbound';

  async function loadPrograms(reset: boolean) {
    const ticket = generation;
    const off = reset ? 0 : offset;
    if (off === null) return;
    const page = await request<ProgramPage>(`/api/integrations/marketo/programs?offset=${off}`, undefined, undefined, true);
    if (ticket !== generation) return;
    const currentProgram = eventMarketoProgram.value;
    if (reset) eventMarketoProgram.replaceChildren(new Option('Choose a program', ''));
    const existing = new Set(Array.from(eventMarketoProgram.options).map(o => o.value));
    for (const p of page.programs) {
      const v = String(p.id);
      if (!existing.has(v)) {
        eventMarketoProgram.add(new Option(p.name, v));
        existing.add(v);
      }
    }
    if (receipt?.program !== undefined) {
      const v = String(receipt.program);
      if (!existing.has(v)) {
        eventMarketoProgram.add(new Option(v, v));
        existing.add(v);
      }
      if (reset || !currentProgram) eventMarketoProgram.value = v;
    }
    offset = page.next_offset;
    eventMarketoMore.hidden = offset === null;
  }

  async function loadFields() {
    const ticket = generation;
    const [leadFieldsRes, registrationRes] = await Promise.all([
      request<{ fields: LeadField[] }>('/api/integrations/marketo/lead-fields', undefined, undefined, true),
      request<{ settings: { enabled: boolean; fields: FormField[] } }>(`/api/live-events/${eventId}/registration`)
    ]);
    if (ticket !== generation) return;
    eventMarketoMapping.replaceChildren();
    const leadFields = leadFieldsRes.fields;
    for (const formField of registrationRes.settings.fields) {
      if (formField.type !== 'checkbox' && formField.type !== 'text' && formField.type !== 'select') continue;
      const label = document.createElement('label');
      label.textContent = formField.label;
      const select = document.createElement('select');
      select.dataset.formField = formField.id;
      select.add(new Option('Do not send', ''));
      const allowed = formField.type === 'checkbox'
        ? leadFields.filter(leadField => leadField.type === 'boolean')
        : (formField.type === 'text' || formField.type === 'select')
          ? leadFields.filter(leadField => leadField.type !== 'boolean')
          : [];
      for (const leadField of allowed) select.add(new Option(leadField.label, leadField.name));
      if (receipt?.mapping?.[formField.id]) select.value = receipt.mapping[formField.id];
      label.append(select);
      eventMarketoMapping.append(label);
    }
    loaded = registrationRes.settings.enabled;
    eventMarketoEnable.disabled = !loaded || !eventMarketoProgram.value;
  }

  let inFlight: number | null = null;

  function renderReceipt(result: MarketoReceipt) {
    receipt = result;
    eventMarketoEngagement.checked = result.engagement === true;
    eventMarketo.hidden = !result.available;
    eventMarketoStop.hidden = !result.enabled;
    const parts: string[] = [];
    if (result.enabled) {
      parts.push('Marketo delivery enabled.');
      for (const [state, count] of Object.entries(result.states)) parts.push(`${state}: ${count}`);
      if (result.engagement && result.engagement_states) {
        const engagement = Object.entries(result.engagement_states).map(([state, count]) => `${state}: ${count}`);
        if (engagement.length) parts.push(`Engagement: ${engagement.join(', ')}`);
      }
    } else {
      parts.push(result.state === 'stale' ? 'Reconnect Marketo and select a program again.' : 'Marketo delivery is off.');
    }
    eventMarketoStatus.textContent = parts.join(' ');
  }

  async function run(operation: () => Promise<void>) {
    if (inFlight !== null) return;
    const current = inFlight = ++generation;
    eventMarketoEnable.disabled = true;
    try {
      await operation();
    } catch (error) {
      if (current === generation) eventMarketoStatus.textContent = String(error instanceof Error ? error.message : error);
    } finally {
      if (current === generation) { inFlight = null; eventMarketoEnable.disabled = !loaded || !eventMarketoProgram.value; }
    }
  }

  eventMarketoLoad.onclick = () => void run(async () => { await Promise.all([loadPrograms(true), loadFields()]); });
  eventMarketoMore.onclick = () => void run(() => loadPrograms(false));
  eventMarketoProgram.onchange = () => {
    eventMarketoEnable.disabled = inFlight !== null || !loaded || !eventMarketoProgram.value;
  };
  eventMarketoRefresh.onclick = () => void run(async () => {
    const ticket = generation;
    const result = await request<MarketoReceipt>(endpoint(), 'GET');
    if (ticket === generation) renderReceipt(result);
  });

  eventMarketoEnable.onclick = () => void run(async () => {
    const ticket = generation;
    if (!loaded) return;
    const program = Number(eventMarketoProgram.value);
    if (!Number.isSafeInteger(program) || program <= 0) return;
    const mapping: Record<string, string> = {};
    eventMarketoMapping.querySelectorAll<HTMLSelectElement>('select[data-form-field]').forEach(select => {
      const value = select.value.trim(), field = select.dataset.formField;
      if (value && field) mapping[field] = value;
    });
    const result = await request<MarketoReceipt>(endpoint(), 'PUT', { program, mapping, engagement: eventMarketoEngagement.checked }, true);
    if (ticket === generation) renderReceipt(result);
  });
  eventMarketoStop.onclick = () => void run(async () => {
    const ticket = generation;
    const result = await request<MarketoReceipt>(endpoint(), 'DELETE', {}, true);
    if (ticket === generation) renderReceipt(result);
  });
  return (selectedBase: string, selectedEvent: string) => {
    generation++;
    base = selectedBase;
    eventId = selectedEvent;
    inFlight = null;
    receipt = undefined;
    loaded = false;
    eventMarketoEngagement.checked = false;
    offset = null;
    eventMarketoStatus.textContent = '';
    eventMarketoMapping.replaceChildren();
    eventMarketoProgram.replaceChildren();
    eventMarketo.hidden = true;
    eventMarketoStop.hidden = true;
    eventMarketoMore.hidden = true;
    eventMarketoEnable.disabled = true;
    eventMarketoRefresh.click();
  };
}
