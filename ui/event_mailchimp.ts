import { formFields, loadFieldPages, readMapping, renderMapping } from './mailchimp_registration_fields.js';
import type { AudiencePage, FormSettings, MailchimpRequest, MergeField } from './mailchimp_registration_fields.js';

type Receipt = { available: boolean; enabled: boolean; state: string; audience?: string; mapping?: Record<string, string>; states: Record<string, number>; engagement?: boolean; engagement_states?: Record<string, number> };

export function setupMailchimp(request: MailchimpRequest) {
  const panel = document.querySelector<HTMLElement>('#eventMailchimp')!;
  const load = document.querySelector<HTMLButtonElement>('#eventMailchimpLoad')!;
  const more = document.querySelector<HTMLButtonElement>('#eventMailchimpMore')!;
  const audience = document.querySelector<HTMLSelectElement>('#eventMailchimpAudience')!;
  const mapping = document.querySelector<HTMLDivElement>('#eventMailchimpMapping')!;
  const mappingStatus = document.querySelector<HTMLElement>('#eventMailchimpMappingStatus')!;
  const save = document.querySelector<HTMLButtonElement>('#eventMailchimpEnable')!;
  const engagement = document.querySelector<HTMLInputElement>('#eventMailchimpEngagement')!;
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
    attendanceAnalytics: boolean;
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
    engagement.disabled = scope.busy || !scope.attendanceAnalytics;
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
    engagement.checked = receipt.engagement === true;
    if (receipt.enabled) {
      const counts = Object.entries(receipt.states)
        .filter(([, count]) => count > 0)
        .map(([state, count]) => `${state}: ${count}`);
      if (receipt.engagement === true && receipt.engagement_states) {
        const engagementCounts = Object.entries(receipt.engagement_states).filter(([, count]) => count !== 0).map(([state, count]) => `${state}: ${count}`).join(", ");
        const engagementText = engagementCounts ? `Engagement ${engagementCounts}` : "";
        if (engagementText) counts.push(engagementText);
      }
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
    scope.attendanceAnalytics = false;
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
    scope.attendanceAnalytics = reg.settings.attendance_analytics === true;
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
  save.addEventListener('click', () => {
    const scope = selected;
    if (scope) {
      mutate('PUT', {
        audience: audience.value,
        mapping: readMapping(mapping),
        engagement: scope.attendanceAnalytics === true && engagement.checked,
      });
    }
  });
  stop.addEventListener('click', () => mutate('DELETE', {}));

  return (base: string, eventId: string) => {
    const scope: Selection = {
      base, eventId, busy: false, nextOffset: null,
      formEnabled: false, marketingPermissions: false, attendanceAnalytics: false,
    };
    selected = scope;
    engagement.checked = false;
    panel.hidden = true;
    status.textContent = '';
    mappingStatus.textContent = '';
    audience.replaceChildren(new Option('Choose an audience',''));
    mapping.replaceChildren();
    return mutate('GET');
  };
}
