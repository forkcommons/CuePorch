import type { Presentation } from "./event_series_presentation.js";
import { setupSeriesContentTranslation, type ContentTranslation } from "./event_series_content_translation.js";

export interface Translation {
  published: boolean;
  title: string;
  description: string;
  date_label: string;
  timezone_label: string;
  content?: ContentTranslation;
}

export interface Localization {
  base: string;
  translations: Record<string, Translation>;
}

export function setupSeriesLocalization(root: HTMLElement) {
  const baseLanguageSelect = root.querySelector<HTMLSelectElement>('#seriesBaseLanguage')!;
  const translationLanguageSelect = root.querySelector<HTMLSelectElement>('#seriesTranslationLanguage')!;
  const fields = root.querySelector<HTMLElement>('#seriesTranslationFields')!;
  const removeButton = root.querySelector<HTMLButtonElement>('#seriesTranslationRemove')!;
  const titleInput = root.querySelector<HTMLInputElement>('[name="translatedTitle"]')!;
  const descriptionInput = root.querySelector<HTMLTextAreaElement>('[name="translatedDescription"]')!;
  const dateLabelInput = root.querySelector<HTMLInputElement>('[name="translatedDateLabel"]')!;
  const timezoneLabelInput = root.querySelector<HTMLInputElement>('[name="translatedTimezoneLabel"]')!;
  const publishedInput = root.querySelector<HTMLInputElement>('[name="translationPublished"]')!;

  const contentFields = setupSeriesContentTranslation(root.querySelector<HTMLElement>("#seriesContentTranslation")!, () => handleEdit());
  let savedPresentation: Presentation | null = null;

  let state: Localization = { base: '', translations: {} };
  let active: string | null = null;
  let hadPersistedTranslations = false;

  const copyTranslation = (translation: Translation): Translation => {
    return {
      published: Boolean(translation.published),
      title: translation.title ?? '',
      description: translation.description ?? '',
      date_label: translation.date_label ?? '',
      timezone_label: translation.timezone_label ?? '',
      content: translation.content ? structuredClone(translation.content) : undefined,
    };
  };

  const copyTranslations = (translations: Record<string, Translation>): Record<string, Translation> => {
    const copy: Record<string, Translation> = {};
    for (const [language, translation] of Object.entries(translations)) {
      copy[language] = copyTranslation(translation);
    }
    return copy;
  };

  const saveActiveTranslation = () => {
    if (active === null) {
      return;
    }
    if (active === state.base) {
      return;
    }
    state.translations[active] = {
      published: publishedInput.checked,
      title: titleInput.value,
      description: descriptionInput.value,
      date_label: dateLabelInput.value,
      timezone_label: timezoneLabelInput.value,
      content: contentFields.payload(),
    };
  };

  const loadActiveTranslation = () => {
    const translation = active ? state.translations[active] : undefined;
    titleInput.value = translation?.title ?? '';
    descriptionInput.value = translation?.description ?? '';
    dateLabelInput.value = translation?.date_label ?? '';
    timezoneLabelInput.value = translation?.timezone_label ?? '';
    publishedInput.checked = translation?.published ?? false;
    contentFields.render(active ? savedPresentation : null, translation?.content);
  };

  const updateBaseLocked = () => {
    const hasCurrentTranslations = Object.keys(state.translations).length > 0;
    baseLanguageSelect.disabled = hadPersistedTranslations || hasCurrentTranslations;
  };

  const updateTranslationOptions = () => {
    for (const option of Array.from(translationLanguageSelect.options)) {
      option.hidden = option.value === state.base;
    }
  };

  const updateRemoveButton = () => {
    const hasSelectedEntry =
      active !== null &&
      active !== state.base &&
      Object.prototype.hasOwnProperty.call(state.translations, active);
    removeButton.disabled = !hasSelectedEntry;
  };

  const refreshBase = () => {
    updateBaseLocked();
    updateTranslationOptions();
  };

  const renderContent = () => {
    if (active === state.base) {
      active = null;
    }
    fields.hidden = active === null || active === state.base;
    translationLanguageSelect.value = active ?? '';
    loadActiveTranslation();
    updateRemoveButton();
  };

  const refresh = () => {
    refreshBase();
    renderContent();
  };

  const handleEdit = () => {
    saveActiveTranslation();
    updateBaseLocked();
    updateRemoveButton();
  };

  titleInput.addEventListener('input', handleEdit);
  descriptionInput.addEventListener('input', handleEdit);
  dateLabelInput.addEventListener('input', handleEdit);
  timezoneLabelInput.addEventListener('input', handleEdit);
  publishedInput.addEventListener('change', handleEdit);

  translationLanguageSelect.addEventListener('change', () => {
    active = translationLanguageSelect.value || null;
    renderContent();
  });

  baseLanguageSelect.addEventListener('change', () => {
    state.base = baseLanguageSelect.value;
    refresh();
  });

  removeButton.addEventListener('click', () => {
    if (active === null) {
      return;
    }
    if (active === state.base) {
      return;
    }
    delete state.translations[active];
    active = null;
    refresh();
  });

  return {
    populate(loc: Localization, presentation: Presentation) {
      savedPresentation = presentation;
      state = {
        base: loc.base,
        translations: copyTranslations(loc.translations),
      };
      hadPersistedTranslations = Object.keys(loc.translations).length > 0;
      active = null;
      baseLanguageSelect.value = loc.base;
      refresh();
    },
    applyDraft(language: string, translation: Translation) {
      state.translations[language] = { ...translation, published: false };
      active = language;
      refresh();
    },
    payload(): Localization {
      return {
        base: state.base,
        translations: copyTranslations(state.translations),
      };
    },
  };
}
