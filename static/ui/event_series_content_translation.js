export function setupSeriesContentTranslation(container, onEdit) {
    const refs = [];
    const render = (presentation, nextTranslation) => {
        container.replaceChildren();
        refs.length = 0;
        if (!presentation) {
            container.hidden = true;
            return;
        }
        const groups = [
            {
                key: "navigation_labels",
                sources: [...new Set(presentation.navigation.map(({ label }) => label))],
                label: (source) => "Navigation translation: " + source,
                maxLength: 100,
                tag: "input",
            },
            {
                key: "faq_questions",
                sources: [...new Set(presentation.faq.map(({ question }) => question))],
                label: (source) => "FAQ question translation: " + source,
                maxLength: 200,
                tag: "input",
            },
            {
                key: "faq_answers",
                sources: [...new Set(presentation.faq.map(({ answer }) => answer))],
                label: (source) => "FAQ answer translation: " + source,
                maxLength: 1500,
                tag: "textarea",
            },
        ];
        for (const group of groups) {
            for (const source of group.sources) {
                const label = document.createElement("label");
                label.textContent = group.label(source);
                const input = document.createElement(group.tag);
                input.maxLength = group.maxLength;
                input.value =
                    nextTranslation?.[group.key]?.find((item) => item.source === source)
                        ?.text ?? "";
                input.addEventListener("input", onEdit);
                label.append(input);
                container.append(label);
                refs.push({ group: group.key, source, input });
            }
        }
        container.hidden = refs.length === 0;
    };
    const payload = () => {
        const result = {};
        for (const { group, source, input } of refs) {
            const text = input.value.trim();
            if (!text)
                continue;
            (result[group] ??= []).push({ source, text });
        }
        return result;
    };
    return { render, payload };
}
