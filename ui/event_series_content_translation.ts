import type { Presentation } from "./event_series_presentation.js";

export type ContentTranslation = {
  navigation_labels?: { source: string; text: string }[];
  faq_questions?: { source: string; text: string }[];
  faq_answers?: { source: string; text: string }[];
};

export function setupSeriesContentTranslation(
  container: HTMLElement,
  onEdit: () => void,
) {
  const refs: {
    group: keyof ContentTranslation;
    source: string;
    input: HTMLInputElement | HTMLTextAreaElement;
  }[] = [];

  const render = (
    presentation: Pick<Presentation, "navigation" | "faq"> | null,
    nextTranslation?: ContentTranslation,
  ) => {
    container.replaceChildren();
    refs.length = 0;

    if (!presentation) {
      container.hidden = true;
      return;
    }

    const groups = [
      {
        key: "navigation_labels" as const,
        sources: [...new Set(presentation.navigation.map(({ label }) => label))],
        label: (source: string) => "Navigation translation: " + source,
        maxLength: 100,
        tag: "input" as const,
      },
      {
        key: "faq_questions" as const,
        sources: [...new Set(presentation.faq.map(({ question }) => question))],
        label: (source: string) => "FAQ question translation: " + source,
        maxLength: 200,
        tag: "input" as const,
      },
      {
        key: "faq_answers" as const,
        sources: [...new Set(presentation.faq.map(({ answer }) => answer))],
        label: (source: string) => "FAQ answer translation: " + source,
        maxLength: 1500,
        tag: "textarea" as const,
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

  const payload = (): ContentTranslation => {
    const result: ContentTranslation = {};

    for (const { group, source, input } of refs) {
      const text = input.value.trim();
      if (!text) continue;
      (result[group] ??= []).push({ source, text });
    }

    return result;
  };

  return { render, payload };
}
