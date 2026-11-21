function formFields(form) {
    const out = [];
    if (form.enabled) {
        if (form.first_name === 'optional' || form.first_name === 'required') {
            out.push({ id: 'first_name', label: 'First name', type: 'text', required: form.first_name === 'required' });
        }
        if (form.last_name === 'optional' || form.last_name === 'required') {
            out.push({ id: 'last_name', label: 'Last name', type: 'text', required: form.last_name === 'required' });
        }
    }
    for (const f of form.fields ?? []) {
        if (f.id && (f.type === 'text' || f.type === 'select'))
            out.push(f);
    }
    return out;
}
function compatible(source, target) {
    if (target.required && !source.required)
        return false;
    const t = target.type.toLowerCase();
    if (t === 'text')
        return source.type === 'text' || source.type === 'select';
    if (t === 'radio' || t === 'dropdown') {
        if (source.type !== 'select' || !source.options)
            return false;
        return source.options.every(c => target.choices.includes(c));
    }
    if (t === 'number' || t === 'zip' || t === 'phone' || t === 'url') {
        return source.type === 'text';
    }
    return null;
}
function renderMapping(container, sources, targets, preferred) {
    container.replaceChildren();
    for (const source of sources) {
        const row = document.createElement('div');
        const label = document.createElement('label');
        label.textContent = source.label + (source.required ? ' (required)' : '');
        const select = document.createElement('select');
        select.dataset.formField = source.id;
        select.append(new Option('Do not send', ''));
        for (const target of targets) {
            if (!compatible(source, target))
                continue;
            select.append(new Option(target.name + (target.required ? ' (required)' : ''), target.tag));
        }
        const want = preferred[source.id];
        if (want && Array.from(select.options).some(o => o.value === want))
            select.value = want;
        label.append(select);
        row.append(label);
        container.append(row);
    }
}
function readMapping(container) {
    return Object.fromEntries(Array.from(container.querySelectorAll('select[data-form-field]'))
        .filter(el => el.value)
        .map(el => [el.dataset.formField, el.value]));
}
async function loadFieldPages(request, audience) {
    let offset = 0, marketingPermissions = false;
    const fields = [], seen = new Set();
    for (let i = 0; i < 20; i++) {
        const page = await request('/api/integrations/mailchimp/audiences/' + encodeURIComponent(audience) + '/fields?offset=' + offset, undefined, undefined, true);
        if (page.audience?.id !== audience)
            throw new Error(`Audience mismatch: expected ${audience}.`);
        marketingPermissions ||= page.audience.marketing_permissions;
        for (const field of page.fields) {
            if (seen.has(field.tag))
                throw new Error(`Duplicate merge field tag: ${field.tag}.`);
            seen.add(field.tag);
            fields.push(field);
        }
        if (page.next_offset === null)
            return { fields, marketingPermissions };
        if (!Number.isSafeInteger(page.next_offset) || page.next_offset <= offset)
            throw new Error('Invalid pagination offset.');
        offset = page.next_offset;
    }
    throw new Error('Field page limit exceeded.');
}
export function setupMailchimp(request) {
    const panel = document.querySelector('#eventMailchimp');
    const load = document.querySelector('#eventMailchimpLoad');
    const more = document.querySelector('#eventMailchimpMore');
    const audience = document.querySelector('#eventMailchimpAudience');
    const mapping = document.querySelector('#eventMailchimpMapping');
    const mappingStatus = document.querySelector('#eventMailchimpMappingStatus');
    const save = document.querySelector('#eventMailchimpEnable');
    const engagement = document.querySelector('#eventMailchimpEngagement');
    const stop = document.querySelector('#eventMailchimpStop');
    const refresh = document.querySelector('#eventMailchimpRefresh');
    const status = document.querySelector('#eventMailchimpStatus');
    let selected;
    const endpoint = (scope) => scope.base + '/registration/mailchimp/outbound';
    function updateControls(scope) {
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
    function mappingIssue(scope) {
        if (!audience.value || scope.fields === undefined)
            return null;
        if (!scope.formEnabled)
            return 'Enable the form to save mappings.';
        if (scope.marketingPermissions) {
            return 'This audience requires marketing permission mapping.';
        }
        const fields = scope.fields;
        const targets = Object.values(readMapping(mapping))
            .map((value) => value.trim())
            .filter(Boolean);
        if (targets.length > 20)
            return 'Use at most 20 mappings.';
        if (new Set(targets).size !== targets.length)
            return 'Remove duplicate targets.';
        const missing = fields.find((field) => field.required && !targets.includes(field.tag));
        if (missing)
            return `Map required field: ${missing.name}.`;
        return null;
    }
    function syncMapping(scope) {
        updateControls(scope);
        mappingStatus.textContent = mappingIssue(scope) ?? '';
    }
    function showReceipt(scope, receipt) {
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
                if (engagementText)
                    counts.push(engagementText);
            }
            status.textContent = ['Mailchimp delivery enabled.', ...counts].join(' ');
        }
        else if (receipt.state === 'stale') {
            status.textContent = 'Mailchimp connection is stale. Reconnect to continue.';
        }
        else {
            status.textContent = 'Mailchimp delivery is off.';
        }
    }
    async function run(scope, operation) {
        if (selected !== scope || scope.busy)
            return;
        scope.busy = true;
        updateControls(scope);
        try {
            await operation();
        }
        catch {
            if (selected === scope)
                status.textContent = 'Mailchimp request failed. Try again.';
        }
        finally {
            if (selected === scope) {
                scope.busy = false;
                syncMapping(scope);
            }
        }
    }
    async function loadAudienceFields(scope) {
        mapping.replaceChildren();
        mappingStatus.textContent = '';
        scope.fields = undefined;
        scope.attendanceAnalytics = false;
        const id = audience.value;
        if (!id)
            return;
        const [schema, reg] = await Promise.all([
            loadFieldPages(request, id),
            request(`/api/live-events/${scope.eventId}/registration`),
        ]);
        if (selected !== scope)
            return;
        const saved = scope.receipt?.audience === id ? scope.receipt : undefined;
        scope.fields = schema.fields;
        scope.marketingPermissions = schema.marketingPermissions;
        scope.formEnabled = reg.settings.enabled;
        scope.attendanceAnalytics = reg.settings.attendance_analytics === true;
        renderMapping(mapping, formFields(reg.settings), schema.fields, saved?.mapping ?? {});
    }
    async function loadAudiencePage(scope, reset) {
        const offset = reset ? 0 : scope.nextOffset;
        if (offset === null)
            return;
        const page = await request(`/api/integrations/mailchimp/audiences?offset=${offset}`, undefined, undefined, true);
        if (selected !== scope)
            return;
        scope.nextOffset = page.next_offset;
        if (reset) {
            audience.replaceChildren(new Option('Choose an audience', ''));
        }
        const options = Array.from(audience.options);
        for (const a of page.audiences) {
            const option = options.find(o => o.value === a.id);
            if (option) {
                option.text = a.name;
            }
            else {
                audience.appendChild(new Option(a.name, a.id));
            }
        }
        if (!reset)
            return;
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
    const mutate = (method, body) => {
        const scope = selected;
        if (!scope)
            return;
        return run(scope, async () => {
            const result = await request(endpoint(scope), method, body, true);
            if (selected !== scope)
                return;
            showReceipt(scope, result);
        });
    };
    load.addEventListener('click', () => {
        const scope = selected;
        if (scope)
            run(scope, () => loadAudiencePage(scope, true));
    });
    more.addEventListener('click', () => {
        const scope = selected;
        if (scope)
            run(scope, () => loadAudiencePage(scope, false));
    });
    audience.addEventListener('change', () => {
        const scope = selected;
        if (scope)
            run(scope, () => loadAudienceFields(scope));
    });
    mapping.addEventListener('change', () => {
        if (selected)
            syncMapping(selected);
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
    return (base, eventId) => {
        const scope = {
            base, eventId, busy: false, nextOffset: null,
            formEnabled: false, marketingPermissions: false, attendanceAnalytics: false,
        };
        selected = scope;
        engagement.checked = false;
        panel.hidden = true;
        status.textContent = '';
        mappingStatus.textContent = '';
        audience.replaceChildren(new Option('Choose an audience', ''));
        mapping.replaceChildren();
        return mutate('GET');
    };
}
