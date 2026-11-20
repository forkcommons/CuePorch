export {};

for (const source of document.querySelectorAll<HTMLScriptElement>("[data-registration-catalogue]")) {
  const section = source.closest<HTMLElement>(".registration-gate")!;
  const catalogue: Record<string, Record<string, string>> = JSON.parse(source.textContent!);
  const picker = section.querySelector<HTMLElement>("[data-registration-language-picker]")!;
  const select = picker.querySelector<HTMLSelectElement>("select")!;
  const description = section.querySelector<HTMLElement>("[data-registration-description]");
  select.onchange = () => {
    const text = catalogue[select.value];
    if (!text) return;
    section.lang = select.value;
    for (const element of section.querySelectorAll<HTMLElement>("[data-registration-copy]")) {
      element.textContent = text[element.dataset.registrationCopy!];
    }
    if (description) description.hidden = !text.description;
  };
  picker.hidden = false;
}
