export interface SpeakerProfile {
  id: string; revision: string; name: string; role: string; bio: string;
}

export function setupSpeakerLineup(onEdit: (profile: SpeakerProfile) => void) {
  const select = document.querySelector<HTMLSelectElement>('#eventLandingSpeakerChoice')!;
  const addButton = document.querySelector<HTMLButtonElement>('#eventLandingSpeakerAdd')!;
  const rows = document.querySelector<HTMLOListElement>('#eventLandingSpeakerRows')!;
  const editButton = document.querySelector<HTMLButtonElement>('#eventLandingSpeakerEdit')!;

  let ids: string[] = [];
  let profiles: SpeakerProfile[] = [];

  const get = (id: string): SpeakerProfile | undefined => profiles.find((p) => p.id === id);
  const syncButtons = (): void => {
  addButton.disabled = select.value === '' || ids.includes(select.value);
  editButton.disabled = !select.value;
};

  const renderChoices = (): void => {
    const current = select.value;
    select.replaceChildren();
    for (const profile of profiles) {
      const option = document.createElement('option');
      option.value = profile.id; option.textContent = profile.name; select.append(option);
    }
    if (profiles.some((p) => p.id === current)) select.value = current;
    else if (profiles.length > 0) select.value = profiles[0].id;
    syncButtons();
  };

  const mkButton = (text: string, label: string, action: () => void): HTMLButtonElement => {
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = text; button.setAttribute('aria-label', label);
    button.addEventListener('click', action); return button;
  };

  const renderRows = (): void => {
    rows.replaceChildren();
    ids.forEach((id, index) => {
      const name = get(id)?.name ?? id;
      const li = document.createElement('li'); li.textContent = name;
      const remove = mkButton('Remove', `Remove ${name}`, () => {
        ids.splice(index, 1); renderRows(); syncButtons();
      });
      const up = mkButton('Up', `Move ${name} up`, () => {
        if (index > 0) { [ids[index - 1], ids[index]] = [ids[index], ids[index - 1]]; renderRows(); }
      });
      up.disabled = index === 0;
      const down = mkButton('Down', `Move ${name} down`, () => {
        if (index < ids.length - 1) { [ids[index + 1], ids[index]] = [ids[index], ids[index + 1]]; renderRows(); }
      });
      down.disabled = index === ids.length - 1;
      li.append(remove, up, down); rows.append(li);
    });
  };

  addButton.addEventListener('click', () => {
    const id = select.value;
    if (id !== '' && !ids.includes(id)) { ids.push(id); renderRows(); syncButtons(); }
  });
  select.addEventListener('change', syncButtons);
  editButton.addEventListener('click', () => { const profile = get(select.value); if (profile) onEdit({ ...profile }); });

  return {
    populate(nextIds: string[], nextProfiles: SpeakerProfile[]): void {
      ids = [...nextIds]; profiles = [...nextProfiles]; renderChoices(); renderRows();
    },
    selected(): string[] { return [...ids]; },
    upsert(profile: SpeakerProfile): void {
      const index = profiles.findIndex((p) => p.id === profile.id);
      if (index < 0) profiles.push({ ...profile });
      else profiles[index] = { ...profile };
      renderChoices();
      select.value = profile.id;
      syncButtons();
      renderRows();
    },
  };
}
