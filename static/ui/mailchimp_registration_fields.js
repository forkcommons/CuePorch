export function formFields(form) {
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
export function renderMapping(container, sources, targets, preferred) {
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
export function readMapping(container) {
    return Object.fromEntries(Array.from(container.querySelectorAll('select[data-form-field]'))
        .filter(el => el.value)
        .map(el => [el.dataset.formField, el.value]));
}
export async function loadFieldPages(request, audience) {
    let offset = 0, marketingPermissions = false, audienceName = '';
    const fields = [], seen = new Set();
    for (let i = 0; i < 20; i++) {
        const page = await request('/api/integrations/mailchimp/audiences/' + encodeURIComponent(audience) + '/fields?offset=' + offset, undefined, undefined, true);
        if (page.audience?.id !== audience)
            throw new Error(`Audience mismatch: expected ${audience}.`);
        audienceName = page.audience.name;
        marketingPermissions ||= page.audience.marketing_permissions;
        for (const field of page.fields) {
            if (seen.has(field.tag))
                throw new Error(`Duplicate merge field tag: ${field.tag}.`);
            seen.add(field.tag);
            fields.push(field);
        }
        if (page.next_offset === null)
            return { fields, marketingPermissions, audienceName };
        if (!Number.isSafeInteger(page.next_offset) || page.next_offset <= offset)
            throw new Error('Invalid pagination offset.');
        offset = page.next_offset;
    }
    throw new Error('Field page limit exceeded.');
}
