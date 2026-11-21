export async function editFolderReviews(folder, opts) {
    const base = `/api/library/folders/${encodeURIComponent(folder.id)}/review-links`;
    const root = opts.openDialog(`Folder reviews — ${folder.name}`);
    const error = document.createElement('p');
    error.setAttribute('role', 'alert');
    const list = document.createElement('ul');
    const note = document.createElement('p');
    note.textContent = 'Videos in this folder and its subfolders are included. Videos you move into the folder later are also included.';
    const mk = (tag, init = {}) => Object.assign(document.createElement(tag), init);
    const field = (text, input) => {
        const label = document.createElement('label');
        label.append(text, ' ', input);
        return label;
    };
    const name = document.createElement('input');
    name.type = 'text';
    name.required = true;
    name.maxLength = 128;
    name.value = folder.name;
    const password = document.createElement('input');
    password.type = 'password';
    password.maxLength = 256;
    password.autocomplete = 'new-password';
    const downloads = document.createElement('input');
    downloads.type = 'checkbox';
    const previous = document.createElement('input');
    previous.type = 'checkbox';
    const expiry = document.createElement('input');
    expiry.type = 'datetime-local';
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.textContent = 'Create folder review link';
    const form = document.createElement('form');
    form.setAttribute('aria-label', 'Create folder review link');
    form.append(field('Folder review name', name), field('Review password (optional, leave blank to reuse defaults)', password), field('Allow downloads', downloads), field('Allow previous versions', previous), field('Expiration date and time (optional, local time)', expiry), submit);
    const fail = (e) => {
        const message = e instanceof Error ? e.message : String(e);
        error.textContent = message;
        opts.status.textContent = message;
    };
    let refresh;
    const render = (link) => {
        const item = document.createElement('li');
        const url = new URL(link.path, opts.publicBase).toString();
        const btn = (text, run) => {
            const button = mk('button', { type: 'button', textContent: text });
            button.addEventListener('click', async () => {
                button.disabled = true;
                try {
                    await run();
                }
                finally {
                    button.disabled = false;
                }
            });
            return button;
        };
        const copy = btn('Copy', async () => {
            try {
                await navigator.clipboard.writeText(url);
                opts.status.textContent = `Copied ${link.name}.`;
            }
            catch (e) {
                fail(e);
            }
        });
        copy.setAttribute('aria-label', `Copy ${link.name}`);
        const toggle = btn(link.enabled ? 'Disable' : 'Enable', async () => {
            try {
                await opts.checked(await opts.request(`${base}/${encodeURIComponent(link.id)}`, 'PUT', { enabled: !link.enabled }));
                await refresh();
            }
            catch (e) {
                fail(e);
            }
        });
        toggle.setAttribute('aria-label', `${link.enabled ? 'Disable' : 'Enable'} ${link.name}`);
        item.append(mk('a', { href: url, textContent: link.name }), ' ', copy, ' ', toggle);
        return item;
    };
    refresh = async () => {
        const res = await opts.checked(await opts.request(base));
        const links = (await res.json());
        list.textContent = '';
        for (const link of links)
            list.append(render(link));
    };
    form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        error.textContent = '';
        opts.status.textContent = '';
        submit.disabled = true;
        try {
            const payload = {
                name: name.value.trim(),
                allow_download: downloads.checked,
                allow_versions: previous.checked,
            };
            if (password.value)
                payload.password = password.value;
            if (expiry.value)
                payload.expires = new Date(expiry.value).getTime() / 1000;
            await opts.checked(await opts.request(base, 'POST', payload));
            password.value = '';
            await refresh();
            opts.status.textContent = 'Folder review link created.';
        }
        catch (e) {
            fail(e);
        }
        finally {
            submit.disabled = false;
        }
    });
    root.append(error, note, list, form);
    try {
        await refresh();
    }
    catch (e) {
        fail(e);
    }
}
