const label = (s) => s === 'waiting' ? 'Waiting for publication approval'
    : s === 'eligible' ? 'Access granted' : s;
export async function groupPlayback(container, read, save) {
    const tpl = document.querySelector('#groupPlaybackSettings');
    if (!tpl)
        throw new Error('Missing #groupPlaybackSettings template');
    const root = tpl.content.firstElementChild.cloneNode(true);
    root.hidden = true;
    container.append(root);
    const status = root.querySelector('[role="status"]');
    const form = root.querySelector('form');
    const slug = form.querySelector('input[name="group"]');
    const publicGroup = form.querySelector('input[name="public_group"]');
    const add = form.querySelector('button[type="submit"]');
    const list = root.querySelector('ul');
    let state = null;
    const say = (m) => { status.textContent = m; };
    const fail = (w, e) => say(`${w}: ${e instanceof Error ? e.message : String(e)}`);
    const render = (s) => {
        state = s;
        root.hidden = !s.can_grant && s.grants.length === 0;
        add.disabled = slug.disabled = !s.can_grant;
        list.replaceChildren();
        for (const g of s.grants) {
            const li = document.createElement('li');
            const name = document.createElement('span');
            name.textContent = g.slug;
            const badge = document.createElement('span');
            badge.textContent = label(g.state);
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.textContent = `Remove ${g.slug}`;
            remove.addEventListener('click', async () => {
                remove.disabled = true;
                try {
                    render(await save({ group: g.slug, allowed: false,
                        privacy_revision: s.privacy_revision, grant_revision: g.revision }));
                    say('');
                }
                catch (e) {
                    remove.disabled = false;
                    fail(`Could not remove ${g.slug}`, e);
                }
            });
            li.append(name, badge, remove);
            list.append(li);
        }
    };
    const refresh = async () => {
        try {
            render(await read());
            say('');
        }
        catch (e) {
            fail('Could not load access', e);
        }
    };
    form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        add.disabled = true;
        try {
            const group = slug.value.trim();
            if (!state?.can_grant)
                return;
            const current = state.grants.find(grant => grant.slug === group);
            render(await save({ group, allowed: true,
                privacy_revision: state.privacy_revision, grant_revision: current?.revision ?? null,
                public_group: publicGroup.checked }));
            slug.value = '';
            publicGroup.checked = false;
            say('');
        }
        catch (e) {
            fail('Could not add access', e);
            add.disabled = slug.disabled = !state?.can_grant;
        }
    });
    await refresh();
    return refresh;
}
