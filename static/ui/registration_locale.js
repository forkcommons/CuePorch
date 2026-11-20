for (const source of document.querySelectorAll("[data-registration-catalogue]")) {
    const section = source.closest(".registration-gate");
    const catalogue = JSON.parse(source.textContent);
    const picker = section.querySelector("[data-registration-language-picker]");
    const select = picker.querySelector("select");
    const description = section.querySelector("[data-registration-description]");
    select.onchange = () => {
        const text = catalogue[select.value];
        if (!text)
            return;
        section.lang = select.value;
        for (const element of section.querySelectorAll("[data-registration-copy]")) {
            element.textContent = text[element.dataset.registrationCopy];
        }
        if (description)
            description.hidden = !text.description;
    };
    picker.hidden = false;
}
export {};
