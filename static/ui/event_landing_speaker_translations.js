export function renderSpeakerTranslations(container, profiles, entry, ensure) {
    container.textContent = '';
    for (const profile of profiles) {
        const saved = entry?.speakers?.[profile.id];
        const fieldset = document.createElement('fieldset');
        const legend = document.createElement('legend');
        legend.textContent = profile.name;
        fieldset.append(legend);
        const roleLabel = document.createElement('label');
        roleLabel.textContent = `Role (base: ${profile.role})`;
        const roleInput = document.createElement('input');
        roleInput.type = 'text';
        roleInput.maxLength = 160;
        roleInput.value = saved?.role ?? '';
        roleLabel.append(roleInput);
        const bioLabel = document.createElement('label');
        bioLabel.textContent = `Bio (base: ${profile.bio})`;
        const bioInput = document.createElement('textarea');
        bioInput.maxLength = 3000;
        bioInput.value = saved?.bio ?? '';
        bioLabel.append(bioInput);
        const status = document.createElement('span');
        const markBtn = document.createElement('button');
        markBtn.type = 'button';
        markBtn.textContent = 'Mark current';
        let current = saved;
        const refresh = () => {
            const stale = !!current && (current.base_role !== profile.role || current.base_bio !== profile.bio);
            status.textContent = stale ? 'Translation needs updating' : '';
            markBtn.hidden = !stale;
        };
        const onInput = () => {
            const role = roleInput.value.trim();
            const bio = bioInput.value.trim();
            if (!current && !role && !bio)
                return;
            const speakers = (ensure().speakers ??= {});
            if (!role && !bio) {
                delete speakers[profile.id];
                current = undefined;
            }
            else {
                current = {
                    role, bio,
                    base_role: current?.base_role ?? profile.role,
                    base_bio: current?.base_bio ?? profile.bio,
                };
                speakers[profile.id] = current;
            }
            refresh();
        };
        markBtn.addEventListener('click', () => {
            if (!current)
                return;
            current.base_role = profile.role;
            current.base_bio = profile.bio;
            refresh();
        });
        roleInput.addEventListener('input', onInput);
        bioInput.addEventListener('input', onInput);
        fieldset.append(roleLabel, bioLabel, markBtn, status);
        container.append(fieldset);
        refresh();
    }
}
